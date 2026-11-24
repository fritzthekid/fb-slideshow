import importlib.util
import itertools
import pathlib
import os
import random
import re
import sys
import time
import math

import numpy as np
import pytest
from PIL import Image

_spec = importlib.util.spec_from_file_location(
    "fbs", pathlib.Path(__file__).with_name("fb-slideshow.py"))
fbs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fbs)

W, H = 48, 32


def mk_jpg(path, color, size=(60, 40)):
    Image.new("RGB", size, color).save(path, "JPEG", quality=95)


def noise(seed):
    return np.random.default_rng(seed).integers(0, 256, (H, W, 3), dtype=np.uint8)


class FakeDisplay:
    size = (W, H)

    def __init__(self):
        self.frames = []

    def show(self, frame):
        self.frames.append(frame.copy())


# ---------------------------------------------------------------- image list
def test_list_images_natural_order_and_filter(tmp_path):
    for n in ["a_10.jpg", "a_2.jpg", "b.JPEG", "c.png", ".hidden.jpg", "d.txt"]:
        (tmp_path / n).write_bytes(b"x")
    got = [pathlib.Path(p).name for p in fbs.list_images(str(tmp_path))]
    assert got == ["a_2.jpg", "a_10.jpg", "b.JPEG"]


def test_list_images_missing_dir():
    assert fbs.list_images("/nonexistent/stick") == []


def test_playlist_loops_in_order(tmp_path):
    for n in "abc":
        (tmp_path / f"{n}.jpg").write_bytes(b"x")
    got = [pathlib.Path(p).name for p in itertools.islice(
        fbs.playlist(str(tmp_path), False, random.Random(0)), 7)]
    assert got == ["a.jpg", "b.jpg", "c.jpg", "a.jpg", "b.jpg", "c.jpg", "a.jpg"]


