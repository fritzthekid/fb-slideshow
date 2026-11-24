import importlib.util
import os
import pathlib
import random
import sys

import numpy as np
import pytest
from PIL import Image

HERE = pathlib.Path(__file__).parent
spec = importlib.util.spec_from_file_location("sequence", HERE / "sequence.py")
seq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seq)
fbs, scene140 = seq.fbs, seq.scene140

W, H = 480, 320

EXAMPLE = (HERE / "config.example.yaml").read_text()


# ------------------------------------------------------------ parsing
def test_example_config_is_valid():
    cfg = seq.parse_config(EXAMPLE)
    assert cfg.loop
    assert [s.kind for s in cfg.steps] == ["scene", "scene", "slideshow"]
    scene, second, show = cfg.steps
    assert scene.args.repeat == 3 and scene.args.travel == 8
    assert second.args.timing == -3 and second.args.color == (30, 180, 10)
    assert second.args.starting == (70, 10)
    assert show.seconds == 600 and show.args.shuffle and show.args.hold == 5
    assert show.args.transitions == ["stack"]


def test_step_options_override_the_command_line_for_that_step_only():
    base = ["--transitions", "stack", "--duration", "3", "--hold", "20"]
    cfg = seq.parse_config("""
sequence:
  - slideshow: {minutes: 1, hold: 5}
  - slideshow: {minutes: 1, transitions: [cut, fade], clock: false}
""", base)
    a, b = cfg.steps
    assert a.args.hold == 5 and a.args.transitions == ["stack"] and a.args.duration == 3
    assert b.args.hold == 20 and b.args.transitions == ["cut", "fade"] and not b.args.clock


def test_to_argv():
    assert seq.to_argv({"hold": 5, "shuffle": True, "fill": False, "clock": False,
                        "transitions": ["a", "b"], "clock_hours": 2}) == [
        "--hold", "5", "--shuffle", "--no-clock", "--transitions", "a,b",
        "--clock-hours", "2"]


def test_defaults_loop_and_plain_step_names():
    cfg = seq.parse_config("sequence:\n  - scene\n  - slideshow\n")
    assert cfg.loop and cfg.steps[0].args.repeat == 1
    assert cfg.steps[1].seconds is None


def test_directory_per_step():
    cfg = seq.parse_config("sequence:\n  - slideshow: {directory: /tmp/x}\n")
    assert cfg.steps[0].args.directory == "/tmp/x"


@pytest.mark.parametrize("text, msg", [
    ("sequence: [", "not valid YAML"),
    ("- slideshow", "expected 'sequence:'"),
    ("sequenz:\n  - slideshow\n", "unknown setting: sequenz"),
    ("sequence: []\n", "at least one step"),
    ("loop: maybe\nsequence:\n  - slideshow\n", "loop: must be true or false"),
    ("sequence:\n  - movie: {}\n", "unknown step 'movie'"),
    ("sequence:\n  - slideshow: {hold: five}\n", "step 1 (slideshow): argument --hold"),
    ("sequence:\n  - slideshow: {transitions: wobble}\n", "Unknown transition: wobble"),
    ("sequence:\n  - slideshow: {minutes: 0}\n", "minutes must be a positive number"),
    ("sequence:\n  - slideshow: {gamma: 2}\n", "'gamma' can't be set per step"),
    ("sequence:\n  - scene: {travel: 0}\n", "step 1 (scene)"),
    ("sequence:\n  - scene: {colour: red}\n", "unrecognized arguments"),
    ("sequence:\n  - slideshow\n  - scene\n", "never ends"),
    ("sequence:\n  - scene: {repeat: 0}\n  - slideshow\n", "never ends"),
    ("sequence:\n  - slideshow: {hold: }\n", "'hold' has no value"),
])
def test_bad_configs_have_readable_errors(text, msg):
    with pytest.raises(seq.ConfigError) as e:
        seq.parse_config(text)
    assert msg in str(e.value)


