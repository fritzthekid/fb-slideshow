#!/usr/bin/env python3
"""Renders a sequence config (config.yaml) into a movie, to check it offline
(issue #23).

The real show code plays the sequence against a simulated display and a
simulated clock, so steps, transitions, scenes, the pile and the clock behave
as on hydra. Each frame "costs" about as long as on hydra (the SPI display
is slow), so the movie is about as jerky as the panel; --smooth renders
every movie frame instead.

    python3 movie.py config.yaml                     # -> config.mp4
    python3 movie.py config.yaml -o show.mp4 --show-args "--shuffle --hold 5"

Paths on the stick (/mnt/stick/...) are taken from --stick (default: the
repo directory, i.e. bilder/ and background/ next to this script). One round
of the sequence is rendered (--rounds); a step that never ends is cut after
--forever-minutes. Needs ffmpeg.
"""
import argparse
import os
import pathlib
import random
import shlex
import shutil
import subprocess
import sys
import time

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sequence as seq  # noqa: E402

fbs, scene140 = seq.fbs, seq.scene140

STICK = "/mnt/stick"
SERVICE = HERE / "ci" / "fb-slideshow.service"
FULL_FRAME_S = 0.22   # a full frame on hydra (SPI + rendering)
ROWS_BASE_S = 0.05    # a partial update (small scene number): base cost


def default_args(service=SERVICE):
    """DEFAULT_ARGS of the service file, as the show on hydra starts with."""
    for line in pathlib.Path(service).read_text().splitlines():
        if line.startswith('Environment="DEFAULT_ARGS='):
            return shlex.split(line.split("=", 2)[2].rstrip('"'))
    return []


class SimClock:
    """Simulated time; every step forward writes the movie frames that fall
    into it, showing what is on the display."""

    def __init__(self, fps, emit):
        self.t = 0.0
        self.fps = fps
        self.emit = emit
        self.next_tick = 0.0
        self.frames = 0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.advance(seconds)

    def advance(self, dt):
        end = self.t + max(dt, 0.0)
        while self.next_tick < end:
            self.t = self.next_tick
            self.emit()
            self.frames += 1
            self.next_tick = self.frames / self.fps
        self.t = end


class MovieDisplay:
    """Stands in for fbs.Display: keeps the frame on the "panel" and lets the
    simulated time pass as hydra would need to send it."""

    def __init__(self, size, clock, overlay=None, smooth=False):
        self.size = size
        self.clock = clock
        self.overlay = overlay
        self.smooth = smooth
        w, h = size
        self.frame = np.zeros((h, w, 3), np.uint8)

    def _cost(self, rows):
        if self.smooth:
            return 1 / self.clock.fps
        h = self.size[1]
        return FULL_FRAME_S if rows >= h else ROWS_BASE_S + FULL_FRAME_S * rows / h

    def current(self):
        """The frame as it is on the panel now, with the clock."""
        if self.overlay is None:
            return self.frame
        return self.overlay.apply(self.frame, self.overlay.text())

    def show(self, frame):
        self.frame = frame
        self.clock.advance(self._cost(self.size[1]))

    def show_rows(self, frame, y0, y1):
        self.frame = frame
        self.clock.advance(self._cost(y1 - y0))

    def refresh(self):
        pass  # the clock is drawn into every movie frame

    def blank(self):
        self.frame = np.zeros_like(self.frame)


class Fixed:
    """The config as a ConfigFile that never changes."""

    def __init__(self, cfg):
        self.cfg = cfg

    def get(self):
        return self.cfg

    def changed(self):
        return False


class AlwaysSynced:
    def available(self):
        return True


def local_paths(cfg, stick):
    """/mnt/stick/... in the steps -> the same path below `stick`."""
    for step in cfg.steps:
        for name in ("directory", "background"):
            val = getattr(step.args, name, None)
            if isinstance(val, str) and (val == STICK or val.startswith(STICK + "/")):
                setattr(step.args, name, str(pathlib.Path(stick) / val[len(STICK) + 1:]))
    return cfg


def finite(cfg, forever_s, log):
    """Steps that never end get an end, so the movie does."""
    steps = []
    for step in cfg.steps:
        if step.seconds is None and step.kind == "slideshow":
            log(f"'{step.text}' cut after {forever_s / 60:g} min")
            step = step._replace(seconds=forever_s)
        elif step.kind == "scene" and step.args.repeat == 0:
            log(f"'{step.text}' played once")
            step.args.repeat = 1
        steps.append(step)
    return cfg._replace(steps=steps)