def test_playlist_rescans_each_round(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    gen = fbs.playlist(str(tmp_path), False, random.Random(0))
    assert [pathlib.Path(next(gen)).name for _ in range(2)] == ["a.jpg", "b.jpg"]
    (tmp_path / "c.jpg").write_bytes(b"x")
    assert [pathlib.Path(next(gen)).name for _ in range(3)] == ["a.jpg", "b.jpg", "c.jpg"]


def test_playlist_shuffle_is_permutation_per_round(tmp_path):
    names = [f"{i}.jpg" for i in range(6)]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    gen = fbs.playlist(str(tmp_path), True, random.Random(1))
    for _ in range(3):
        rnd = [pathlib.Path(next(gen)).name for _ in range(6)]
        assert sorted(rnd) == sorted(names)


def test_playlist_empty_waits_and_retries(tmp_path):
    calls = []

    def sleep(s):
        calls.append(s)
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        next(fbs.playlist(str(tmp_path), False, random.Random(0),
                          empty_wait=3, sleep=sleep, log=lambda m: None))
    assert calls == [3]


def test_playlist_once_empty_ends(tmp_path):
    assert list(fbs.playlist(str(tmp_path), False, random.Random(0), loop=False)) == []


def test_playlist_once_single_round(tmp_path):
    for n in "ab":
        (tmp_path / f"{n}.jpg").write_bytes(b"x")
    assert len(list(fbs.playlist(str(tmp_path), False, random.Random(0), loop=False))) == 2


# ------------------------------------------------- framebuffer / encoding
def test_encode_rgb565_exact():
    frame = np.array([[[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]]], np.uint8)
    out = fbs.encode_frame(frame, fbs.FbInfo(4, 1, 16, 8), False)
    assert out == b"\x00\xf8" b"\xe0\x07" b"\x1f\x00" b"\xff\xff"


def test_encode_invert_black_becomes_white():
    frame = np.zeros((2, 2, 3), np.uint8)
    out = fbs.encode_frame(frame, fbs.FbInfo(2, 2, 16, 4), True)
    assert out == b"\xff" * 8


def test_encode_32bpp_bgra():
    frame = np.array([[[10, 20, 30]]], np.uint8)
    assert fbs.encode_frame(frame, fbs.FbInfo(1, 1, 32, 4), False) == bytes([30, 20, 10, 255])


def test_encode_respects_stride_padding():
    frame = np.full((2, 2, 3), 255, np.uint8)
    out = fbs.encode_frame(frame, fbs.FbInfo(2, 2, 16, 8), False)
    assert len(out) == 16
    assert out[:4] == b"\xff" * 4 and out[4:8] == b"\x00" * 4
    assert out[8:12] == b"\xff" * 4 and out[12:] == b"\x00" * 4


def test_encode_unsupported_bpp():
    with pytest.raises(ValueError):
        fbs.encode_frame(np.zeros((1, 1, 3), np.uint8), fbs.FbInfo(1, 1, 24, 3), False)


def test_detect_invert(tmp_path):
    (tmp_path / "waveshare35a@0").mkdir()
    assert fbs.detect_invert(str(tmp_path)) is True
    other = tmp_path / "x"
    other.mkdir()
    (other / "tft35a@0").mkdir()
    assert fbs.detect_invert(str(other)) is False
    empty = tmp_path / "y"
    empty.mkdir()
    assert fbs.detect_invert(str(empty)) is False
    assert fbs.detect_invert(str(tmp_path / "missing")) is False


def test_read_fb_info_from_sysfs(tmp_path):
    d = tmp_path / "fb1"
    d.mkdir()
    (d / "virtual_size").write_text("480,320\n")
    (d / "bits_per_pixel").write_text("16\n")
    (d / "stride").write_text("960\n")
    assert fbs.read_fb_info("/dev/fb1", str(tmp_path)) == fbs.FbInfo(480, 320, 16, 960)
    (d / "stride").unlink()
    assert fbs.read_fb_info("/dev/fb1", str(tmp_path)).stride == 960


def test_display_writes_frame_to_file(tmp_path):
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    info = fbs.FbInfo(4, 2, 16, 8)
    d = fbs.Display(str(fb), info, invert=False)
    frame = np.zeros((2, 4, 3), np.uint8)
    frame[0, 0] = (255, 0, 0)
    d.show(frame)
    assert fb.read_bytes() == fbs.encode_frame(frame, info, False)
    assert fb.stat().st_size == 16
    d.close()


def test_display_blank_respects_invert(tmp_path):
    for invert, byte in ((False, b"\x00"), (True, b"\xff")):
        fb = tmp_path / f"fb{invert}"
        fb.write_bytes(b"")
        d = fbs.Display(str(fb), fbs.FbInfo(4, 2, 16, 8), invert)
        d.show(np.full((2, 4, 3), 123, np.uint8))
        d.blank()
        assert fb.read_bytes() == byte * 16
        d.close()


# --------------------------------------------------------------- image loading
def test_load_image_contain_pillarbox(tmp_path):
    p = tmp_path / "p.jpg"
    mk_jpg(p, (255, 0, 0), size=(40, 60))
    a = fbs.load_image(str(p), (W, H))
    assert a.shape == (H, W, 3) and a.dtype == np.uint8
    assert a[0, 0].tolist() == [0, 0, 0]
    assert a[H // 2, W // 2][0] > 200 and a[H // 2, W // 2][1] < 60


def test_load_image_fill_covers_everything(tmp_path):
    p = tmp_path / "p.jpg"
    mk_jpg(p, (255, 0, 0), size=(40, 60))
    a = fbs.load_image(str(p), (W, H), fill=True)
    assert a.shape == (H, W, 3)
    assert (a[..., 0] > 200).all()


def test_load_image_applies_exif_rotation(tmp_path):
    p = tmp_path / "r.jpg"
    im = Image.new("RGB", (60, 40), (255, 0, 0))
    ex = im.getexif()
    ex[0x0112] = 6  # 90 degrees -> portrait
    im.save(p, "JPEG", exif=ex)
    a = fbs.load_image(str(p), (W, H))
    assert a[0, 0].tolist() == [0, 0, 0]  # pillarbox => was rotated


# ---------------------------------------------------------------- transitions
@pytest.mark.parametrize("name", sorted(fbs.TRANSITIONS))
def test_transition_endpoints_exact_for_many_random_variants(name):
    a, b = noise(1), noise(2)
    for seed in range(12):
        t = fbs.make_transition(name, random.Random(seed), (W, H))
        assert np.array_equal(t(a, b, 0.0), a)
        assert np.array_equal(t(a, b, 1.0), b)
        for p in (0.1, 0.5, 0.9):
            out = t(a, b, p)
            assert out.shape == (H, W, 3) and out.dtype == np.uint8


MOVING = sorted(set(fbs.TRANSITIONS) - {"cut"})  # all except the hard cut


@pytest.mark.parametrize("name", MOVING)
def test_transition_progresses_monotonically(name):
    a, b = np.zeros((H, W, 3), np.uint8), np.full((H, W, 3), 255, np.uint8)
    for seed in range(8):
        t = fbs.make_transition(name, random.Random(seed), (W, H))
        means = [float(t(a, b, p).mean()) for p in (0.1, 0.3, 0.5, 0.7, 0.9)]
        assert means == sorted(means), (name, seed, means)
        assert 0 < means[2] < 255
        assert not np.array_equal(t(a, b, 0.5), a)
        assert not np.array_equal(t(a, b, 0.5), b)


@pytest.mark.parametrize("name", sorted(fbs.TRANSITIONS))
def test_transition_deterministic_per_seed(name):
    a, b = noise(1), noise(2)
    t1 = fbs.make_transition(name, random.Random(5), (W, H))
    t2 = fbs.make_transition(name, random.Random(5), (W, H))
    assert np.array_equal(t1(a, b, 0.5), t2(a, b, 0.5))


@pytest.mark.parametrize("name", ["wipe", "push", "stack", "iris", "dissolve", "blinds", "split", "zoom"])
def test_transition_varies_between_runs(name):
    a, b = noise(1), noise(2)
    outs = {fbs.make_transition(name, random.Random(s), (W, H))(a, b, 0.5).tobytes()
            for s in range(30)}
    assert len(outs) > 1


def test_stack_next_picture_waits_below():
    """The revealed part of b is b itself, in place, and only ever grows."""
    a, b = np.zeros((H, W, 3), np.uint8), noise(2)
    for seed in range(16):
        t = fbs.make_transition("stack", random.Random(seed), (W, H))
        prev = np.zeros((H, W), bool)
        for p in (0.1, 0.3, 0.5, 0.7, 0.9):
            exact_b = (t(a, b, p) == b).all(axis=2) & b.any(axis=2)
            assert (exact_b | ~prev).all(), (seed, p)  # never covered again
            prev = exact_b
        assert prev.mean() > 0.5  # most of b visible near the end


def test_stack_current_picture_slides_unchanged():
    """The visible part of a is an exact copy of a at stack_position(p): only
    the offset of the top picture changes (no scaling, no blending)."""
    a, b = noise(1), np.zeros((H, W, 3), np.uint8)
    for seed in range(20):
        params = fbs.stack_params(random.Random(seed))
        t = fbs.make_stack(random.Random(seed), W, H)
        for p in (0.2, 0.5, 0.8):
            out = t(a, b, p)
            fx, fy = fbs.stack_position(p, params, (W, H))
            sx, sy = int(round(fx)), int(round(fy))
            x0, x1, y0, y1 = max(sx, 0), min(sx + W, W), max(sy, 0), min(sy + H, H)
            assert np.array_equal(out[y0:y1, x0:x1], a[y0 - sy:y1 - sy, x0 - sx:x1 - sx])


def test_stack_shadow_darkens_only_near_the_edge():
    a, b = np.zeros((H, W, 3), np.uint8), np.full((H, W, 3), 200, np.uint8)
    out = fbs.make_stack(random.Random(0), W, H)(a, b, 0.5)
    revealed = out.any(axis=2)
    assert (out[revealed] <= 200).all()
    assert (out[revealed] < 200).any() and (out[revealed] == 200).any()


def test_make_transition_unknown():
    with pytest.raises(ValueError):
        fbs.make_transition("nope", random.Random(0), (W, H))


def test_run_transition_time_based_ends_on_b():
    class Clock:
        t = -0.1

        def __call__(self):
            self.t += 0.1
            return self.t

    seen = []

    def spy(a, b, p):
        seen.append(p)
        return a

    a, b = noise(1), noise(2)
    d = FakeDisplay()
    fbs.run_transition(d, spy, a, b, 1.0, clock=Clock())
    assert len(seen) >= 5
    assert seen == sorted(seen) and all(0 <= p <= 1 for p in seen)
    assert np.array_equal(d.frames[-1], b)
    assert len(d.frames) == len(seen) + 1


def test_run_transition_zero_duration_shows_only_b():
    a, b = noise(1), noise(2)
    d = FakeDisplay()
    fbs.run_transition(d, lambda *_: a, a, b, 0)
    assert len(d.frames) == 1 and np.array_equal(d.frames[0], b)


# ------------------------------------------------------------ main loop
def run_show(paths, **kw):
    d = FakeDisplay()
    sleeps = []
    fbs.slideshow(d, paths, kw.get("hold", 7), kw.get("duration", 0), False,
                  kw.get("transitions", ["fade"]), random.Random(0),
                  sleep=sleeps.append)
    return d, sleeps


def test_slideshow_shows_images_in_order_and_holds(tmp_path):
    cols = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    for i, c in enumerate(cols, 1):
        mk_jpg(tmp_path / f"{i}.jpg", c, size=(W, H))
    d, sleeps = run_show(fbs.list_images(str(tmp_path)))
    assert sleeps == [7, 7, 7]
    assert len(d.frames) == 3
    for f, ch in zip(d.frames, range(3)):
        assert f[..., ch].mean() > 200 and f[..., (ch + 1) % 3].mean() < 60


def test_slideshow_first_transition_starts_from_black(tmp_path):
    mk_jpg(tmp_path / "1.jpg", (255, 0, 0), size=(W, H))
    seen = []
    orig = fbs.make_transition

    def spy(name, rng, size):
        fn = orig(name, rng, size)

        def run(a, b, p):
            seen.append(a.copy())
            return fn(a, b, p)
        return run

    fbs.make_transition, saved = spy, fbs.make_transition
    try:
        d = FakeDisplay()
        clock = iter([0.0, 0.5, 2.0]).__next__
        fbs.slideshow(d, fbs.list_images(str(tmp_path)), 1, 1.0, False,
                      ["fade"], random.Random(0), sleep=lambda s: None, clock=clock)
    finally:
        fbs.make_transition = saved
    assert seen and not seen[0].any()


def test_slideshow_skips_broken_image(tmp_path):
    mk_jpg(tmp_path / "1.jpg", (255, 0, 0), size=(W, H))
    (tmp_path / "2.jpg").write_bytes(b"not a jpeg")
    mk_jpg(tmp_path / "3.jpg", (0, 0, 255), size=(W, H))
    d, sleeps = run_show(fbs.list_images(str(tmp_path)))
    assert len(d.frames) == 2 and sleeps == [7, 7]


def test_slideshow_never_same_transition_twice_in_a_row(tmp_path):
    for i in range(10):
        mk_jpg(tmp_path / f"{i}.jpg", (i * 20, 0, 0), size=(W, H))
    names = []
    orig = fbs.make_transition

    def spy(name, rng, size):
        names.append(name)
        return orig(name, rng, size)

    fbs.make_transition, saved = spy, fbs.make_transition
    try:
        run_show(fbs.list_images(str(tmp_path)), transitions=["fade", "wipe", "iris"])
    finally:
        fbs.make_transition = saved
    assert len(names) == 10
    assert all(x != y for x, y in zip(names, names[1:]))


def test_slideshow_single_image_loops_without_retransition(tmp_path):
    p = tmp_path / "1.jpg"
    mk_jpg(p, (255, 0, 0), size=(W, H))
    d, sleeps = run_show([str(p)] * 3)
    assert len(d.frames) == 1 and sleeps == [7, 7, 7]


# ------------------------------------------------------------------- CLI
def test_parse_args_defaults():
    a = fbs.parse_args([], env={})
    assert a.directory == "/mnt/stick/bilder" and a.fb == "/dev/fb1"
    assert a.hold == 10 and a.duration == 2.0 and a.invert == "auto"
    assert a.transitions == ["cut"] and not a.shuffle and not a.fill
    assert a.easing == "ease-in-out" and a.seed is None


def test_parse_args_env_and_flags():
    a = fbs.parse_args(["--shuffle", "--fill", "--transitions", "fade, iris",
                        "--invert", "on", "--size", "480x320", "--bpp", "16"],
                       env={"FB_DEVICE": "/dev/fb0", "HOLD": "3"})
    assert a.fb == "/dev/fb0" and a.hold == 3
    assert a.transitions == ["fade", "iris"] and a.shuffle and a.fill
    assert a.invert == "on" and a.size == (480, 320) and a.bpp == 16


def test_parse_args_rejects_unknown_transition():
    with pytest.raises(SystemExit):
        fbs.parse_args(["--transitions", "fade,bogus"], env={})


def test_main_once_end_to_end_blanks_on_exit(tmp_path):
    for i in (1, 2):
        mk_jpg(tmp_path / f"{i}.jpg", (255, 255, 255), size=(W, H))
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    rc = fbs.main([str(tmp_path), "--fb", str(fb), "--size", f"{W}x{H}", "--bpp", "16",
                   "--invert", "off", "--hold", "0", "--duration", "0.05", "--once"])
    assert rc == 0
    assert fb.stat().st_size == W * H * 2
    assert fb.read_bytes() == b"\x00" * (W * H * 2)


def test_main_missing_geometry_returns_2(tmp_path):
    assert fbs.main([str(tmp_path), "--fb", "/dev/doesnotexist9"]) == 2


def test_main_empty_dir_once_returns_0(tmp_path):
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    assert fbs.main([str(tmp_path / "empty"), "--fb", str(fb), "--size", "48x32",
                     "--invert", "off", "--once"]) == 0


# ------------------------------------------------------------ pause / test card
def test_slideshow_pause_holds_until_resumed(tmp_path):
    for i in (1, 2, 3):
        mk_jpg(tmp_path / f"{i}.jpg", (255, 0, 0), size=(W, H))
    answers = iter([True, True])
    d = FakeDisplay()
    sleeps = []
    fbs.slideshow(d, fbs.list_images(str(tmp_path)), 7, 0, False, ["fade"],
                  random.Random(0), sleep=sleeps.append,
                  paused=lambda: next(answers, False))
    assert sleeps == [7, 0.2, 0.2, 7, 7]
    assert len(d.frames) == 3


def test_slideshow_pause_shows_no_new_image_while_paused(tmp_path):
    for i in (1, 2):
        mk_jpg(tmp_path / f"{i}.jpg", (255, 0, 0), size=(W, H))
    d = FakeDisplay()
    counts = []

    def paused():
        counts.append(len(d.frames))
        return len(counts) < 4

    fbs.slideshow(d, fbs.list_images(str(tmp_path)), 1, 0, False, ["fade"],
                  random.Random(0), sleep=lambda s: None, paused=paused)
    assert counts[:3] == [1, 1, 1]  # while paused: still only image 1


def test_testcard_bars_and_ramp():
    t = fbs.make_testcard(80, 40)
    assert t.shape == (40, 80, 3) and t.dtype == np.uint8
    assert t[0, 5].tolist() == [255, 255, 255]
    assert t[0, 25].tolist() == [0, 255, 255]
    assert t[0, 45].tolist() == [255, 0, 255]
    assert t[0, 55].tolist() == [255, 0, 0]
    assert t[0, 75].tolist() == [0, 0, 0]
    ramp = t[30, :, 0]
    assert ramp[0] == 0 and ramp[-1] == 255 and (np.diff(ramp.astype(int)) >= 0).all()
    assert len(set(ramp.tolist())) == 16
    assert (t[30, :, 0] == t[30, :, 1]).all() and (t[30, :, 1] == t[30, :, 2]).all()


# ------------------------------------------------------------------ Gamma
def test_gamma_identity_returns_same_frame():
    f = noise(1)
    assert fbs.apply_gamma(f, 1.0) is f


def test_gamma_values_endpoints_and_monotonic():
    ramp = np.arange(256, dtype=np.uint8).reshape(1, 256, 1).repeat(3, axis=2)
    out = fbs.apply_gamma(ramp, 2.0)[0, :, 0]
    assert out[0] == 0 and out[255] == 255 and out[128] == 64
    assert (np.diff(out.astype(int)) >= 0).all()
    assert fbs.apply_gamma(ramp, 0.5)[0, 128, 0] == 181  # <1 brightens


def test_display_applies_gamma_before_encoding(tmp_path):
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    info = fbs.FbInfo(2, 1, 16, 4)
    d = fbs.Display(str(fb), info, invert=False, gamma=2.0)
    frame = np.full((1, 2, 3), 128, np.uint8)
    d.show(frame)
    assert fb.read_bytes() == fbs.encode_frame(np.full((1, 2, 3), 64, np.uint8), info, False)
    d.close()


def test_parse_args_gamma():
    assert fbs.parse_args([], env={}).gamma == (1.0, 1.0, 1.0)
    assert fbs.parse_args(["--gamma", "2.2"], env={}).gamma == (2.2,) * 3
    assert fbs.parse_args([], env={"GAMMA": "1.8"}).gamma == (1.8,) * 3
    assert fbs.parse_args(["--gamma", "2.4,1.8,2.4"], env={}).gamma == (2.4, 1.8, 2.4)
    with pytest.raises(SystemExit):
        fbs.parse_args(["--gamma", "0"], env={})


def test_parse_args_gamma_rejects_bad_values():
    for bad in ("1,2", "a", "1,2,3,4", "-1", "2,0,2"):
        with pytest.raises(SystemExit):
            fbs.parse_args(["--gamma", bad], env={})


def test_gamma_per_channel():
    f = np.full((1, 1, 3), 128, np.uint8)
    out = fbs.apply_gamma(f, (2.0, 1.0, 0.5))[0, 0].tolist()
    assert out == [64, 128, 181]


def test_display_applies_per_channel_gamma(tmp_path):
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    info = fbs.FbInfo(1, 1, 32, 4)
    d = fbs.Display(str(fb), info, invert=False, gamma=(2.0, 1.0, 0.5))
    d.show(np.full((1, 1, 3), 128, np.uint8))
    assert fb.read_bytes() == bytes([181, 128, 64, 255])  # BGRA
    d.close()


def test_testcard_ramps_per_channel_rows():
    t = fbs.make_testcard(80, 40, "ramps")
    assert t.shape == (40, 80, 3)
    gray, red, green, blue = t[5], t[15], t[25], t[35]
    assert (gray[:, 0] == gray[:, 1]).all() and (gray[:, 1] == gray[:, 2]).all()
    for row, ch in ((red, 0), (green, 1), (blue, 2)):
        others = [c for c in range(3) if c != ch]
        assert (row[:, others] == 0).all()
        assert row[0, ch] == 0 and row[-1, ch] == 255
        assert len(set(row[:, ch].tolist())) == 16
    assert len(set(gray[:, 0].tolist())) == 16


def test_parse_args_testcard_modes():
    assert fbs.parse_args([], env={}).testcard is None
    assert fbs.parse_args(["--testcard"], env={}).testcard == "bars"
    assert fbs.parse_args(["--testcard", "ramps"], env={}).testcard == "ramps"
    assert fbs.parse_args(["--testcard", "--gamma", "2"], env={}).testcard == "bars"


# ------------------------------------------------------- limit / dithering
def _ramp():
    return np.arange(256, dtype=np.uint8).reshape(1, 256, 1).repeat(3, axis=2)


def test_limit_compresses_range_monotonically():
    out = fbs.apply_gamma(_ramp(), 1.0, 85)[0, :, 0]
    assert out[0] == 0 and out[255] == 85 and out[128] == 43
    assert (np.diff(out.astype(int)) >= 0).all()


def test_limit_and_gamma_per_channel():
    f = np.full((1, 1, 3), 255, np.uint8)
    assert fbs.apply_gamma(f, 1.0, (85, 100, 120))[0, 0].tolist() == [85, 100, 120]
    f = np.full((1, 1, 3), 128, np.uint8)
    assert fbs.apply_gamma(f, 2.0, 100)[0, 0].tolist() == [25, 25, 25]


def test_parse_limit():
    assert fbs.parse_args([], env={}).limit == (255.0,) * 3
    assert fbs.parse_args(["--limit", "85"], env={}).limit == (85.0,) * 3
    assert fbs.parse_args([], env={"LIMIT": "90"}).limit == (90.0,) * 3
    assert fbs.parse_args(["--limit", "80,85,90"], env={}).limit == (80.0, 85.0, 90.0)
    for bad in ("0", "256", "a", "1,2", "-5"):
        with pytest.raises(SystemExit):
            fbs.parse_args(["--limit", bad], env={})


def test_dither_flag_default_off():
    assert fbs.parse_args([], env={}).dither is False
    assert fbs.parse_args(["--dither"], env={}).dither is True


def _decode565(raw, w, h):
    px = np.frombuffer(raw, "<u2").reshape(h, w).astype(np.int32)
    return (px >> 11) & 31, (px >> 5) & 63, px & 31


def test_dither_preserves_mean_intensity_and_extremes():
    info = fbs.FbInfo(16, 16, 16, 32)
    flat = np.full((16, 16, 3), 100, np.uint8)
    r, g, b = _decode565(fbs.encode_frame(flat, info, False, dither=True), 16, 16)
    assert abs(r.mean() * 8 - 100) <= 0.5 and abs(b.mean() * 8 - 100) <= 0.5
    assert abs(g.mean() * 4 - 100) <= 0.25
    assert len(set(r.flatten().tolist())) == 2  # two adjacent levels
    for v, rmax, gmax in ((0, 0, 0), (255, 31, 63)):
        f = np.full((16, 16, 3), v, np.uint8)
        r, g, b = _decode565(fbs.encode_frame(f, info, False, dither=True), 16, 16)
        assert (r == rmax).all() and (g == gmax).all() and (b == rmax).all()


def test_dither_off_equals_plain_truncation_and_32bpp_untouched():
    info16 = fbs.FbInfo(8, 8, 16, 16)
    f = np.full((8, 8, 3), 100, np.uint8)
    assert fbs.encode_frame(f, info16, False) == fbs.encode_frame(f, info16, False, False)
    r, _, _ = _decode565(fbs.encode_frame(f, info16, False), 8, 8)
    assert (r == 100 >> 3).all()
    info32 = fbs.FbInfo(8, 8, 32, 32)
    assert fbs.encode_frame(f, info32, False, True) == fbs.encode_frame(f, info32, False, False)


def test_dither_is_spatially_varying_and_deterministic():
    f = np.full((8, 8, 3), 100, np.uint8)
    d1, d2 = fbs.dither565(f), fbs.dither565(f)
    assert np.array_equal(d1, d2) and len(np.unique(d1[..., 0] >> 3)) == 2
    assert d1.shape == f.shape and d1.dtype == np.uint8


def test_display_applies_limit_then_dither(tmp_path):
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    info = fbs.FbInfo(4, 4, 16, 8)
    d = fbs.Display(str(fb), info, invert=False, gamma=1.0, limit=85, dither=True)
    d.show(np.full((4, 4, 3), 255, np.uint8))
    expect = fbs.encode_frame(np.full((4, 4, 3), 85, np.uint8), info, False, True)
    assert fb.read_bytes() == expect
    d.close()


# ------------------------------------------------------------- black level
def test_black_offset_endpoints_and_monotonic():
    out = fbs.apply_gamma(_ramp(), 1.0, 85, 20)[0, :, 0]
    assert out[0] == 20 and out[255] == 85
    assert (np.diff(out.astype(int)) >= 0).all()
    assert out[128] == round(20 + 65 * 128 / 255)


def test_black_per_channel_lifts_weak_channels():
    f = np.zeros((1, 1, 3), np.uint8)
    assert fbs.apply_gamma(f, 1.0, 85, (22, 16, 0))[0, 0].tolist() == [22, 16, 0]
    f = np.full((1, 1, 3), 255, np.uint8)
    assert fbs.apply_gamma(f, 1.0, 85, (22, 16, 0))[0, 0].tolist() == [85, 85, 85]


def test_black_default_is_identity():
    f = noise(3)
    assert fbs.apply_gamma(f, 1.0, 255.0, 0.0) is f


def test_parse_black():
    assert fbs.parse_args([], env={}).black == (0.0,) * 3
    assert fbs.parse_args(["--black", "10"], env={}).black == (10.0,) * 3
    assert fbs.parse_args(["--black", "22,16,0", "--limit", "85"], env={}).black == (22.0, 16.0, 0.0)
    assert fbs.parse_args([], env={"BLACK": "5"}).black == (5.0,) * 3
    for bad in ("-1", "255", "a", "1,2"):
        with pytest.raises(SystemExit):
            fbs.parse_args(["--black", bad], env={})


def test_parse_black_must_stay_below_limit():
    with pytest.raises(SystemExit):
        fbs.parse_args(["--limit", "85", "--black", "85"], env={})
    with pytest.raises(SystemExit):
        fbs.parse_args(["--limit", "85,85,40", "--black", "0,0,50"], env={})
    assert fbs.parse_args(["--limit", "85", "--black", "84"], env={}).black == (84.0,) * 3


def test_display_applies_black_offset(tmp_path):
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    info = fbs.FbInfo(1, 1, 32, 4)
    d = fbs.Display(str(fb), info, invert=False, limit=85, black=(22, 16, 0))
    d.show(np.zeros((1, 1, 3), np.uint8))
    assert fb.read_bytes() == bytes([0, 16, 22, 255])  # BGRA
    d.close()


def test_blank_uses_raw_black_ignoring_corrections(tmp_path):
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    info = fbs.FbInfo(4, 2, 16, 8)
    d = fbs.Display(str(fb), info, invert=False, gamma=2.0, limit=85,
                    black=(0, 4, 0), dither=True)
    d.show(np.zeros((2, 4, 3), np.uint8))
    assert fb.read_bytes() != b"\x00" * 16  # correction lifts black (G=4)
    d.blank()
    assert fb.read_bytes() == b"\x00" * 16


def test_blank_with_invert_is_raw_white_bytes(tmp_path):
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    d = fbs.Display(str(fb), fbs.FbInfo(4, 2, 16, 8), invert=True,
                    limit=85, black=(0, 4, 0), dither=True)
    d.blank()
    assert fb.read_bytes() == b"\xff" * 16


# ------------------------------------------------- stack (issue #4 criteria)
@pytest.mark.parametrize("d", fbs.STACK_DIRECTIONS)
def test_stack_position_geometry_per_direction(d):
    """t = 0: at rest; t = 0.5: half way out; t = 1: completely off-screen."""
    for drift in (-fbs.STACK_MAX_DRIFT, 0.0, fbs.STACK_MAX_DRIFT):
        params = fbs.StackParams(d[0], d[1], drift)
        assert fbs.stack_position(0, params, (W, H)) == (0, 0)
        x, y = fbs.stack_position(0.5, params, (W, H))
        x1, y1 = fbs.stack_position(1, params, (W, H))
        if d[0]:
            assert x == d[0] * W / 2 and x1 == d[0] * W  # fully left/right of the display
            assert abs(y1) <= fbs.STACK_MAX_DRIFT * H + 1e-9
        else:
            assert y == d[1] * H / 2 and y1 == d[1] * H  # fully above/below
            assert abs(x1) <= fbs.STACK_MAX_DRIFT * W + 1e-9


def test_stack_position_moves_continuously():
    params = fbs.StackParams(-1, 0, 0.05)
    xs = [fbs.stack_position(i / 100, params, (W, H))[0] for i in range(101)]
    steps = np.diff(xs)
    assert (steps < 0).all() and np.allclose(steps, steps[0])  # no jumps


def test_stack_params_reproducible_and_varied():
    assert [fbs.stack_params(random.Random(s)) for s in range(10)] == \
           [fbs.stack_params(random.Random(s)) for s in range(10)]
    many = [fbs.stack_params(random.Random(s)) for s in range(200)]
    assert {(p.dx, p.dy) for p in many} == set(fbs.STACK_DIRECTIONS)  # only 4 sides
    assert all(abs(p.drift) <= fbs.STACK_MAX_DRIFT for p in many)
    assert len({round(p.drift, 6) for p in many}) > 100


@pytest.mark.parametrize("name", fbs.EASINGS)
def test_easing_endpoints_and_monotonic(name):
    e = fbs.EASINGS[name]
    vals = [e(i / 50) for i in range(51)]
    assert vals[0] == 0 and vals[-1] == 1
    assert all(x <= y for x, y in zip(vals, vals[1:]))


def test_easing_shapes():
    e = fbs.EASINGS
    assert e["ease-out"](0.2) > e["linear"](0.2) > e["ease-in-out"](0.2)
    assert e["linear"](0.5) == e["ease-in-out"](0.5) == 0.5


def test_run_transition_uses_easing():
    seen = []

    def spy(a, b, p):
        seen.append(p)
        return a

    clock = iter([0.0, 0.5, 2.5]).__next__  # t0, one frame at 25%, then done
    fbs.run_transition(FakeDisplay(), spy, noise(1), noise(2), 2.0, clock=clock,
                       easing=fbs.EASINGS["ease-out"])
    assert seen == [pytest.approx(1 - 0.75 ** 2)]


def test_run_transition_slow_frames_do_not_stretch_duration():
    """Every frame takes 0.7 s: frames are dropped, the total stays 2 s."""
    t = [-0.7]

    def clock():
        t[0] += 0.7
        return t[0]

    seen = []
    d = FakeDisplay()
    fbs.run_transition(d, lambda a, b, p: seen.append(p) or a, noise(1), noise(2),
                       2.0, clock=clock, easing=fbs.EASINGS["linear"])
    assert seen == [pytest.approx(0.35), pytest.approx(0.7)]
    assert t[0] == pytest.approx(2.1) and np.array_equal(d.frames[-1], noise(2))


def test_run_transition_is_limited_to_max_fps():
    """Frames that take no time: the rest of each 1/max_fps is slept, so a
    2 s transition has about 2 * max_fps frames instead of thousands."""
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s
    seen = []
    d = FakeDisplay()
    fbs.run_transition(d, lambda a, b, p: seen.append(p) or a, noise(1), noise(2),
                       2.0, clock=lambda: t[0], easing=fbs.EASINGS["linear"],
                       sleep=sleep, max_fps=10)
    assert len(seen) == 20  # p = 0, 0.05, ... 0.95
    assert seen == [pytest.approx(i / 20) for i in range(20)]
    assert all(s == pytest.approx(0.1) for s in slept)
    assert t[0] == pytest.approx(2.0) and np.array_equal(d.frames[-1], noise(2))


def test_max_fps_is_above_what_the_spi_display_shows():
    # 15 MHz SPI, 480x320 at 16 bit: about 6 full frames per second
    assert 15e6 / (480 * 320 * 16) < fbs.MAX_FPS <= 25


def _show(tmp_path, transitions, seed=0, duration=1.0):
    for i, c in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)], 1):
        mk_jpg(tmp_path / f"{i}.jpg", c, size=(80, 40))
    tick = [0.0]

    def clock():
        tick[0] += 0.1
        return tick[0]

    d = FakeDisplay()
    fbs.slideshow(d, fbs.list_images(str(tmp_path)), 1, duration, False, transitions,
                  random.Random(seed), sleep=lambda s: None, clock=clock)
    return d


