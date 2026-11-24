#!/usr/bin/env python3
"""Slideshow for the small SPI TFT (fbtft) on a Raspberry Pi.

Shows the JPEGs from a directory (default: /mnt/stick/bilder) in order on
the framebuffer, with a hard cut or a transition (e.g. "stack").

Unlike fbi, this script writes directly into the framebuffer (mmap), because
that is the only way to render transition frames. As with fb-image-watcher.sh:
 - the framebuffer number may vary (FB_DEVICE, default /dev/fb1)
 - the waveshare35a overlay drives the panel with inverted colors
   -> detected from the device tree (or forced with --invert on/off)

Clock:             7-segment HH:MM lower left, only while the time is synced
Pause/resume:      kill -USR1 <pid>   (or pkill -USR1 -f fb-slideshow.py)
Color diagnostics: --testcard [bars|ramps] [--gamma 2.0 | R,G,B]

Dependencies:  sudo apt install python3-pil python3-numpy
"""
import argparse
import math
import mmap
import os
import random
import re
import signal
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import numpy as np
from PIL import Image, ImageOps

DEFAULT_DIR = "/mnt/stick/bilder"
DEFAULT_FB = "/dev/fb1"
DT_SPI = "/proc/device-tree/soc/spi@7e204000"
SYSFS_GRAPHICS = "/sys/class/graphics"
IMAGE_EXT = (".jpg", ".jpeg")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Framebuffer
# --------------------------------------------------------------------------
class FbInfo(NamedTuple):
    width: int
    height: int
    bpp: int
    stride: int  # bytes per row


def read_fb_info(dev, sysfs=SYSFS_GRAPHICS):
    """Geometry from sysfs (no fbset needed)."""
    base = os.path.join(sysfs, os.path.basename(dev))

    def rd(name):
        with open(os.path.join(base, name)) as f:
            return f.read().strip()

    w, h = (int(x) for x in rd("virtual_size").split(","))
    bpp = int(rd("bits_per_pixel"))
    try:
        stride = int(rd("stride"))
    except OSError:
        stride = w * bpp // 8
    return FbInfo(w, h, bpp, stride)


def detect_invert(dt_dir=DT_SPI):
    """waveshare35a drives the panel inverted, tft35a does not."""
    try:
        names = os.listdir(dt_dir)
    except OSError:
        return False
    return any(n.lower().startswith("waveshare35a@") for n in names)


def _triple(x):
    return (float(x),) * 3 if isinstance(x, (int, float)) else tuple(float(v) for v in x)


def apply_gamma(frame, gamma, limit=255.0, black=0.0):
    """Per-channel tone curve: out = black + (limit - black) * (in/255)**gamma.

    gamma: number or (R,G,B); >1 darkens the midtones.
    limit: highest raw value used (number or (R,G,B), 1..255). For panels
    with a folded response curve, use only the first monotonic range.
    black: raw value for input 0 (number or (R,G,B)); compensates a color
    cast in the blacks by lifting the weaker channels."""
    g, lim, bl = _triple(gamma), _triple(limit), _triple(black)
    if g == (1.0,) * 3 and lim == (255.0,) * 3 and bl == (0.0,) * 3:
        return frame
    x = np.arange(256) / 255.0
    lut = np.stack([np.round(b + (li - b) * x ** gi)
                    for gi, li, b in zip(g, lim, bl)], axis=1)
    return lut.astype(np.uint8)[frame, np.arange(3)]


_BAYER4 = (np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]],
                    np.float32) + 0.5) / 16