def test_issue_example_style_is_rejected_clearly():
    # the sketch in issue #14 (make commands) is not a list of steps
    with pytest.raises(seq.ConfigError) as e:
        seq.parse_config('sequenz:\n  make scene SCENE_ARGS="--repeat 3"\n  make start\n')
    assert "unknown setting" in str(e.value)


# ------------------------------------------------------------ config file
def test_config_file_reload_and_keeping_the_last_good_one(tmp_path):
    f = tmp_path / "config.yaml"
    logs = []
    cf = seq.ConfigFile(str(f), [], log=logs.append)
    assert [s.text for s in cf.get().steps] == ["slideshow forever"]  # no file
    f.write_text("sequence:\n  - scene\n  - slideshow: {minutes: 2}\n")
    assert cf.changed()
    assert [s.kind for s in cf.get().steps] == ["scene", "slideshow"]
    assert not cf.changed()
    f.write_text("sequence: [\n")  # broken: keep the previous
    os.utime(f, (1, 1))
    assert [s.kind for s in cf.get().steps] == ["scene", "slideshow"]
    assert "keeping the previous sequence" in logs[-1]
    f.unlink()
    assert [s.text for s in cf.get().steps] == ["slideshow forever"]


def test_a_broken_file_at_the_start_says_plain_slideshow(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("sequence:\n  - scene: {starting: -6}\n")
    logs = []
    cf = seq.ConfigFile(str(f), [], log=logs.append)
    assert [s.text for s in cf.get().steps] == ["slideshow forever"]
    assert "--starting" in logs[-1] and "NOT USED, plain slideshow" in logs[-1]
    assert "keeping" not in logs[-1]


# ------------------------------------------------------------ running
class FakeDisplay:
    size = (W, H)

    def __init__(self):
        self.frames = []
        self.overlay = "clock"

    def show(self, frame):
        self.frames.append(("show", self.overlay, frame))

    def show_rows(self, frame, y0, y1):
        self.frames.append(("rows", self.overlay, frame))

    def refresh(self):
        pass


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.05
        return self.t

    def sleep(self, s):
        self.t += s


def pictures(tmp_path, n):
    d = tmp_path / "pics"
    d.mkdir()
    for i in range(n):
        Image.new("RGB", (W, H), (20 * i, 0, 0)).save(d / f"{i:02d}.jpg", quality=95)
    return d


class StaticConfig:
    def __init__(self, cfg):
        self.cfg = cfg

    def get(self):
        return self.cfg

    def changed(self):
        return False


def test_run_plays_steps_in_order_and_continues_the_pictures(tmp_path, monkeypatch):
    d = pictures(tmp_path, 6)
    cfg = seq.parse_config(f"""
sequence:
  - slideshow: {{directory: {d}, minutes: 0.05, hold: 1, transitions: cut}}
  - scene: {{repeat: 1, travel: 0.2, pause: 0, explode: 0.2, background: none}}
""")
    shown = []
    real_load = fbs.load_image
    monkeypatch.setattr(fbs, "load_image",
                        lambda p, *a, **k: (shown.append(pathlib.Path(p).name),
                                            real_load(p, *a, **k))[1])
    scenes = []
    real_play = scene140.play_many
    monkeypatch.setattr(scene140, "play_many",
                        lambda disp, e, **k: (scenes.append(disp.overlay),
                                              real_play(disp, e, **k))[1])
    c = Clock()
    disp = FakeDisplay()
    seq.run(disp, StaticConfig(cfg), "clock", random.Random(0), clock=c,
            sleep=c.sleep, rounds=2)
    assert scenes == [None, None]  # no clock during the scene
    # 3 s per round of slideshow at hold 1: pictures go on where they stopped,
    # the one preloaded at the end of a step is not skipped
    order = list(dict.fromkeys(shown))  # a preloaded picture is loaded again
    assert len(order) >= 4
    assert order == [f"{i:02d}.jpg" for i in range(len(order))]  # none skipped


def test_run_once_config_stops_after_one_round(tmp_path):
    d = pictures(tmp_path, 2)
    cfg = seq.parse_config(f"""
loop: false
sequence:
  - slideshow: {{directory: {d}, minutes: 0.02, hold: 1, transitions: cut}}
""")
    c = Clock()
    seq.run(FakeDisplay(), StaticConfig(cfg), None, random.Random(0), clock=c,
            sleep=c.sleep)  # returns: loop is false
    assert c.t < 10


def test_run_restarts_with_the_new_config_when_the_file_changes(tmp_path):
    d = pictures(tmp_path, 3)
    f = tmp_path / "config.yaml"
    cf = seq.ConfigFile(str(f), [str(d), "--hold", "1", "--transitions", "cut"],
                        log=lambda m: None)
    c = Clock()
    calls = []

    def sleep(s):
        c.sleep(s)
        if len(calls) == 0 and c.t > 3:  # the user saves a config meanwhile
            f.write_text("loop: false\nsequence:\n  - scene: {travel: 0.1, "
                         "pause: 0, explode: 0.1, background: none}\n")
            calls.append(1)

    disp = FakeDisplay()
    seq.run(disp, cf, None, random.Random(0), clock=c, sleep=sleep)
    assert calls  # the plain slideshow ended and the new sequence played once


def test_main_check_validates_the_config(tmp_path, capsys):
    good = tmp_path / "good.yaml"
    good.write_text(EXAMPLE)
    bad = tmp_path / "bad.yaml"
    bad.write_text("sequence:\n  - slideshow: {hold: five}\n")
    assert seq.main(["--config", str(good), "--check"]) == 0
    out = capsys.readouterr().out
    assert "ok: scene XXX x3, scene XXX x1 -3s, slideshow 10 min (loop)" in out
    assert seq.main(["--config", str(bad), "--check"]) == 2
    assert "argument --hold" in capsys.readouterr().err
    assert seq.main(["--config", str(tmp_path / "none.yaml"), "--check"]) == 0


def test_main_end_to_end_once_on_a_fake_framebuffer(tmp_path):
    d = pictures(tmp_path, 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sequence:\n  - scene: {travel: 0.2, pause: 0, explode: 0.2, "
                   "background: none}\n  - slideshow: {hold: 0}\n")
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    assert seq.main(["--config", str(cfg), str(d), "--fb", str(fb), "--size",
                     f"{W}x{H}", "--invert", "off", "--once", "--no-clock"]) == 0
    assert fb.read_bytes() == b"\x00" * (W * H * 2)


def test_service_runs_the_sequence():
    unit = (HERE / "ci" / "fb-slideshow.service").read_text()
    assert "/fb-slideshow/sequence.py $DEFAULT_ARGS $SHOW_ARGS" in unit


# ------------------------------------------------- scene blocks (issue #17)
def test_issue_17_example_parses():
    cfg = seq.parse_config("""
sequence:
  - scene:
      repeat: 1
      travel: 8
      color: 30, 180, 10       # final color
  - scene:
      repeat: 1
      travel: 8
      timing: -3
      starting: 100, 20        # horizontal, vertical
  - slideshow:
""")
    a, b = cfg.steps[0].args, cfg.steps[1].args
    assert a.color == (30, 180, 10) and a.starting is None
    assert b.timing == -3 and b.starting == (100, 20)
    assert cfg.steps[1].text == "scene XXX x1 -3s"


def test_scene_block_timeline_overlaps_and_shares_the_background():
    cfg = seq.parse_config("""
sequence:
  - scene: {repeat: 2, travel: 2, pause: 0, explode: 1, background: one.jpg}
  - scene: {travel: 2, pause: 0, explode: 1, timing: -1}
  - scene: {travel: 2, pause: 0, explode: 1, timing: 2, background: two.jpg}
""")
    loaded = []

    def load(path, size):
        loaded.append(path)
        return np.full((H, W, 3), len(loaded), np.uint8)
    scenes, entries = seq.scene_entries(cfg.steps, (W, H), load)
    starts = [s for s, _ in entries]
    assert starts == [0, 3, 5, 10]  # x2 back to back, -1 s overlap, +2 s gap
    assert loaded == ["one.jpg", "two.jpg"]
    assert scenes[0][1].background is scenes[1][1].background
    assert scenes[2][1].background is not scenes[0][1].background


def test_timing_never_starts_before_the_previous_scene():
    cfg = seq.parse_config("""
sequence:
  - scene: {travel: 2, pause: 0, explode: 1, background: none}
  - scene: {travel: 2, pause: 0, explode: 1, background: none, timing: -10}
""")
    _, entries = seq.scene_entries(cfg.steps, (W, H))
    assert [s for s, _ in entries] == [0, 0]


def test_run_plays_consecutive_scenes_as_one_block(monkeypatch):
    cfg = seq.parse_config("""
sequence:
  - scene: {travel: 0.2, pause: 0, explode: 0.2, background: none}
  - scene: {travel: 0.2, pause: 0, explode: 0.2, background: none, timing: -0.2}
""")
    blocks = []
    monkeypatch.setattr(scene140, "play_many",
                        lambda d, e, **k: blocks.append(list(e)))
    logs = []
    seq.run(FakeDisplay(), StaticConfig(cfg), None, random.Random(0),
            rounds=1, log=logs.append)
    assert [[round(s, 2) for s, _ in b] for b in blocks] == [[0, 0.2]]
    assert logs == ["step: scene XXX x1 + scene XXX x1 -0.2s"]


# ------------------------------------------------------- photo pile (issue #20)
def test_pile_per_step():
    assert seq.to_argv({"pile": False}) == ["--no-pile"]
    cfg = seq.parse_config("""
sequence:
  - slideshow: {minutes: 1, transitions: stack}
  - slideshow: {minutes: 1, transitions: stack, pile: false}
  - slideshow: {transitions: fade}
""")
    assert [s.args.pile for s in cfg.steps] == [True, False, False]
    turned = seq.parse_config("sequence:\n  - slideshow: {pile_turn: 3}\n")
    assert turned.steps[0].args.pile_turn == 3


def test_run_with_pile_continues_the_pictures(tmp_path, monkeypatch):
    d = pictures(tmp_path, 8)
    cfg = seq.parse_config(f"""
sequence:
  - slideshow: {{directory: {d}, minutes: 0.05, hold: 1, transitions: stack, duration: 0}}
  - scene: {{repeat: 1, travel: 0.2, pause: 0, explode: 0.2, background: none}}
""")
    monkeypatch.setattr(scene140, "play_many", lambda *a, **k: 0)
    tops = []
    real = fbs.pile_frame
    monkeypatch.setattr(fbs, "pile_frame",
                        lambda size, top, *a: (tops.append(top), real(size, top, *a))[1])
    real_load = fbs.load_card

    def load(p, *a, **k):
        c = real_load(p, *a, **k)
        c.image.info["name"] = pathlib.Path(p).name
        return c
    monkeypatch.setattr(fbs, "load_card", load)
    c = Clock()
    seq.run(FakeDisplay(), StaticConfig(cfg), None, random.Random(0), clock=c,
            sleep=c.sleep, rounds=3)
    order = [t.image.info["name"] for t in tops]
    order = [n for i, n in enumerate(order) if i == 0 or n != order[i - 1]]
    assert len(order) >= 6
    # none skipped (the playlist starts again after the 8th)
    assert order == [f"{i % 8:02d}.jpg" for i in range(len(order))]


# ------------------------------------------------- show_args (issue #31)
def slideshow_args(cfg):
    return vars(cfg.steps[-1].args)


def test_show_args_in_config_same_as_on_the_command_line():
    # acceptance: DEFAULT_ARGS=--transitions cut == show_args in config.yaml
    via_service = seq.parse_config("sequence:\n  - slideshow\n", ["--transitions", "cut"])
    via_config = seq.parse_config("show_args:\n  transitions: cut\n"
                                  "sequence:\n  - slideshow\n")
    assert slideshow_args(via_config) == slideshow_args(via_service)
    assert via_config.steps[0].args.transitions == ["cut"]


@pytest.mark.parametrize("form", [
    "show_args:\n  transitions: stack\n  duration: 3\n  easing: ease-out\n  shuffle: true\n",
    "show_args:\n  - transitions stack\n  - duration: 3\n  - easing: ease-out\n  - shuffle\n",
    "show_args: --transitions stack --duration 3 --easing ease-out --shuffle\n",
])
def test_show_args_forms(form):
    cfg = seq.parse_config(form + "sequence:\n  - slideshow\n")
    a = cfg.steps[0].args
    assert a.transitions == ["stack"] and a.duration == 3
    assert a.easing == "ease-out" and a.shuffle
    assert cfg.show_argv[:2] == ("--transitions", "stack")


def test_show_args_win_over_the_service_and_steps_win_over_show_args():
    cfg = seq.parse_config("""
show_args: {hold: 7, transitions: cut}
sequence:
  - slideshow: {minutes: 1}
  - slideshow: {minutes: 1, hold: 2}
""", ["--hold", "20", "--transitions", "stack", "--duration", "3"])
    a, b = cfg.steps
    assert a.args.hold == 7 and a.args.transitions == ["cut"] and a.args.duration == 3
    assert b.args.hold == 2 and b.args.transitions == ["cut"]


def test_show_args_directory_and_display_options():
    cfg = seq.parse_config("show_args: {directory: /pics, no-clock: true, gamma: 1.5}\n"
                           "sequence:\n  - slideshow\n")
    a = cfg.steps[0].args
    assert a.directory == "/pics" and not a.clock and a.gamma == (1.5, 1.5, 1.5)


@pytest.mark.parametrize("text, msg", [
    ("show_args: {holdd: 5}", "show_args: unrecognized arguments"),
    ("show_args: {hold: x}", "show_args: argument --hold"),
    ("show_args: {once: true}", "'once' can't be set here"),
    ("show_args: --check", "'check' can't be set here"),
    ("show_args: hold 5", "starts with '--'"),
    ("show_args: 5", "expected 'name: value' lines or a list"),
])
def test_show_args_errors(text, msg):
    with pytest.raises(seq.ConfigError) as e:
        seq.parse_config(text + "\nsequence:\n  - slideshow\n")
    assert msg in str(e.value)


def test_display_options_in_show_args_need_a_restart(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("show_args: {hold: 5}\nsequence:\n  - slideshow: {minutes: 1}\n")
    cf = seq.ConfigFile(str(f), ["--size", "480x320"], log=lambda m: None)
    cf.get()
    opened = cf.display_key()
    f.write_text("show_args: {hold: 9, shuffle: true}\nsequence:\n  - slideshow: {minutes: 1}\n")
    os.utime(f, (1, 1))
    cf.get()
    assert cf.display_key() == opened  # slideshow options: no restart
    f.write_text("show_args: {no-clock: true}\nsequence:\n  - slideshow: {minutes: 1}\n")
    os.utime(f, (2, 2))
    with pytest.raises(seq.RestartNeeded):
        seq.run(FakeDisplay(), cf, None, random.Random(1), clock=Clock(),
                sleep=lambda s: None, rounds=1, opened_with=opened)


def test_main_exits_for_a_restart_when_display_options_change(tmp_path, monkeypatch):
    d = pictures(tmp_path, 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("show_args: {no-clock: true}\nsequence:\n  - slideshow: {hold: 0}\n")
    fb = tmp_path / "fb"
    fb.write_bytes(b"")
    calls = []

    def fake_run(*a, opened_with=None, **kw):
        calls.append(opened_with)
        raise seq.RestartNeeded()
    monkeypatch.setattr(seq, "run", fake_run)
    assert seq.main(["--config", str(cfg), str(d), "--fb", str(fb), "--size",
                     f"{W}x{H}", "--invert", "off"]) == seq.RESTART_EXIT
    # the display was opened with the config's show_args (no clock)
    base = fbs.parse_args([str(d), "--size", f"{W}x{H}", "--no-clock"], env={})
    assert calls[0][seq.RESTART_OPTIONS.index("clock")] == repr(base.clock)


def test_scene_text_per_step():
    cfg = seq.parse_config("sequence:\n  - scene: {text: 140}\n  - scene\n")
    assert [s.args.text for s in cfg.steps] == ["140", "XXX"]
    assert cfg.steps[0].text == "scene 140 x1"


# ------------------------------------------------- cloud: in config.yaml
@pytest.mark.parametrize("value, expected", [
    ({"address": "https://cloud.example.org", "user": "diashow"},
     ("https://cloud.example.org/remote.php/dav/files/diashow/", "diashow", "bilder")),
    ({"address": "cloud.example.org/", "user": "u", "folder": "/A/B/"},
     ("https://cloud.example.org/remote.php/dav/files/u/", "u", "A/B")),
    (None, None),
])
def test_cloud_settings(value, expected):
    assert seq.cloud_settings(value) == expected


@pytest.mark.parametrize("value, msg", [
    ({"address": "https://x.org", "user": "u", "password": "p"}, "password is not set here"),
    ({"user": "u"}, "address must be given"),
    ({"address": "https://x.org/remote.php", "user": "u"}, "the server only"),
    ({"address": "ftp://x.org", "user": "u"}, "the server only"),
    ({"address": "https://x.org", "user": "a/b"}, "must not contain"),
    ({"address": "https://x.org", "user": "u", "folder": "../x"}, "folder must be"),
    ("https://x.org", "expected 'address:'"),
    ({"address": "https://x.org", "user": "u", "pull": "yes"}, "pull must be true or false"),
])
def test_cloud_settings_errors(value, msg):
    with pytest.raises(seq.ConfigError) as e:
        seq.cloud_settings(value)
    assert msg in str(e.value)


def test_cloud_pull_is_off_by_default():
    assert seq.cloud_pull({"address": "https://x.org", "user": "u"}) is False
    assert seq.cloud_pull({"address": "https://x.org", "user": "u", "pull": True}) is True
    assert seq.cloud_pull(None) is False


def test_config_with_cloud_is_valid_and_checked():
    seq.parse_config("cloud: {address: https://x.org, user: u}\nsequence:\n  - slideshow\n")
    with pytest.raises(seq.ConfigError):
        seq.parse_config("cloud: {address: https://x.org}\nsequence:\n  - slideshow\n")


def test_main_cloud_prints_the_settings(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    assert seq.main(["--config", str(cfg), "--cloud"]) == 1  # no file
    cfg.write_text("sequence:\n  - slideshow\n")
    assert seq.main(["--config", str(cfg), "--cloud"]) == 1  # no cloud section
    cfg.write_text("cloud: {address: https://x.org, user: u, folder: f}\n"
                   "sequence: broken\n")  # only the cloud section counts
    assert seq.main(["--config", str(cfg), "--cloud"]) == 0
    assert capsys.readouterr().out.split() == [
        "https://x.org/remote.php/dav/files/u/", "u", "f", "false"]  # pull: default off
    cfg.write_text("cloud: {address: https://x.org, user: u, pull: true}\n")
    assert seq.main(["--config", str(cfg), "--cloud"]) == 0
    assert capsys.readouterr().out.split()[-1] == "true"
    cfg.write_text("cloud: {user: u}\n")
    assert seq.main(["--config", str(cfg), "--cloud"]) == 2