def test_cut_is_default_and_shows_no_intermediate_frames(tmp_path):
    assert fbs.parse_args([], env={}).transitions == ["cut"]
    d = _show(tmp_path, ["cut"])
    assert len(d.frames) == 3  # one frame per picture, nothing in between


def test_config_switch_stack(tmp_path):
    assert fbs.parse_args(["--transitions", "stack"], env={}).transitions == ["stack"]
    assert fbs.parse_args([], env={"TRANSITIONS": "stack"}).transitions == ["stack"]
    d = _show(tmp_path, ["stack"])
    assert len(d.frames) > 3 * 5  # animated: several frames per transition


def test_stack_ends_pixel_identical_to_static_render(tmp_path):
    d = _show(tmp_path, ["stack"])
    last = fbs.load_image(str(tmp_path / "3.jpg"), (W, H))
    assert np.array_equal(d.frames[-1], last)


def test_stack_no_black_frame_between_pictures(tmp_path):
    """Next picture is prepared before the transition: from the first picture
    on, no frame is ever black (the letterbox bars aside)."""
    d = _show(tmp_path, ["stack"])
    first = next(i for i, f in enumerate(d.frames) if np.array_equal(
        f, fbs.load_image(str(tmp_path / "1.jpg"), (W, H))))
    for f in d.frames[first:]:
        # not the centre pixel alone: the black letterbox bars of these 2:1
        # test pictures pass the centre while a print is drawn away
        lit = f.any(axis=2)
        assert lit.mean() > 0.5  # most of the screen shows a picture