def dither565(frame):
    """Ordered 4x4 dithering before reduction to RGB565 (R/B 5 bits, G 6 bits)."""
    h, w, _ = frame.shape
    t = np.tile(_BAYER4, (-(-h // 4), -(-w // 4)))[:h, :w]
    out = frame.astype(np.float32) + t[..., None] * np.array([8, 4, 8], np.float32)
    return np.minimum(out, 255).astype(np.uint8)


def parse_gamma(s):
    try:
        vals = [float(v) for v in s.split(",")]
    except ValueError:
        vals = []
    if len(vals) == 1:
        vals *= 3
    if len(vals) != 3 or any(v <= 0 for v in vals):
        raise argparse.ArgumentTypeError(
            "format: G or R,G,B with values > 0, e.g. 2.2 or 2.4,1.8,2.4")
    return tuple(vals)


def parse_black(s):
    try:
        vals = [float(v) for v in s.split(",")]
    except ValueError:
        vals = []
    if len(vals) == 1:
        vals *= 3
    if len(vals) != 3 or any(not 0 <= v <= 254 for v in vals):
        raise argparse.ArgumentTypeError(
            "format: N or R,G,B with 0 <= N <= 254, e.g. 22,16,0")
    return tuple(vals)


def parse_limit(s):
    try:
        vals = [float(v) for v in s.split(",")]
    except ValueError:
        vals = []
    if len(vals) == 1:
        vals *= 3
    if len(vals) != 3 or any(not 1 <= v <= 255 for v in vals):
        raise argparse.ArgumentTypeError(
            "format: N or R,G,B with 1 <= N <= 255, e.g. 85")
    return tuple(vals)


def encode_frame(frame, info, invert, dither=False):
    """RGB frame (HxWx3 uint8) -> raw bytes for the framebuffer."""
    if invert:
        frame = 255 - frame
    if dither and info.bpp == 16:
        frame = dither565(frame)
    h, w, _ = frame.shape
    r, g, b = frame[..., 0], frame[..., 1], frame[..., 2]
    if info.bpp == 16:  # RGB565, little endian (fbtft)
        px = (
            ((r.astype(np.uint16) >> 3) << 11)
            | ((g.astype(np.uint16) >> 2) << 5)
            | (b.astype(np.uint16) >> 3)
        )
        row = px.astype("<u2").view(np.uint8).reshape(h, w * 2)
    elif info.bpp == 32:  # BGRA (e.g. HDMI framebuffer)
        px = np.empty((h, w, 4), np.uint8)
        px[..., 0], px[..., 1], px[..., 2], px[..., 3] = b, g, r, 255
        row = px.reshape(h, w * 4)
    else:
        raise ValueError(f"bpp {info.bpp} not supported (only 16/32)")
    if info.stride == row.shape[1]:
        return row.tobytes()
    out = np.zeros((h, info.stride), np.uint8)
    out[:, : row.shape[1]] = row
    return out.tobytes()


# --------------------------------------------------------------------------
# Clock (7-segment, lower left corner)
# --------------------------------------------------------------------------
TIMESYNC_FILE = "/run/systemd/timesync/synchronized"
DISPLAY_DIAGONAL_IN = 3.5
CLOCK_MARGIN_CM = 0.25  # from the left and the bottom edge
CLOCK_HEIGHT_CM = 0.5   # digit height
CLOCK_SLANT = 0.10      # forward slant of the digits (dx per height)

# segments a b c d e f g of the digits 0-9
SEGMENTS = {
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
    "5": "afgcd", "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcdfg",
}


class TimeSync:
    """Is the system time trustworthy? systemd-timesyncd touches TIMESYNC_FILE
    on every successful sync. The file is in /run (RAM), so it is missing after
    power-on until the first sync. After the last sync the time is trusted for
    max_age seconds."""

    def __init__(self, path=TIMESYNC_FILE, max_age=48 * 3600, now=time.time):
        self.path, self.max_age, self.now = path, max_age, now

    def available(self):
        try:
            synced = os.stat(self.path).st_mtime
        except OSError:
            return False
        return self.now() - synced <= self.max_age


def _segment_polygons(h, w, t):
    """Hexagonal segments of one digit (height h, width w, thickness t),
    upright, origin top left."""
    g = t * 0.12  # small gap between the segments
    xl, xr = t / 2, w - t / 2
    yt, ym, yb = t / 2, h / 2, h - t / 2

    def hor(x0, x1, y):
        x0, x1 = x0 + g, x1 - g
        return [(x0, y), (x0 + t / 2, y - t / 2), (x1 - t / 2, y - t / 2),
                (x1, y), (x1 - t / 2, y + t / 2), (x0 + t / 2, y + t / 2)]

    def ver(x, y0, y1):
        y0, y1 = y0 + g, y1 - g
        return [(x, y0), (x + t / 2, y0 + t / 2), (x + t / 2, y1 - t / 2),
                (x, y1), (x - t / 2, y1 - t / 2), (x - t / 2, y0 + t / 2)]

    return {"a": hor(xl, xr, yt), "g": hor(xl, xr, ym), "d": hor(xl, xr, yb),
            "f": ver(xl, yt, ym), "b": ver(xr, yt, ym),
            "e": ver(xl, ym, yb), "c": ver(xr, ym, yb)}


def render_clock(text, h, scale=4):
    """'HH:MM' (a space blanks a digit) -> (alpha, shadow), float32 masks
    0..1 of the same size. The shadow is a soft dark rim that keeps the white
    digits readable on bright pictures."""
    from PIL import ImageDraw, ImageFilter
    pad = max(2, round(h * 0.06))
    t, dw, gap = h * 0.15, h * 0.55, h * 0.22
    widths = [t if c == ":" else dw for c in text]
    W = sum(widths) + gap * (len(text) - 1) + h * CLOCK_SLANT + 2 * pad
    H = h + 2 * pad
    img = Image.new("L", (math.ceil(W * scale), math.ceil(H * scale)), 0)
    draw = ImageDraw.Draw(img)

    def put(points, x):  # slant and place into the image
        draw.polygon([((pad + x + px + (h - py) * CLOCK_SLANT) * scale,
                       (pad + py) * scale) for px, py in points], fill=255)

    segs = _segment_polygons(h, dw, t)
    x = 0.0
    for c, cw in zip(text, widths):
        if c == ":":
            for cy in (h * 0.32, h * 0.68):
                put([(0, cy - t / 2), (t, cy - t / 2), (t, cy + t / 2),
                     (0, cy + t / 2)], x)
        else:
            for s in SEGMENTS.get(c, ""):
                put(segs[s], x)
        x += cw + gap
    size = (math.ceil(W), math.ceil(H))
    alpha = img.resize(size, Image.BOX)
    rim = alpha.filter(ImageFilter.MaxFilter(2 * (pad // 2) + 1)).filter(
        ImageFilter.GaussianBlur(pad / 2))
    return (np.asarray(alpha, np.float32) / 255,
            np.asarray(rim, np.float32) / 255)


class ClockOverlay:
    """White 7-segment clock (HH:MM, 24 h) in the lower left corner; only
    while the time is trustworthy (see TimeSync)."""

    SHADOW = 0.55  # darkening at the rim

    def __init__(self, size, sync, diagonal_in=DISPLAY_DIAGONAL_IN,
                 now=time.time):
        w, h = size
        px_cm = math.hypot(w, h) / (diagonal_in * 2.54)
        self.sync, self.now = sync, now
        self.height = max(7, round(CLOCK_HEIGHT_CM * px_cm))
        alpha, _ = render_clock("88:88", self.height)
        pad = max(2, round(self.height * 0.06))
        margin = round(CLOCK_MARGIN_CM * px_cm)
        self.x0 = margin - pad
        y_bottom = h - margin + pad
        self.y0 = y_bottom - alpha.shape[0]
        if self.x0 < 0 or self.y0 < 0 or self.x0 + alpha.shape[1] > w:
            raise ValueError(f"display {w}x{h} too small for the clock")
        # whole rows, aligned to 4 (Bayer dither phase), for partial updates
        self.rows = (self.y0 // 4 * 4, min(h, -(-y_bottom // 4) * 4))
        self._cache = {}

    def text(self):
        """Current 'HH:MM', or None when the clock is not to be shown."""
        if not self.sync.available():
            return None
        tm = time.localtime(self.now())
        return f"{tm.tm_hour:2d}:{tm.tm_min:02d}"  # classic: blank leading 0

    def apply(self, frame, text):
        """frame with the clock drawn in (a copy), or frame itself for None."""
        if text is None:
            return frame
        if text not in self._cache:
            self._cache = {text: render_clock(text, self.height)}
        alpha, rim = self._cache[text]
        h, w = alpha.shape
        out = frame.copy()
        box = out[self.y0:self.y0 + h, self.x0:self.x0 + w].astype(np.float32)
        box *= (1 - self.SHADOW * rim)[..., None]
        box += (255 - box) * alpha[..., None]
        out[self.y0:self.y0 + h, self.x0:self.x0 + w] = np.round(box).astype(np.uint8)
        return out


class Display:
    def __init__(self, dev, info, invert, gamma=1.0, limit=255.0, dither=False,
                 black=0.0, overlay=None):
        self.info = info
        self.invert = invert
        self.gamma = gamma
        self.limit = limit
        self.dither = dither
        self.black = black
        self.overlay = overlay
        self.size = (info.width, info.height)
        self._frame = None   # last frame, without overlay
        self._text = None    # overlay text on the panel
        length = info.stride * info.height
        self._f = open(dev, "r+b")
        if stat.S_ISREG(os.fstat(self._f.fileno()).st_mode):
            self._f.truncate(length)  # tests: regular file as fake framebuffer
        self._m = mmap.mmap(self._f.fileno(), length)

    def _encode(self, frame):
        frame = apply_gamma(frame, self.gamma, self.limit, self.black)
        return encode_frame(frame, self.info, self.invert, self.dither)

    def _overlay_text(self):
        text = self.overlay.text()
        if (text is None) != (self._text is None):
            log("clock shown (time synchronized)" if text else
                "clock hidden (no time synchronization)")
        return text

    def show(self, frame):
        self._frame = frame
        if self.overlay is not None:
            self._text = self._overlay_text()
            frame = self.overlay.apply(frame, self._text)
        data = self._encode(frame)
        self._m[: len(data)] = data

    def refresh(self):
        """Redraw the overlay if it changed (e.g. a new minute). Writes only
        the clock's rows, so fbtft sends only those over SPI."""
        if self.overlay is None or self._frame is None:
            return
        text = self._overlay_text()
        if text == self._text:
            return
        self._text = text
        y0, y1 = self.overlay.rows
        self.show_rows(self.overlay.apply(self._frame, text), y0, y1)

    def show_rows(self, frame, y0, y1):
        """Write only rows y0..y1 of frame; fbtft then sends only those over
        SPI. Keep y0 a multiple of 4 when dithering (Bayer pattern phase)."""
        data = self._encode(frame[y0:y1])
        off = y0 * self.info.stride
        self._m[off: off + len(data)] = data

    def blank(self):
        """Deepest black of the panel: raw value 0, without gamma/limit/black/dither."""
        w, h = self.size
        data = encode_frame(np.zeros((h, w, 3), np.uint8), self.info, self.invert)
        self._m[: len(data)] = data

    def close(self):
        self._m.close()
        self._f.close()


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------
def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def list_images(directory):
    """JPEGs, naturally sorted (img_2 before img_10); empty if the stick is missing."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    good = [
        n for n in names
        if n.lower().endswith(IMAGE_EXT) and not n.startswith(".")
    ]
    return [os.path.join(directory, n) for n in sorted(good, key=natural_key)]


def playlist(directory, shuffle, rng, loop=True, empty_wait=5.0,
             sleep=time.sleep, log=log):
    """Endless generator; rescans the directory every round."""
    while True:
        files = list_images(directory)
        if not files:
            if not loop:
                return
            log(f"No images in {directory} (stick not mounted?) - waiting")
            sleep(empty_wait)
            continue
        if shuffle:
            rng.shuffle(files)
        yield from files
        if not loop:
            return


def load_image(path, size, fill=False):
    """Load an image and fit it to the display size -> HxWx3 uint8."""
    w, h = size
    m = max(w, h)
    with Image.open(path) as im:
        im.draft("RGB", (m, m))  # fast downscaling inside the JPEG decoder
        im = ImageOps.exif_transpose(im).convert("RGB")
    if fill:
        im = ImageOps.fit(im, (w, h), Image.LANCZOS)
    else:
        im = ImageOps.contain(im, (w, h), Image.LANCZOS)
        canvas = Image.new("RGB", (w, h))
        canvas.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
        im = canvas
    return np.array(im, dtype=np.uint8)


# --------------------------------------------------------------------------
# Transitions: make_xxx(rng, w, h) -> f(a, b, p), p in (0,1) -> frame.
# Random parameters (direction, center, block size ...) are rolled on each
# call of the factory, so that no two transitions are ever identical.
# --------------------------------------------------------------------------
def _smooth(p):
    return p * p * (3 - 2 * p)


# Easing: maps elapsed time (0..1) to progress of the motion (0..1).
EASINGS = {
    "ease-in-out": _smooth,                # starts and ends gently
    "ease-out": lambda p: 1 - (1 - p) ** 2,  # starts at full speed, settles
    "linear": lambda p: p,
}


def _mix(a, b, p):
    k = int(p * 256)
    return (
        (a.astype(np.uint16) * (256 - k) + b.astype(np.uint16) * k) >> 8
    ).astype(np.uint8)


def _blend(a, b, mask):
    """mask: float 0..1, (H,W) or broadcastable; 1 = b."""
    af = a.astype(np.float32)
    out = af + (b.astype(np.float32) - af) * mask[..., None]
    return (out + 0.5).astype(np.uint8)


def _grid(w, h):
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    return xs, ys


def make_cut(rng, w, h):
    """Hard cut: the next picture replaces the current one at once."""
    return lambda a, b, p: b


def make_fade(rng, w, h):
    return _mix


def make_wipe(rng, w, h):
    """Soft edge that sweeps across the image in one of 8 directions."""
    ang = math.radians(rng.choice([0, 45, 90, 135, 180, 225, 270, 315]))
    xs, ys = _grid(w, h)
    c = xs * math.cos(ang) + ys * math.sin(ang)
    c = (c - c.min()) / (c.max() - c.min())
    f = 0.2

    def run(a, b, p):
        return _blend(a, b, np.clip((p * (1 + f) - c) / f, 0, 1))
    return run


def make_push(rng, w, h):
    """The new image pushes the old one out of the frame."""
    d = rng.choice(["left", "right", "up", "down"])

    def run(a, b, p):
        if d in ("left", "right"):
            off = int(round(w * p))
            if d == "left":
                return np.concatenate((a[:, off:], b[:, :off]), axis=1)
            return np.concatenate((b[:, w - off:], a[:, : w - off]), axis=1)
        off = int(round(h * p))
        if d == "up":
            return np.concatenate((a[off:], b[:off]), axis=0)
        return np.concatenate((b[h - off:], a[: h - off]), axis=0)
    return run


class StackParams(NamedTuple):
    dx: int       # exit direction: (dx, dy) is one of (±1, 0), (0, ±1)
    dy: int
    drift: float  # sideways offset at the end, as a fraction of the
                  # perpendicular display size (like a hand pulling it off)


STACK_DIRECTIONS = [(1, 0), (-1, 0), (0, 1), (0, -1)]  # right, left, down, up
STACK_MAX_DRIFT = 0.08


def stack_params(rng):
    """One variation of the stack transition, drawn from the seeded rng."""
    dx, dy = rng.choice(STACK_DIRECTIONS)
    return StackParams(dx, dy, rng.uniform(-STACK_MAX_DRIFT, STACK_MAX_DRIFT))


def stack_position(t, params, size):
    """Top-left corner (x, y) of the top picture at progress t (0..1).

    Pure function: t = 0 is the resting position (0, 0); at t = 1 the picture
    has moved one full display width/height and is completely off-screen."""
    w, h = size
    if params.dx:
        return params.dx * w * t, params.drift * h * t
    return params.drift * w * t, params.dy * h * t


def make_stack(rng, w, h):
    """Stack of photos: the current picture slides off to the left, right, top
    or bottom (with a slight sideways drift), the next one already lies still
    underneath. The moving picture casts a soft shadow onto the one below.
    Only the offset of the top picture changes; both pictures are pre-scaled."""
    params = stack_params(rng)
    shadow = 0.04 * max(w, h)  # width of the shadow in pixels
    depth = 0.35               # darkening right at the edge (0..1)
    xs = np.arange(w, dtype=np.float32)
    ys = np.arange(h, dtype=np.float32)

    def run(a, b, p):
        fx, fy = stack_position(p, params, (w, h))
        sx, sy = int(round(fx)), int(round(fy))
        # distance of every pixel from the moved picture's rectangle
        ex = np.maximum(np.maximum(sx - xs, xs - (sx + w - 1)), 0)
        ey = np.maximum(np.maximum(sy - ys, ys - (sy + h - 1)), 0)
        dist = np.hypot(ex[None, :], ey[:, None])
        shade = 1 - depth * np.clip(1 - dist / shadow, 0, 1)
        out = (b * shade[..., None] + 0.5).astype(np.uint8)
        x0, x1 = max(sx, 0), min(sx + w, w)
        y0, y1 = max(sy, 0), min(sy + h, h)
        if x0 < x1 and y0 < y1:
            out[y0:y1, x0:x1] = a[y0 - sy:y1 - sy, x0 - sx:x1 - sx]
        return out
    return run


def make_blinds(rng, w, h):
    """Venetian blinds: n stripes open simultaneously."""
    vertical = rng.random() < 0.5
    n = rng.choice([6, 8, 10, 12, 16])
    size = w if vertical else h
    local = (np.arange(size, dtype=np.float32) * n / size) % 1.0

    def run(a, b, p):
        m = local < p
        m = m[None, :, None] if vertical else m[:, None, None]
        return np.where(m, b, a)
    return run


def make_dissolve(rng, w, h):
    """Random blocks (mosaic), rows (TV scanlines) or columns."""
    bh, bw = rng.choice([(8, 8), (16, 16), (32, 32), (2, w), (h, 6)])
    ny, nx = -(-h // bh), -(-w // bw)
    gen = np.random.default_rng(rng.getrandbits(32))
    rank = gen.permutation(ny * nx).reshape(ny, nx) / (ny * nx)
    full = np.repeat(np.repeat(rank, bh, 0), bw, 1)[:h, :w]

    def run(a, b, p):
        return np.where((full < p)[..., None], b, a)
    return run


def make_iris(rng, w, h):
    """A circle grows from a random point (or shrinks to reveal the new image)."""
    cx, cy = w * rng.uniform(0.25, 0.75), h * rng.uniform(0.25, 0.75)
    reverse = rng.random() < 0.5
    xs, ys = _grid(w, h)
    dist = np.hypot(xs - cx, ys - cy)
    rmax = float(dist.max())
    fe = 0.06 * max(w, h)

    def run(a, b, p):
        if not reverse:
            r = p * (rmax + fe)
            return _blend(a, b, np.clip((r - dist) / fe, 0, 1))
        r = (1 - p) * (rmax + fe) - fe
        return _blend(a, b, np.clip((dist - r) / fe, 0, 1))
    return run


def make_zoom(rng, w, h):
    """Old image zooms away / new image zooms in, cross-fading meanwhile."""
    zoom_out_old = rng.random() < 0.5
    amount = 0.5

    def zoomed(img, s):
        cw, ch = w / s, h / s
        box = ((w - cw) / 2, (h - ch) / 2, (w + cw) / 2, (h + ch) / 2)
        return np.asarray(
            Image.fromarray(img).resize((w, h), Image.BILINEAR, box=box)
        )

    def run(a, b, p):
        if zoom_out_old:
            return _mix(zoomed(a, 1 + amount * p), b, p)
        return _mix(a, zoomed(b, 1 + amount * (1 - p)), p)
    return run


def make_split(rng, w, h):
    """Curtain: opens from the center outwards (or from the edges to the center)."""
    vertical = rng.random() < 0.5
    edges_first = rng.random() < 0.5
    size = w if vertical else h
    d = np.abs(np.arange(size, dtype=np.float32) - (size - 1) / 2) / (size / 2)
    d = np.clip(d, 0, 1)
    c = 1 - d if edges_first else d
    c = c[None, :] if vertical else c[:, None]
    f = 0.15

    def run(a, b, p):
        return _blend(a, b, np.broadcast_to(
            np.clip((p * (1 + f) - c) / f, 0, 1), (h, w)))
    return run


TRANSITIONS = {
    "cut": make_cut,
    "fade": make_fade,
    "wipe": make_wipe,
    "push": make_push,
    "stack": make_stack,
    "blinds": make_blinds,
    "dissolve": make_dissolve,
    "iris": make_iris,
    "zoom": make_zoom,
    "split": make_split,
}


def make_transition(name, rng, size):
    """Factory + guarantee: p<=0 returns exactly a, p>=1 exactly b."""
    if name not in TRANSITIONS:
        raise ValueError(f"Unknown transition: {name}")
    fn = TRANSITIONS[name](rng, size[0], size[1])

    def run(a, b, p):
        if p <= 0:
            return a
        if p >= 1:
            return b
        return fn(a, b, p)
    return run


# The SPI display (15 MHz, 480x320 at 16 bit = 2.5 Mbit per frame) shows
# about 6 full frames per second. Frames computed faster are never seen and
# only cost CPU (and power): transitions are limited to MAX_FPS.
MAX_FPS = 8


def run_transition(display, fn, a, b, duration, clock=time.monotonic,
                   easing=_smooth, sleep=time.sleep, max_fps=MAX_FPS):
    """Time-driven: each frame shows the state for the current time, so slow
    frames are skipped instead of stretching the transition. At most max_fps
    frames per second are computed; the time left of a frame is slept."""
    if duration <= 0:
        display.show(b)
        return
    t0 = due = clock()
    while True:
        now = clock()
        if now < due:  # faster than max_fps: wait for the next frame
            sleep(due - now)
            now = due
        p = (now - t0) / duration
        if p >= 1:
            break
        display.show(fn(a, b, easing(max(p, 0.0))))
        due = now + 1 / max_fps
    display.show(b)


# --------------------------------------------------------------------------
# Photo pile (issue #20): with the stack transition the pictures lie as a
# pile of prints on a dark table. The top one lies straight, the next one
# peeks out below it, slightly turned; the top one is drawn away and the
# next one straightens up into its place while a new one shows up below.
# --------------------------------------------------------------------------
PILE_SCALE = 1.0             # box of the top print, part of the screen
PILE_BORDER = 0.01           # white border, part of the screen height
PILE_TABLE = (20, 20, 22)
PILE_BORDER_COLOR = (236, 234, 228)
PILE_TURN_MIN = 0.4          # least turn, part of --pile-turn
PILE_SHIFT = 0.025           # offset of the print below, part of the screen
PILE_SHADOW = 0.55           # darkness of the shadow under a print


class Card(NamedTuple):
    """A print: the picture with a white border (RGBA) and its soft shadow
    (L, larger by 2 * pad), unturned."""
    image: Image.Image
    shadow: Image.Image
    pad: int


class Pose(NamedTuple):
    angle: float  # degrees, counterclockwise
    dx: float     # offset of the center from the screen center, px
    dy: float


TOP = Pose(0.0, 0.0, 0.0)


def load_card(path, size, fill=False):
    """Load a picture as a print for the pile (fits the top box keeping its
    aspect; --fill: cropped to the box)."""
    from PIL import ImageFilter
    w, h = size
    border = max(2, round(min(w, h) * PILE_BORDER))
    box = (round(w * PILE_SCALE) - 2 * border, round(h * PILE_SCALE) - 2 * border)
    with Image.open(path) as im:
        im.draft("RGB", (max(box), max(box)))
        im = ImageOps.exif_transpose(im).convert("RGB")
    im = (ImageOps.fit if fill else ImageOps.contain)(im, box, Image.LANCZOS)
    card = Image.new("RGBA", (im.width + 2 * border, im.height + 2 * border),
                     PILE_BORDER_COLOR + (255,))
    card.paste(im, (border, border))
    pad = max(4, round(min(w, h) * 0.03))
    shadow = Image.new("L", (card.width + 2 * pad, card.height + 2 * pad), 0)
    shadow.paste(round(255 * PILE_SHADOW),
                 (pad, pad, pad + card.width, pad + card.height))
    shadow = shadow.filter(ImageFilter.GaussianBlur(pad / 2))
    return Card(card, shadow, pad)


def pile_pose(rng, size, turn=0.0):
    """Random pose of a print lying below the top one. turn: largest angle
    in degrees (0: not turned, only shifted)."""
    w, h = size
    angle = (rng.uniform(PILE_TURN_MIN * turn, turn) * rng.choice((-1, 1))
             if turn > 0 else 0.0)
    return Pose(angle, rng.uniform(-1, 1) * PILE_SHIFT * w,
                rng.uniform(-1, 1) * PILE_SHIFT * h)


def mix_pose(a, b, q):
    return Pose(*(x + (y - x) * q for x, y in zip(a, b)))


def draw_card(canvas, card, pose, opacity=1.0, shift=(0, 0)):
    """Lay a print (with its shadow) onto the canvas (PIL RGB)."""
    W, H = canvas.size
    img, shadow = card.image, card.shadow
    if pose.angle:
        img = img.rotate(pose.angle, Image.BILINEAR, expand=True)
        shadow = shadow.rotate(pose.angle, Image.BILINEAR, expand=True)
    alpha = img.getchannel("A")
    if opacity < 1:
        alpha = alpha.point(lambda v: round(v * opacity))
        shadow = shadow.point(lambda v: round(v * opacity))
    cx = W / 2 + pose.dx + shift[0]
    cy = H / 2 + pose.dy + shift[1]
    off = max(1, card.pad // 3)  # light from the top left
    sx, sy = round(cx - shadow.width / 2) + off, round(cy - shadow.height / 2) + off
    canvas.paste((0, 0, 0), (sx, sy, sx + shadow.width, sy + shadow.height), shadow)
    canvas.paste(img.convert("RGB"), (round(cx - img.width / 2),
                                      round(cy - img.height / 2)), alpha)


def pile_frame(size, top, under=None, under_pose=None):
    """The pile at rest: top print straight, the next one below it."""
    canvas = Image.new("RGB", size, PILE_TABLE)
    if under is not None:
        draw_card(canvas, under, under_pose)
    if top is not None:
        draw_card(canvas, top, TOP)
    return np.asarray(canvas)


def make_pile_transition(rng, size, top, under, under_pose, new, new_pose):
    """Top print drawn away (direction as for stack), the one below
    straightens up into its place, a new one fades in at the bottom.
    f(a, b, p) as for the transitions; a and b are the rest frames."""
    params = stack_params(rng)
    table = Image.new("RGB", size, PILE_TABLE)
    bottom = table.copy()
    if new is not None:
        draw_card(bottom, new, new_pose)

    def run(a, b, p):
        if p <= 0:
            return a
        if p >= 1:
            return b
        canvas = Image.blend(table, bottom, min(p / 0.3, 1.0))
        if under is not None:
            q = _smooth(min(max((p - 0.2) / 0.8, 0.0), 1.0))
            draw_card(canvas, under, mix_pose(under_pose, TOP, q))
        draw_card(canvas, top, TOP, shift=stack_position(p, params, size))
        return np.asarray(canvas)
    return run


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def make_testcard(w, h, kind="bars"):
    """Diagnostic test card.

    bars : color bars (top) + 16 gray levels (bottom)
    ramps: 16 steps each for gray, red, green, blue (rows from top to bottom)
    """
    img = np.zeros((h, w, 3), np.uint8)
    steps = ((np.arange(w) * 16 // w) * 17).astype(np.uint8)
    if kind == "ramps":
        for i, ch in enumerate((None, 0, 1, 2)):
            rows = slice(i * h // 4, (i + 1) * h // 4)
            if ch is None:
                img[rows] = steps[None, :, None]
            else:
                img[rows, :, ch] = steps[None, :]
        return img
    bars = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
            (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
    top = h // 2
    for i, c in enumerate(bars):
        img[:top, i * w // 8:(i + 1) * w // 8] = c
    img[top:] = steps[None, :, None]
    return img


def slideshow(display, paths, hold, duration, fill, transitions, rng,
              sleep=time.sleep, clock=time.monotonic, paused=lambda: False,
              easing=_smooth, refresh=None, stop=None, start=None, pile=False,
              pile_turn=0.0):
    """refresh: called about once a second while an image stands (clock).
    stop: checked after each image's hold time; True ends the show.
    start: frame on the display now (the first transition starts from it).
    pile: the pictures as a pile of prints (issue #20; meant for "stack");
    pile_turn: largest turn of the print below, degrees (0: straight).
    Returns (last frame shown, paths preloaded but not shown, in order), so a
    following show can continue without skipping a picture."""
    size = display.size

    def wait(seconds):
        if refresh is None:
            sleep(seconds)
            return
        end = clock() + seconds
        while True:
            refresh()
            left = end - clock()
            if left <= 0:
                return
            sleep(min(left, 1.0))

    w, h = size
    paths = iter(paths)
    loader = ThreadPoolExecutor(max_workers=1)

    def load(path):
        try:
            if pile:
                return path, load_card(path, size, fill)
            return path, load_image(path, size, fill)
        except Exception as e:  # broken / partially copied file
            log(f"Skipping {path}: {e}")
            return path, None

    def submit():
        p = next(paths, None)
        return None if p is None else loader.submit(load, p)

    prev = np.zeros((h, w, 3), np.uint8) if start is None else start
    if pile:
        try:
            return _pile_show(display, submit, prev, hold, duration, rng,
                              easing, wait, paused, sleep, refresh, stop, clock,
                              pile_turn)
        finally:
            loader.shutdown(wait=False, cancel_futures=True)
    prev_path = None
    last = None
    fut = submit()
    try:
        while fut is not None:
            path, img = fut.result()
            fut = submit()  # load the next image during the hold time
            if img is None:
                continue
            if path != prev_path:
                choices = [t for t in transitions if t != last] or transitions
                last = rng.choice(choices)
                fn = make_transition(last, rng, size)
                run_transition(display, fn, prev, img,
                               0 if last == "cut" else duration, clock, easing,
                               sleep)
                prev, prev_path = img, path
            wait(hold)
            while paused():  # image stays until the next SIGUSR1 arrives
                sleep(0.2)
                if refresh is not None:
                    refresh()
            if stop is not None and stop():
                return prev, [fut.result()[0]] if fut is not None else []
        return prev, []
    finally:
        loader.shutdown(wait=False, cancel_futures=True)


def _pile_show(display, submit, prev, hold, duration, rng, easing, wait,
               paused, sleep, refresh, stop, clock, turn=0.0):
    """slideshow() with the pile: the next print is loaded before the top one
    is drawn away, the one after it during the hold."""
    size = display.size
    fut = submit()

    def take():
        """Next loaded print (path, card, pose) or None at the end; starts
        loading the one after it."""
        nonlocal fut
        while fut is not None:
            path, card = fut.result()
            fut = submit()
            if card is not None:
                return path, card, pile_pose(rng, size, turn)
        return None

    top, under = take(), None
    if top is None:
        return prev, []
    under = take()

    def rest(top, under):
        return pile_frame(size, top[1], *(under[1:] if under else (None, None)))

    frame = rest(top, under)
    # whatever is on the display slides off the pile
    run_transition(display, make_transition("stack", rng, size), prev, frame,
                   duration, clock, easing, sleep)
    while True:
        wait(hold)
        while paused():
            sleep(0.2)
            if refresh is not None:
                refresh()
        if stop is not None and stop():
            unused = [under[0]] if under else []
            if fut is not None:
                unused.append(fut.result()[0])
            return frame, unused
        if under is None:
            return frame, []
        new = take()
        nxt = rest(under, new)
        fn = make_pile_transition(rng, size, top[1], under[1], under[2],
                                  *(new[1:] if new else (None, None)))
        run_transition(display, fn, frame, nxt, duration, clock, easing, sleep)
        top, under, frame = under, new, nxt


def parse_size(s):
    m = re.fullmatch(r"(\d+)x(\d+)", s)
    if not m:
        raise argparse.ArgumentTypeError("format: WIDTHxHEIGHT, e.g. 480x320")
    return int(m.group(1)), int(m.group(2))


def parse_args(argv, env=os.environ):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("directory", nargs="?",
                    default=env.get("IMAGE_DIR", env.get("BILDER_DIR", DEFAULT_DIR)))
    ap.add_argument("--fb", default=env.get("FB_DEVICE", DEFAULT_FB))
    ap.add_argument("--hold", type=float, default=float(env.get("HOLD", 10)),
                    help="seconds per image (default 10)")
    ap.add_argument("--duration", type=float,
                    default=float(env.get("DURATION", 2.0)),
                    help="transition duration in seconds (default 2.0)")
    ap.add_argument("--easing", choices=list(EASINGS),
                    default=env.get("EASING", "ease-in-out"),
                    help="speed curve of the transition (default ease-in-out)")
    ap.add_argument("--shuffle", action="store_true", help="random order")
    ap.add_argument("--fill", action="store_true",
                    help="crop the image instead of black borders")
    ap.add_argument("--transitions", default=env.get("TRANSITIONS", "cut"),
                    help="comma-separated list, picked at random, never the same "
                         "twice in a row; default: cut. Available: %s"
                         % ",".join(TRANSITIONS))
    ap.add_argument("--invert", choices=["auto", "on", "off"], default="auto")
    ap.add_argument("--size", type=parse_size, help="override, e.g. 480x320")
    ap.add_argument("--bpp", type=int, help="override, e.g. 16")
    ap.add_argument("--gamma", type=parse_gamma, default=env.get("GAMMA", "1.0"),
                    help="tone curve in**gamma: one value or R,G,B "
                         "(e.g. 2.4,1.8,2.4); >1 = darker midtones")
    ap.add_argument("--limit", type=parse_limit, default=env.get("LIMIT", "255"),
                    help="highest raw value (1..255), one value or R,G,B; "
                         "e.g. 85 for a folded panel response curve")
    ap.add_argument("--black", type=parse_black, default=env.get("BLACK", "0"),
                    help="raw value for black, one value or R,G,B (e.g. 22,16,0): "
                         "compensates purple-tinted black")
    ap.add_argument("--dither", action="store_true",
                    help="ordered dithering before RGB565 (against banding)")
    ap.add_argument("--testcard", nargs="?", const="bars", default=None,
                    choices=["bars", "ramps"],
                    help="only show a test card (bars or ramps)")
    ap.add_argument("--seed", type=int,
                    default=int(env["SEED"]) if env.get("SEED") else None,
                    help="random seed (order with --shuffle, transition variations); "
                         "default: new each start, logged so a run can be repeated")
    ap.add_argument("--pile", action=argparse.BooleanOptionalAction,
                    default=None,
                    help="pictures as a pile of prints (default: on when "
                         "--transitions is only stack; --no-pile: off)")
    ap.add_argument("--pile-turn", type=float, metavar="DEG",
                    default=float(env.get("PILE_TURN", 0)),
                    help="pile: the print below lies turned by up to DEG "
                         "degrees, e.g. 4 (default 0: straight)")
    ap.add_argument("--clock", action=argparse.BooleanOptionalAction,
                    default=env.get("CLOCK", "on").lower() not in ("0", "off", "no"),
                    help="7-segment clock in the lower left corner, only while the "
                         "time is synchronized (default on; --no-clock: off)")
    ap.add_argument("--clock-hours", type=float,
                    default=float(env.get("CLOCK_HOURS", 48)),
                    help="keep the clock this many hours after the last time "
                         "sync (default 48)")
    ap.add_argument("--clock-sync-file", default=env.get("CLOCK_SYNC_FILE",
                                                         TIMESYNC_FILE),
                    help="file touched on every time sync (default: %s)"
                         % TIMESYNC_FILE)
    ap.add_argument("--diagonal", type=float,
                    default=float(env.get("DIAGONAL", DISPLAY_DIAGONAL_IN)),
                    help="display diagonal in inches, for the clock's size "
                         "(default %s)" % DISPLAY_DIAGONAL_IN)
    ap.add_argument("--once", action="store_true", help="only one round (testing)")
    ap.add_argument("--check", action="store_true",
                    help="only check the options, then exit (display untouched)")
    args = ap.parse_args(argv)
    names = [t.strip() for t in args.transitions.split(",") if t.strip()]
    bad = [t for t in names if t not in TRANSITIONS]
    if bad or not names:
        ap.error("Unknown transition: %s" % ",".join(bad or ["(empty)"]))
    args.transitions = names
    if args.pile is None:
        args.pile = names == ["stack"]
    if any(b >= li for b, li in zip(args.black, args.limit)):
        ap.error("--black must be less than --limit")
    if not 0 <= args.pile_turn <= 45:
        ap.error("--pile-turn must be 0..45 degrees")
    if args.clock_hours < 0:
        ap.error("--clock-hours must not be negative")
    if args.diagonal <= 0:
        ap.error("--diagonal must be positive")
    return args


def open_display(args):
    """(Display, clock overlay or None) from the options, or None when the
    framebuffer geometry cannot be read (logged)."""
    if args.size:
        bpp = args.bpp or 16
        info = FbInfo(args.size[0], args.size[1], bpp, args.size[0] * bpp // 8)
    else:
        try:
            info = read_fb_info(args.fb)
        except OSError as e:
            log(f"Cannot read framebuffer geometry of {args.fb} ({e}); "
                f"pass --size/--bpp. Hint: dmesg | grep -i fb_ili9486")
            return None
        if args.bpp:
            info = info._replace(bpp=args.bpp, stride=info.width * args.bpp // 8)
    invert = detect_invert() if args.invert == "auto" else args.invert == "on"

    overlay = None
    if args.clock and not args.testcard:
        try:
            overlay = ClockOverlay(
                (info.width, info.height),
                TimeSync(args.clock_sync_file, args.clock_hours * 3600),
                args.diagonal)
        except ValueError as e:
            log(f"no clock: {e}")
    display = Display(args.fb, info, invert, args.gamma, args.limit, args.dither,
                      args.black, overlay)
    return display, overlay


def exit_code(e):
    """Exit code of a SystemExit: 0 for a clean stop (SIGTERM, sys.exit()),
    otherwise the error code, so systemd (Restart=on-failure) restarts."""
    if e.code is None:
        return 0
    return e.code if isinstance(e.code, int) else 1


def install_signals():
    """SIGTERM exits cleanly, SIGUSR1 toggles pause. Returns the pause flag."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    paused = threading.Event()

    def toggle(*_):
        paused.clear() if paused.is_set() else paused.set()
        log("PAUSED" if paused.is_set() else "RESUMED")

    signal.signal(signal.SIGUSR1, toggle)
    return paused


def make_rng(args):
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    return random.Random(seed), seed


def describe(args, display, overlay, seed):
    info = display.info
    return (f"{args.directory} -> {args.fb} {info.width}x{info.height} "
            f"{info.bpp}bpp invert={display.invert} hold={args.hold}s "
            f"transitions={','.join(args.transitions)} duration={args.duration}s "
            f"easing={args.easing}{' pile' if args.pile else ''} seed={seed} "
            f"clock={'on' if overlay else 'off'}")


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.check:
        print("options ok")
        return 0
    opened = open_display(args)
    if opened is None:
        return 2
    display, overlay = opened
    paused = install_signals()
    rng, seed = make_rng(args)
    log("Slideshow " + describe(args, display, overlay, seed))
    try:
        if args.testcard:
            display.show(make_testcard(*display.size, args.testcard))
            while True:
                time.sleep(3600)
        slideshow(
            display,
            playlist(args.directory, args.shuffle, rng, loop=not args.once),
            args.hold, args.duration, args.fill, args.transitions, rng,
            paused=paused.is_set, easing=EASINGS[args.easing],
            refresh=display.refresh if overlay else None, pile=args.pile,
            pile_turn=args.pile_turn,
        )
    except KeyboardInterrupt:
        pass
    except SystemExit as e:  # SIGTERM -> 0 (stopped), anything else = error
        return exit_code(e)
    finally:
        display.blank()
        display.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
