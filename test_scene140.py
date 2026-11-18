import importlib.util
import pathlib

import numpy as np
import pytest
from PIL import Image

spec = importlib.util.spec_from_file_location(
    "scene140", pathlib.Path(__file__).with_name("scene140.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)
fbs = sc.fbs


def scene140(*args, **kw):
    """The scene with the text "140" (the geometry tests were made for it)."""
    return sc.Scene(*args, text="140", **kw)

W, H = 480, 320


@pytest.fixture(scope="module")
def scene():
    return scene140((W, H), seed=1)


def ink(frame, thresh=40):
    ys, xs = np.nonzero(frame.max(axis=2) > thresh)
    return xs, ys


def test_starts_tiny_at_a_third_of_the_width_at_clock_height(scene):
    frame, rows = scene.frame(0)
    xs, ys = ink(frame)
    px_cm = np.hypot(W, H) / (3.5 * 2.54)
    assert abs(xs.min() - W / 3) <= 2                   # left edge at 1/3
    assert abs((H - ys.max()) - 0.25 * px_cm) <= 3      # clock's baseline
    assert ys.max() - ys.min() + 1 <= 12                # 5 pt: tiny
    assert rows[1] - rows[0] <= 16


def test_starts_white_ends_red_warm_on_the_way(scene):
    assert sc.ramp_color(0) == (255, 255, 255)
    r, g, b = sc.ramp_color(1)
    assert r > 200 and g < 60 and b < 60
    for q in np.linspace(0, 1, 21):
        r, g, b = sc.ramp_color(q)
        assert r >= g >= b  # warm all the way


def test_ends_in_the_middle_half_the_screen_high(scene):
    frame, _ = scene.frame(scene.travel + 0.1)
    xs, ys = ink(frame)
    assert abs((ys.max() - ys.min() + 1) - H / 2) <= 3
    assert abs((xs.min() + xs.max()) / 2 - W / 2) <= 3
    assert abs((ys.min() + ys.max()) / 2 - H / 2) <= 3
    r, g, b = frame[ys, xs].max(axis=0)
    assert r > 200 and g < 60


def test_arc_goes_up_first_then_right(scene):
    pts = [scene.position(q) for q in np.linspace(0, 1, 11)]
    dx0, dy0 = pts[1] - pts[0]
    dx1, dy1 = pts[-1] - pts[-2]
    assert -dy0 > 3 * abs(dx0)   # first mostly upward
    assert dx1 > 3 * abs(dy1)    # finally mostly to the right
    hs = [scene.height(q) for q in np.linspace(0, 1, 11)]
    assert hs == sorted(hs)      # only grows


def test_burst_fills_the_screen_and_fades_out(scene):
    t_burst = scene.travel + scene.pause
    frame, rows = scene.frame(t_burst + 0.5 * scene.explode)
    assert rows is None
    xs, ys = ink(frame, 10)
    # pieces in every ninth of the screen
    cells = {(x * 3 // W, y * 3 // H) for x, y in zip(xs, ys)}
    assert len(cells) == 9
    early = scene.frame(t_burst + 0.1 * scene.explode)[0].astype(int).sum()
    late = scene.frame(t_burst + 0.9 * scene.explode)[0].astype(int).sum()
    assert late < early / 5
    assert scene.frame(scene.total - 1e-6)[0].max() < 8  # all but gone


def test_same_seed_same_burst():
    a = scene140((W, H), seed=7).frame(8.0)[0]
    b = scene140((W, H), seed=7).frame(8.0)[0]
    c = scene140((W, H), seed=8).frame(8.0)[0]
    assert np.array_equal(a, b) and not np.array_equal(a, c)


class Clock:
    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def test_play_writes_only_the_rows_of_the_small_number(tmp_path):
    info = fbs.FbInfo(W, H, 16, W * 2)
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    d = fbs.Display(str(fb), info, False)
    writes = []
    real_rows, real_show = d.show_rows, d.show
    d.show_rows = lambda f, y0, y1: (writes.append((y0, y1)), real_rows(f, y0, y1))
    d.show = lambda f: (writes.append((0, H)), real_show(f))
    scene = scene140((W, H), travel=2.0, pause=0.2, explode=1.0, seed=1)
    n = sc.play(d, scene, clock=Clock(0.05), sleep=lambda s: None)
    assert n > 10
    first = writes[2:5]  # after the initial black and the first full frame
    assert all(y1 - y0 <= 32 and y0 % 4 == 0 for y0, y1 in first)
    assert writes[-1] == (0, H)
    d._m.flush()
    assert fb.read_bytes() == b"\x00" * (W * H * 2)  # ends black
    d.close()


def test_preview_gif(tmp_path):
    out = tmp_path / "140.gif"
    assert sc.main(["--preview", str(out), "--fps", "4", "--travel", "1",
                    "--explode", "1", "--seed", "1"]) == 0
    im = Image.open(out)
    assert im.size == (W, H) and im.n_frames > 3


def test_main_on_a_fake_framebuffer(tmp_path):
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    assert sc.main(["--fb", str(fb), "--size", f"{W}x{H}", "--invert", "off",
                    "--travel", "0.3", "--pause", "0", "--explode", "0.3"]) == 0
    assert fb.read_bytes() == b"\x00" * (W * H * 2)


@pytest.mark.parametrize("bad", [["--travel", "0"], ["--repeat", "-1"],
                                 ["--pause", "-1"], ["--fps", "0"]])
def test_bad_options(bad):
    with pytest.raises(SystemExit):
        sc.parse_args(bad)


def test_repeat_0_loops_until_stopped(tmp_path, monkeypatch):
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    rounds = []

    def fake_play(display, scene, **kw):
        rounds.append(1)
        if len(rounds) == 5:
            raise KeyboardInterrupt  # Ctrl+C
        return 1

    monkeypatch.setattr(sc, "play", fake_play)
    assert sc.parse_args(["--repeat", "0"]).repeat == 0
    assert sc.main(["--fb", str(fb), "--size", f"{W}x{H}", "--invert", "off",
                    "--repeat", "0"]) == 0
    assert len(rounds) == 5
    assert fb.read_bytes() == b"\x00" * (W * H * 2)  # black after Ctrl+C


@pytest.fixture(scope="module")
def picture():
    rng = np.random.default_rng(5)
    return rng.integers(0, 256, (H, W, 3), dtype=np.uint8)


def test_background_picture_stays_behind_the_number(picture):
    s = scene140((W, H), seed=1, background=picture)
    frame, (y0, y1) = s.frame(0)
    assert np.array_equal(frame[:y0], picture[:y0])
    assert np.array_equal(frame[y1:], picture[y1:])
    assert not np.array_equal(frame[y0:y1], picture[y0:y1])  # the tiny 140
    mid, _ = s.frame(s.travel + 0.1)
    changed = (mid != picture).any(axis=2)
    ys, xs = np.nonzero(changed)
    assert 20 < xs.min() and xs.max() < W - 20  # only around the number (~390 px wide)
    assert 40 < ys.min() and ys.max() < H - 40


def test_background_shows_through_as_the_fragments_fade(picture):
    s = scene140((W, H), seed=1, background=picture)
    t = s.travel + s.pause
    diff = [np.abs(s.frame(t + f * s.explode)[0].astype(int) - picture).mean()
            for f in (0.1, 0.5, 0.95)]
    assert diff[0] > diff[1] > diff[2] and diff[2] < 1
    assert np.array_equal(s.frame(s.total - 1e-9)[0], picture) or diff[2] < 1


def test_number_has_a_dark_rim_on_bright_pictures():
    white = np.full((H, W, 3), 255, np.uint8)
    s = scene140((W, H), seed=1, background=white)
    frame, _ = s.frame(0)  # white number on white: only the rim shows it
    assert frame.min() < 200


def test_load_background(tmp_path):
    pic = tmp_path / "bg.jpg"
    Image.new("RGB", (1600, 1200), (0, 0, 200)).save(pic)
    bg = sc.load_background(str(pic), (W, H))
    assert bg.shape == (H, W, 3) and abs(int(bg[0, 0, 2]) - 200) < 8  # filled
    assert not sc.load_background(str(tmp_path / "missing.jpg"), (W, H)).any()
    assert not sc.load_background(None, (W, H)).any()
    assert sc.parse_args(["--background", "none"]).background is None
    assert sc.parse_args([]).background == sc.DEFAULT_BACKGROUND


# ------------------------------------------------- variable scene (issue #17)
def test_color_option_ends_in_that_color():
    s = scene140((W, H), seed=1, color=(30, 180, 10))
    assert sc.ramp_color(0, s.ramp) == (255, 255, 255)
    frame, _ = s.frame(s.travel + 0.1)
    xs, ys = ink(frame)
    r, g, b = np.median(frame[ys, xs].astype(int), axis=0)
    assert g > 150 and r < 80 and b < 60
    burst, _ = s.frame(s.travel + s.pause + 0.5)
    xs, ys = ink(burst)
    r, g, b = burst[ys, xs].astype(int).mean(axis=0)
    assert g > r and g > b  # the fragments are green too


def test_starting_option_in_percent_from_left_and_bottom():
    s = scene140((W, H), seed=1, start=(70, 10))
    xs, ys = ink(s.frame(0)[0])
    assert abs(xs.min() - 0.7 * W) <= 2
    assert abs((H - ys.max()) - 0.1 * H) <= 3
    # the end stays the middle
    assert np.allclose(s.position(1), (W / 2, H / 2))


@pytest.mark.parametrize("text,ok", [("30, 180, 10", (30, 180, 10)),
                                     ("30,180,10", (30, 180, 10)),
                                     ("30 180", None), ("300,0,0", None),
                                     ("a,b,c", None)])
def test_parse_color(text, ok):
    if ok:
        assert sc.parse_color(text) == ok
    else:
        with pytest.raises(Exception):
            sc.parse_color(text)


def test_parse_starting():
    assert sc.parse_starting("100, 20") == (100, 20)
    with pytest.raises(Exception):
        sc.parse_starting("120,5")


class FakeDisplay:
    def __init__(self):
        self.calls = []

    def show(self, frame):
        self.calls.append(("show", frame.copy()))

    def show_rows(self, frame, y0, y1):
        self.calls.append(("rows", frame.copy()))


def test_play_many_overlapping_scenes_are_drawn_together():
    bg = np.full((H, W, 3), 40, np.uint8)
    a = scene140((W, H), 1.0, 0.0, 1.0, seed=1, background=bg)
    b = scene140((W, H), 1.0, 0.0, 1.0, seed=2, background=bg,
                 color=(30, 180, 10), start=(80, 60))
    seen = []
    real = sc.Scene.draw

    def spy(self, canvas, t):
        seen.append((id(self), round(t, 2)))
        return real(self, canvas, t)
    sc.Scene.draw, d = spy, FakeDisplay()
    try:
        clock = Clock(0.1)
        n = sc.play_many(d, [(0.0, a), (1.5, b)], clock=clock, sleep=lambda s: None)
    finally:
        sc.Scene.draw = real
    both = {t for i, t in seen if i == id(a)} & {t + 1.5 for i, t in seen if i == id(b)}
    assert n > 0 and both  # at some moments both were drawn
    assert clock.t < 2 * 3.5  # 3.5 s in all, not 4 (a then b)
    assert all(f.max() > 0 for _, f in d.calls)  # never black: the background
    assert (d.calls[-1][1] == bg).all()


def test_play_many_stops_starting_scenes_when_told():
    a = scene140((W, H), 0.3, 0.0, 0.3, seed=1)
    import itertools
    endless = ((i * 0.6, a) for i in itertools.count())
    clock = Clock(0.05)
    sc.play_many(FakeDisplay(), endless, clock=clock, sleep=lambda s: None,
                 stop=lambda: clock.t > 2)
    assert clock.t < 5


def test_text_option_and_default():
    assert sc.parse_args([]).text == "XXX"
    assert sc.parse_args(["--text", "140"]).text == "140"
    for bad in ["", "   ", "x" * 41]:
        with pytest.raises(SystemExit):
            sc.parse_args(["--text", bad])


def test_long_text_fits_the_width():
    s = sc.Scene((W, H), seed=1, text="Happy Birthday Richard")
    assert s.text == "Happy Birthday Richard"
    assert s.end_h < sc.END_HEIGHT * H                 # smaller than 140 ends
    assert s.master.shape[1] <= sc.END_WIDTH * W + 1   # fits the width
    assert s.start_h <= s.end_h
    frame, _ = s.frame(s.travel + 0.1)
    assert frame.shape == (H, W, 3)


def test_short_text_ends_half_the_screen_high():
    assert sc.Scene((W, H), seed=1, text="1").end_h == sc.END_HEIGHT * H
    s = sc.Scene((W, H), seed=1)  # "XXX": a bit wider than the screen
    assert s.master.shape[1] <= sc.END_WIDTH * W
    assert 0.8 * sc.END_HEIGHT * H < s.end_h <= sc.END_HEIGHT * H