def test_next_picture_loaded_before_transition_starts(tmp_path):
    for i in (1, 2):
        mk_jpg(tmp_path / f"{i}.jpg", (200, 0, 0), size=(W, H))
    events = []
    orig_load, orig_run = fbs.load_image, fbs.run_transition

    def load(path, size, fill=False):
        img = orig_load(path, size, fill)
        events.append(("loaded", path))
        return img

    def run(display, fn, a, b, *args, **kw):
        assert b.shape == (H, W, 3)  # already scaled to the display size
        events.append(("transition", len([e for e in events if e[0] == "loaded"])))
        return orig_run(display, fn, a, b, *args, **kw)

    fbs.load_image, fbs.run_transition = load, run
    try:
        fbs.slideshow(FakeDisplay(), fbs.list_images(str(tmp_path)), 0, 0, False,
                      ["stack"], random.Random(0), sleep=lambda s: None)
    finally:
        fbs.load_image, fbs.run_transition = orig_load, orig_run
    transitions = [n for kind, n in events if kind == "transition"]
    assert transitions[0] >= 1 and transitions[1] >= 2


def test_seed_makes_a_run_reproducible(tmp_path):
    a = _show(tmp_path, ["stack"], seed=42)
    b = _show(tmp_path, ["stack"], seed=42)
    c = _show(tmp_path, ["stack"], seed=7)
    assert len(a.frames) == len(b.frames)
    assert all(np.array_equal(x, y) for x, y in zip(a.frames, b.frames))
    assert len(a.frames) != len(c.frames) or not all(
        np.array_equal(x, y) for x, y in zip(a.frames, c.frames))