def ffmpeg_writer(path, size, fps):
    """A function taking RGB frames, writing them through ffmpeg; .close()
    finishes the file."""
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise SystemExit("ffmpeg not found (sudo apt install ffmpeg)")
    w, h = size
    cmd = [exe, "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    if str(path).lower().endswith(".gif"):
        cmd += [str(path)]
    else:
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                "-movflags", "+faststart", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def write(frame):
        proc.stdin.write(np.ascontiguousarray(frame, np.uint8).tobytes())

    def close():
        proc.stdin.close()
        if proc.wait():
            raise SystemExit(f"ffmpeg failed ({proc.returncode})")
    write.close = close
    return write


def render(config_text, write, base_argv=(), stick=HERE, fps=25, size=(480, 320),
           rounds=1, forever_s=60.0, seed=None, start_time=None, smooth=False,
           log=fbs.log):
    """Plays the config into write(frame). Returns (frames, seconds)."""
    cfg = seq.parse_config(config_text, list(base_argv))
    cfg = finite(local_paths(cfg, stick), forever_s, log)
    base = fbs.parse_args([*base_argv, *cfg.show_argv], env={})  # + show_args
    rng = random.Random(seed)
    clock = None

    def emit():
        write(display.current())
    clock = SimClock(fps, emit)
    t0 = time.time() if start_time is None else start_time
    overlay = None
    if base.clock:
        overlay = fbs.ClockOverlay(size, AlwaysSynced(), base.diagonal,
                                   now=lambda: t0 + clock.t)
    display = MovieDisplay(size, clock, overlay, smooth)
    seq.run(display, Fixed(cfg), overlay, rng, clock=clock, sleep=clock.sleep,
            rounds=rounds, once=False, log=log)
    clock.advance(1.0)  # the last picture a moment longer
    return clock.frames, clock.t


def parse_start(text):
    """'HH:MM' today -> epoch seconds."""
    try:
        hh, mm = (int(x) for x in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError("format: HH:MM")
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0, 0, 0, -1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("config", help="sequence config, e.g. config.yaml")
    ap.add_argument("-o", "--output", help="movie file (.mp4 or .gif; "
                                           "default: the config's name .mp4)")
    ap.add_argument("--show-args", default=os.environ.get("SHOW_ARGS", ""),
                    help="options as set with 'make config' on hydra, e.g. "
                         "\"--shuffle --hold 5\" (added to the service defaults)")
    ap.add_argument("--stick", default=str(HERE),
                    help="local copy of /mnt/stick (default: the repo directory)")
    ap.add_argument("--fps", type=float, default=25, help="movie frame rate (default 25)")
    ap.add_argument("--rounds", type=int, default=1, help="rounds of the sequence")
    ap.add_argument("--forever-minutes", type=float, default=1.0,
                    help="length of a step that never ends (default 1)")
    ap.add_argument("--seed", type=int, help="random seed (order, transitions)")
    ap.add_argument("--start", type=parse_start, metavar="HH:MM",
                    help="time on the clock at the start (default: now)")
    ap.add_argument("--smooth", action="store_true",
                    help="every movie frame rendered, not as jerky as on hydra")
    a = ap.parse_args(argv)
    if a.fps <= 0 or a.rounds < 1 or a.forever_minutes <= 0:
        ap.error("--fps, --rounds and --forever-minutes must be positive")
    out = a.output or str(pathlib.Path(a.config).with_suffix(".mp4"))
    base_argv = default_args() + shlex.split(a.show_args)
    try:
        text = pathlib.Path(a.config).read_text(encoding="utf-8")
        write = ffmpeg_writer(out, (480, 320), a.fps)
        t = time.monotonic()
        frames, seconds = render(text, write, base_argv, a.stick, a.fps,
                                 rounds=a.rounds, forever_s=a.forever_minutes * 60,
                                 seed=a.seed, start_time=a.start, smooth=a.smooth)
        write.close()
    except (OSError, seq.ConfigError) as e:
        print(f"{a.config}: {e}", file=sys.stderr)
        return 2
    print(f"{out}: {seconds:.0f} s, {frames} frames "
          f"(rendered in {time.monotonic() - t:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