def test_parse_args_seed_and_easing():
    assert fbs.parse_args(["--seed", "5"], env={}).seed == 5
    assert fbs.parse_args([], env={"SEED": "9"}).seed == 9
    assert fbs.parse_args(["--easing", "linear"], env={}).easing == "linear"
    assert fbs.parse_args([], env={"EASING": "ease-out"}).easing == "ease-out"
    with pytest.raises(SystemExit):
        fbs.parse_args(["--easing", "bounce"], env={})


_writes = None


def _audit(event, args):
    if _writes is None or event != "open":
        return
    path, mode, flags = args
    if (mode and any(c in mode for c in "wax+")) or (
            flags and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND)):
        _writes.append(str(path))


sys.addaudithook(_audit)


def test_show_with_stack_writes_only_to_the_framebuffer(tmp_path):
    """No file other than the framebuffer is opened for writing (SD card)."""
    global _writes
    pics = tmp_path / "pics"
    pics.mkdir()
    for i in (1, 2, 3):
        mk_jpg(pics / f"{i}.jpg", (i * 60, 0, 0), size=(W, H))
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    _writes = []
    try:
        rc = fbs.main([str(pics), "--fb", str(fb), "--size", f"{W}x{H}", "--bpp", "16",
                       "--invert", "off", "--hold", "0", "--duration", "0.2",
                       "--transitions", "stack", "--seed", "1", "--once"])
    finally:
        writes, _writes = _writes, None
    assert rc == 0
    assert set(writes) == {str(fb)}


# ------------------------------------------------------------ option check
def test_check_accepts_valid_options_without_touching_the_display(capsys):
    # the framebuffer does not exist: --check must not open it
    assert fbs.main(["--fb", "/dev/doesnotexist9", "--transitions", "stack",
                     "--shuffle", "--hold", "5", "--check"]) == 0
    assert "options ok" in capsys.readouterr().out
    assert fbs.main(["--fb", "/dev/doesnotexist9", "--check"]) == 0  # defaults


@pytest.mark.parametrize("bad", [
    ["--shuffle=0ff"], ["--shuffle=off"], ["--transitions", "bogus"],
    ["--hold", "five"], ["--easing", "bounce"], ["--limit", "85", "--black", "90"],
    ["--unknown"],
])
def test_check_rejects_bad_options(bad, capsys):
    with pytest.raises(SystemExit) as e:
        fbs.main(["--fb", "/dev/doesnotexist9", *bad, "--check"])
    assert e.value.code == 2
    assert "error:" in capsys.readouterr().err


def _service_default_args():
    unit = pathlib.Path(__file__).with_name("ci") / "fb-slideshow.service"
    m = re.search(r'^Environment="DEFAULT_ARGS=(.*)"$', unit.read_text(), re.M)
    assert m, "DEFAULT_ARGS missing in ci/fb-slideshow.service"
    return m.group(1).split()


def test_service_default_args_are_valid():
    assert fbs.main(["--fb", "/dev/doesnotexist9", *_service_default_args(),
                     "--check"]) == 0


def test_saved_options_override_service_defaults():
    # the service runs: fb-slideshow.py $DEFAULT_ARGS $SHOW_ARGS
    args = fbs.parse_args([*_service_default_args(), "--transitions", "cut",
                           "--shuffle"], env={})
    assert args.transitions == ["cut"]
    assert args.shuffle
    args = fbs.parse_args(_service_default_args(), env={})
    assert args.duration == 3 and args.easing == "ease-out"


# ------------------------------------------------------------ clock
PW, PH = 480, 320  # the real panel


class Sync:
    def __init__(self, ok=True):
        self.ok = ok

    def available(self):
        return self.ok


def at(h, m):
    return time.mktime((2026, 11, 14, h, m, 30, 0, 0, -1))


def test_timesync_needs_a_recent_sync(tmp_path):
    f = tmp_path / "synchronized"
    now = [1_000_000.0]
    s = fbs.TimeSync(str(f), max_age=48 * 3600, now=lambda: now[0])
    assert not s.available()  # after power-on: no sync yet
    f.touch()
    os.utime(f, (now[0], now[0]))
    assert s.available()
    now[0] += 48 * 3600 - 1
    assert s.available()  # sync lost, still within 48 h
    now[0] += 2
    assert not s.available()  # then the clock vanishes


def test_clock_text_24h_with_blank_leading_zero():
    t = [at(9, 5)]
    ov = fbs.ClockOverlay((PW, PH), Sync(), now=lambda: t[0])
    assert ov.text() == " 9:05"
    t[0] = at(23, 59)
    assert ov.text() == "23:59"
    assert fbs.ClockOverlay((PW, PH), Sync(False)).text() is None


def test_clock_size_and_position_lower_left():
    ov = fbs.ClockOverlay((PW, PH), Sync())
    out = ov.apply(np.zeros((PH, PW, 3), np.uint8), "88:88")
    ys, xs = np.nonzero(out.min(axis=2) > 200)
    px_cm = math.hypot(PW, PH) / (3.5 * 2.54)
    assert abs(xs.min() - 0.25 * px_cm) <= 2          # 0.25 cm from the left
    assert abs((PH - 1 - ys.max()) - 0.25 * px_cm) <= 2  # 0.25 cm from the bottom
    assert abs((ys.max() - ys.min() + 1) - 0.5 * px_cm) <= 2   # 0.5 cm high
    assert xs.max() < PW * 0.3  # HH:MM is about 1.7 cm wide
    assert out.max() == 255 and out[ys, xs].min() > 200  # white


def test_clock_draws_the_right_segments():
    ov = fbs.ClockOverlay((PW, PH), Sync())
    black = np.zeros((PH, PW, 3), np.uint8)
    lit = {t: (ov.apply(black, t).min(axis=2) > 128).sum()
           for t in (" 1:11", " 8:88", " 7:00")}
    assert lit[" 1:11"] < lit[" 7:00"] < lit[" 8:88"]
    assert (ov.apply(black, " 0:00") != ov.apply(black, " 8:00")).any()


def test_clock_apply_keeps_the_input_and_none_draws_nothing():
    ov = fbs.ClockOverlay((PW, PH), Sync())
    frame = np.full((PH, PW, 3), 100, np.uint8)
    keep = frame.copy()
    out = ov.apply(frame, "12:34")
    assert np.array_equal(frame, keep)
    assert (out != frame).any()
    y0, y1 = ov.rows
    assert np.array_equal(out[:y0], frame[:y0]) and np.array_equal(out[y1:], frame[y1:])
    assert ov.apply(frame, None) is frame


def test_clock_does_not_fit_a_tiny_display():
    with pytest.raises(ValueError):
        fbs.ClockOverlay((W, H), Sync(), diagonal_in=0.2)  # 0.5 cm > 32 px


def _fb_display(tmp_path, overlay):
    info = fbs.FbInfo(PW, PH, 16, PW * 2)
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    return fbs.Display(str(fb), info, False, overlay=overlay), fb


def test_display_refresh_rewrites_only_the_clock_rows(tmp_path):
    t = [at(12, 34)]
    sync = Sync()
    ov = fbs.ClockOverlay((PW, PH), sync, now=lambda: t[0])
    d, fb = _fb_display(tmp_path, ov)
    img = np.full((PH, PW, 3), 60, np.uint8)
    d.show(img)
    d._m.flush()
    first = np.frombuffer(fb.read_bytes(), "<u2").reshape(PH, PW).copy()
    writes = []
    real = d._m

    class Spy:  # records which byte ranges are written
        def __setitem__(self, k, v):
            writes.append((k.start, k.stop))
            real[k] = v

    d._m = Spy()
    d.refresh()
    assert writes == []  # same minute: nothing written
    t[0] = at(12, 35)
    d.refresh()
    y0, y1 = ov.rows
    assert writes == [(y0 * PW * 2, y1 * PW * 2)]
    d._m = real
    d._m.flush()
    second = np.frombuffer(fb.read_bytes(), "<u2").reshape(PH, PW)
    assert (second != first).any()
    assert np.array_equal(second[:y0], first[:y0])
    assert np.array_equal(second, fbs.np.frombuffer(
        fbs.encode_frame(ov.apply(img, "12:35"), d.info, False), "<u2").reshape(PH, PW))
    sync.ok = False  # sync too old: the clock vanishes, the picture is back
    d.refresh()
    d._m.flush()
    plain = np.frombuffer(fbs.encode_frame(img, d.info, False), "<u2").reshape(PH, PW)
    assert np.array_equal(np.frombuffer(fb.read_bytes(), "<u2").reshape(PH, PW), plain)
    d.close()


def test_display_without_overlay_is_unchanged(tmp_path):
    d, fb = _fb_display(tmp_path, None)
    img = noise(3)
    img = np.array(Image.fromarray(img).resize((PW, PH)))
    d.show(img)
    d.refresh()
    d._m.flush()
    assert fb.read_bytes() == fbs.encode_frame(img, d.info, False)
    d.close()


def test_slideshow_refreshes_the_clock_while_holding_and_paused(tmp_path):
    for i in (1, 2):
        mk_jpg(tmp_path / f"{i}.jpg", (255, 0, 0), size=(W, H))
    now = [0.0]
    sleeps, refreshes = [], []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    answers = iter([True])
    fbs.slideshow(FakeDisplay(), fbs.list_images(str(tmp_path)), 2.5, 0, False,
                  ["cut"], random.Random(0), sleep=sleep, clock=lambda: now[0],
                  paused=lambda: next(answers, False),
                  refresh=lambda: refreshes.append(now[0]))
    assert max(sleeps) <= 1.0  # never longer than a second without a refresh
    assert sum(s for s in sleeps if s != 0.2) == pytest.approx(5.0)
    assert 0.2 in sleeps  # paused once
    assert len(refreshes) >= 8


def test_clock_options():
    a = fbs.parse_args([], env={})
    assert a.clock and a.clock_hours == 48
    assert a.clock_sync_file == "/run/systemd/timesync/synchronized"
    assert not fbs.parse_args(["--no-clock"], env={}).clock
    assert not fbs.parse_args([], env={"CLOCK": "off"}).clock
    assert fbs.parse_args(["--clock-hours", "12"], env={}).clock_hours == 12
    for bad in (["--clock-hours", "-1"], ["--diagonal", "0"], ["--clock=yes"]):
        with pytest.raises(SystemExit):
            fbs.parse_args(bad, env={})


def test_main_with_clock_end_to_end(tmp_path):
    pics = tmp_path / "pics"
    pics.mkdir()
    mk_jpg(pics / "1.jpg", (0, 0, 0), size=(PW, PH))
    sync = tmp_path / "synchronized"
    sync.touch()
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    shown = []
    real_show = fbs.Display.show

    def spy(self, frame):
        real_show(self, frame)
        self._m.flush()
        shown.append(fb.read_bytes())

    fbs.Display.show = spy
    try:
        rc = fbs.main([str(pics), "--fb", str(fb), "--size", f"{PW}x{PH}", "--bpp",
                       "16", "--invert", "off", "--hold", "0", "--once",
                       "--clock-sync-file", str(sync)])
    finally:
        fbs.Display.show = real_show
    assert rc == 0
    px = np.frombuffer(shown[-1], "<u2")
    assert (px == 0xFFFF).any()  # white clock digits on the black picture
    assert fb.read_bytes() == b"\x00" * (PW * PH * 2)  # blanked on exit


# ------------------------------------------------------- photo pile (issue #20)
PILE_COLORS = [(250, 0, 0), (0, 250, 0), (0, 0, 250), (250, 250, 0), (0, 250, 250)]


def pile_pics(tmp_path, n=4):
    for i in range(n):
        mk_jpg(tmp_path / f"{i}.jpg", PILE_COLORS[i], size=(W, H))
    return fbs.list_images(str(tmp_path))


def card(color, size=(W, H)):
    img = Image.new("RGBA", (round(size[0] * 0.8), round(size[1] * 0.8)), color + (255,))
    return fbs.Card(img, Image.new("L", (img.width + 8, img.height + 8), 100), 4)


def test_pile_option_defaults_to_on_with_only_stack():
    assert fbs.parse_args(["--transitions", "stack"], env={}).pile
    assert not fbs.parse_args(["--transitions", "stack,fade"], env={}).pile
    assert not fbs.parse_args([], env={}).pile
    assert not fbs.parse_args(["--transitions", "stack", "--no-pile"], env={}).pile
    assert fbs.parse_args(["--transitions", "fade", "--pile"], env={}).pile


def test_load_card_fits_the_box_with_a_white_border(tmp_path):
    W, H = 480, 320
    mk_jpg(tmp_path / "wide.jpg", (0, 0, 200), size=(400, 100))
    c = fbs.load_card(str(tmp_path / "wide.jpg"), (W, H))
    assert c.image.mode == "RGBA"
    assert c.image.width <= round(W * fbs.PILE_SCALE)
    assert c.image.height < c.image.width / 3  # keeps its aspect
    px = np.asarray(c.image)
    assert tuple(px[0, 0, :3]) == fbs.PILE_BORDER_COLOR  # border
    assert px[px.shape[0] // 2, px.shape[1] // 2, 2] > 150  # the picture
    filled = fbs.load_card(str(tmp_path / "wide.jpg"), (W, H), fill=True)
    assert abs(filled.image.width / filled.image.height - W / H) < 0.05


def test_pile_frame_top_straight_in_the_middle_next_one_peeks_out():
    W, H = 480, 320
    rng = random.Random(1)
    pose = fbs.pile_pose(rng, (W, H), turn=4)
    assert 0.4 * 4 <= abs(pose.angle) <= 4
    top, under = card((250, 0, 0), (W, H)), card((0, 250, 0), (W, H))
    f = fbs.pile_frame((W, H), top, under, pose)
    assert tuple(f[H // 2, W // 2]) == (250, 0, 0)
    assert tuple(f[0, 0]) == fbs.PILE_TABLE or f[0, 0].max() < 30
    green = (f[..., 1] > 200) & (f[..., 0] < 50)
    assert green.sum() > 100  # the edge of the next print shows


def test_pile_transition_draws_the_top_away_and_straightens_the_next():
    W, H = 480, 320
    rng = random.Random(2)
    size = (W, H)
    p1, p2 = fbs.pile_pose(rng, size, 3), fbs.pile_pose(rng, size, 3)
    top, under, new = (card(c, size) for c in
                       ((250, 0, 0), (0, 250, 0), (0, 0, 250)))
    a = fbs.pile_frame(size, top, under, p1)
    b = fbs.pile_frame(size, under, new, p2)
    fn = fbs.make_pile_transition(rng, size, top, under, p1, new, p2)
    assert fn(a, b, 0) is a and fn(a, b, 1) is b
    red = [((fn(a, b, p)[..., 0] > 200) & (fn(a, b, p)[..., 1] < 50)).sum()
           for p in (0.01, 0.5, 0.99)]
    assert red[0] > red[1] > red[2]  # the top print leaves the screen
    late = fn(a, b, 0.99).astype(int)
    assert np.abs(late - b).mean() < 8  # almost the next rest frame


def test_slideshow_with_pile_shows_all_pictures_in_order(tmp_path):
    paths = pile_pics(tmp_path, 4)
    d = FakeDisplay()
    fbs.slideshow(d, paths, 1, 0, False, ["stack"], random.Random(0),
                  sleep=lambda s: None, pile=True)
    centers = [tuple(f[H // 2, W // 2]) for f in d.frames]
    tops = [min(range(4), key=lambda i: sum(abs(int(x) - y) for x, y in
                                            zip(c, PILE_COLORS[i])))
            for c in centers]
    assert tops == [0, 1, 2, 3]


def test_slideshow_with_pile_returns_the_unused_pictures_in_order(tmp_path):
    paths = pile_pics(tmp_path, 5)
    stops = iter([False, True])
    frame, unused = fbs.slideshow(FakeDisplay(), paths, 1, 0, False, ["stack"],
                                  random.Random(0), sleep=lambda s: None,
                                  stop=lambda: next(stops), pile=True)
    # shown as top: 0, 1; 2 lies below, 3 was loading
    assert [pathlib.Path(p).name for p in unused] == ["2.jpg", "3.jpg"]
    assert frame.shape == (H, W, 3)


def test_pile_turn_is_optional_default_straight():
    assert fbs.parse_args([], env={}).pile_turn == 0
    assert fbs.parse_args(["--pile-turn", "4"], env={}).pile_turn == 4
    with pytest.raises(SystemExit):
        fbs.parse_args(["--pile-turn", "-1"], env={})
    rng = random.Random(0)
    assert all(fbs.pile_pose(rng, (480, 320)).angle == 0 for _ in range(20))
    assert all(fbs.pile_pose(rng, (480, 320), 2).angle != 0 for _ in range(20))
