#!/usr/bin/env python3
"""Plays a sequence of slideshow and scene steps from a config file (issue #14).

The service starts this instead of fb-slideshow.py. Without a config file it
is the plain slideshow, exactly as before.

    /mnt/stick/config.yaml:

    loop: true              # start again from the top (default true)
    show_args:              # options of fb-slideshow.py for the whole show
      transitions: stack
      duration: 3
      easing: ease-out
    sequence:
      - scene:              # options as for scene140.py
          text: 140
          repeat: 3
          travel: 8
      - scene:              # follows a scene: starts 3 s before its end
          timing: -3
          color: 30, 180, 10
          starting: 70, 10
      - slideshow:          # options as for fb-slideshow.py, plus minutes
          minutes: 10
          shuffle: true
          hold: 5
          transitions: stack

show_args are the options of fb-slideshow.py, as on the command line (the
service's DEFAULT_ARGS and "make config"), and win over those; a step's
options replace them for that step only. Display options (fb, size, gamma,
clock, ...) can be set in show_args but not per step; when they change, the
show exits with code 3 and systemd starts it again with them. Scenes following each other play as one block over one background
(the first scene's, unless a later one sets its own); "timing" places a scene
relative to the end of the one before (negative: overlapping). The file is read again at the start of every round, so changes on the
stick take effect without a restart; if it is broken, the error is logged
and the last good version stays in use.

    python3 sequence.py [--config FILE] [fb-slideshow.py options]
    python3 sequence.py --check            # options and config file ok?
"""
import argparse
import contextlib
import importlib.util
import io
import itertools
import os
import pathlib
import shlex
import sys
import time
from typing import NamedTuple, Optional

import yaml

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_CONFIG = "/mnt/stick/config.yaml"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fbs = sys.modules.get("fbs") or _load("fbs", "fb-slideshow.py")
scene140 = sys.modules.get("scene140") or _load("scene140", "scene140.py")

# set once for the display, not per step
DISPLAY_OPTIONS = {"fb", "size", "bpp", "invert", "gamma", "limit", "black",
                   "dither", "diagonal", "clock_hours", "clock_sync_file"}
NOT_IN_STEPS = {"testcard", "once", "check", "preview", "fps", "config"}
# options that are used when the display is opened: a change needs a restart
RESTART_OPTIONS = sorted(DISPLAY_OPTIONS | {"clock", "seed"})
RESTART_EXIT = 3  # exit code: started again by systemd (Restart=on-failure)


class ConfigError(ValueError):
    pass


class RestartNeeded(Exception):
    """show_args changed an option used when the display was opened."""


class Step(NamedTuple):
    kind: str                 # "slideshow" | "scene"
    args: argparse.Namespace
    seconds: Optional[float]  # slideshow: None = forever
    text: str                 # for the log


class Config(NamedTuple):
    loop: bool
    steps: list
    show_argv: tuple = ()     # show_args as command-line options


def to_argv(opts):
    """{'hold': 5, 'shuffle': True, 'clock': False, 'transitions': ['a', 'b']}
    -> ['--hold', '5', '--shuffle', '--no-clock', '--transitions', 'a,b']"""
    argv = []
    for key, val in opts.items():
        flag = "--" + str(key).replace("_", "-")
        if val is True:
            argv.append(flag)
        elif val is False:
            if key in ("clock", "pile"):
                argv.append("--no-" + key)
            # other switches are off unless given
        elif isinstance(val, (list, tuple)):
            argv += [flag, ",".join(str(v) for v in val)]
        elif val is None:
            raise ConfigError(f"'{key}' has no value")
        else:
            argv += [flag, str(val)]
    return argv


def show_args_argv(value):
    """show_args from config.yaml -> command-line options. Accepts
        show_args: {transitions: stack, duration: 3, shuffle: true}
        show_args: [transitions stack, "duration: 3", {easing: ease-out}, shuffle]
        show_args: --transitions stack --duration 3"""
    if value is None:
        return []
    argv = []
    if isinstance(value, str):  # one line, as on the command line
        argv, value = shlex.split(value), {}
        if argv and not argv[0].startswith("-"):
            raise ConfigError("show_args: a line of options starts with '--'")
    items = value.items() if isinstance(value, dict) else None
    if items is None:
        if not isinstance(value, list):
            raise ConfigError("show_args: expected 'name: value' lines or a list")
        pairs = []
        for item in value:
            if isinstance(item, dict):
                pairs += item.items()
            elif isinstance(item, str) and item.strip():
                name, _, rest = item.strip().partition(" ")
                name = name.rstrip(":")
                rest = rest.strip()
                pairs.append((name, rest if rest else True))
            else:
                raise ConfigError(f"show_args: can't read {item!r}")
        items = pairs
    directory = None
    opts = {}
    for key, val in items:
        key = str(key).lstrip("-")
        if key.replace("-", "_") in NOT_IN_STEPS:
            raise ConfigError(f"show_args: '{key}' can't be set here")
        if key == "directory":
            directory = val
        else:
            opts[key] = val
    argv += to_argv(opts)
    for tok in argv:
        name = tok[2:].split("=")[0].replace("-", "_") if tok.startswith("--") else ""
        if name in NOT_IN_STEPS:
            raise ConfigError(f"show_args: '{name}' can't be set here")
    if directory is not None:
        if not isinstance(directory, str):
            raise ConfigError("show_args: directory must be a path")
        argv.append(directory)
    return argv


def display_key(args):
    """The options of a parsed fb-slideshow.py namespace that need a restart."""
    return tuple(repr(getattr(args, k, None)) for k in RESTART_OPTIONS)


def _parse(parser, argv, where):
    """Run an argparse-based parser; its error message becomes a ConfigError."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return parser(argv)
    except SystemExit:
        msg = [line.split("error: ", 1)[1] for line in err.getvalue().splitlines()
               if "error: " in line]
        raise ConfigError(f"{where}: {msg[0] if msg else err.getvalue().strip()}")


def parse_config(text, base_argv=()):
    """config.yaml text -> Config. base_argv: the command-line options the
    slideshow steps start from. Raises ConfigError with a readable message."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"not valid YAML: {e}")
    if not isinstance(data, dict):
        raise ConfigError("expected 'sequence:' with a list of steps")
    unknown = set(data) - {"loop", "sequence", "show_args"}
    if unknown:
        raise ConfigError(f"unknown setting: {', '.join(sorted(map(str, unknown)))}")
    loop = data.get("loop", True)
    if not isinstance(loop, bool):
        raise ConfigError("loop: must be true or false")
    show_argv = show_args_argv(data.get("show_args"))
    base_argv = [*base_argv, *show_argv]
    _parse(lambda a: fbs.parse_args(a, env={}), list(base_argv), "show_args")
    seq = data.get("sequence")
    if not isinstance(seq, list) or not seq:
        raise ConfigError("sequence: must be a list with at least one step")
    steps = []
    for i, item in enumerate(seq, 1):
        where = f"step {i}"
        if isinstance(item, str):
            item = {item: None}  # "- slideshow" without options
        if not isinstance(item, dict) or len(item) != 1:
            raise ConfigError(f"{where}: expected '- slideshow:' or '- scene:'")
        (kind, opts), = item.items()
        opts = dict(opts or {})
        if not isinstance(opts, dict):
            raise ConfigError(f"{where} ({kind}): options must be 'name: value' lines")
        where = f"{where} ({kind})"
        bad = {str(k).replace("-", "_") for k in opts} & (DISPLAY_OPTIONS | NOT_IN_STEPS)
        if bad:
            raise ConfigError(f"{where}: '{sorted(bad)[0]}' can't be set per step "
                              "(display options go on the command line / SHOW_ARGS)")
        if kind == "slideshow":
            minutes = opts.pop("minutes", None)
            directory = opts.pop("directory", None)  # positional, set after
            if directory is not None and not isinstance(directory, str):
                raise ConfigError(f"{where}: directory must be a path")
            if minutes is not None and (isinstance(minutes, bool) or
                                        not isinstance(minutes, (int, float))
                                        or minutes <= 0):
                raise ConfigError(f"{where}: minutes must be a positive number")
            args = _parse(lambda a: fbs.parse_args(a, env={}),
                          [*base_argv, *to_argv(opts)], where)
            if directory is not None:
                args.directory = directory
            seconds = None if minutes is None else minutes * 60
            steps.append(Step(kind, args, seconds,
                              f"slideshow {minutes if minutes else 'forever'}"
                              f"{' min' if minutes else ''}"))
        elif kind == "scene":
            args = _parse(scene140.parse_args, to_argv(opts), where)
            args.background_set = "background" in opts
            seconds = None if args.repeat == 0 else 0
            steps.append(Step(kind, args, seconds,
                              f"scene {args.text} x{args.repeat or 'forever'}"
                              + (f" {args.timing:+g}s" if args.timing else "")))
        else:
            raise ConfigError(f"step {i}: unknown step '{kind}' "
                              "(slideshow or scene)")
    for step in steps[:-1]:
        if step.seconds is None:
            raise ConfigError(f"'{step.text}' never ends, so the steps after it "
                              "would never play: give it minutes / repeat, or "
                              "make it the last step")
    return Config(loop, steps, tuple(show_argv))


def plain(base_argv):
    """No config file: the slideshow forever, as before."""
    return Config(True, [Step("slideshow", fbs.parse_args(list(base_argv)),
                              None, "slideshow forever")])


class ConfigFile:
    """Reads the config file when it changed; keeps the last good version."""

    def __init__(self, path, base_argv, log=fbs.log):
        self.path, self.base_argv, self.log = path, list(base_argv), log
        self._stamp = ()  # never a real stamp: the first get() reads/logs
        self.config = plain(self.base_argv)
        self.good = False  # a config from the file was read successfully

    def display_args(self):
        """fb-slideshow.py options of the whole show: command line + show_args."""
        return fbs.parse_args([*self.base_argv, *self.config.show_argv], env={})

    def display_key(self):
        return display_key(self.display_args())

    def _current(self):
        try:
            st = os.stat(self.path)
            return st.st_mtime, st.st_size
        except OSError:
            return None

    def changed(self):
        """The file was edited, added or removed since it was last read."""
        return self._current() != self._stamp

    def get(self):
        stamp = self._current()
        if stamp == self._stamp:
            return self.config
        self._stamp = stamp
        if stamp is None:
            self.log(f"no {self.path}: plain slideshow")
            self.config = plain(self.base_argv)
            self.good = False
            return self.config
        try:
            with open(self.path, encoding="utf-8") as f:
                self.config = parse_config(f.read(), self.base_argv)
            self.log(f"{self.path}: " + ", ".join(s.text for s in self.config.steps)
                     + (" (loop)" if self.config.loop else " (once)"))
            self.good = True
        except (OSError, UnicodeDecodeError, ConfigError) as e:
            self.log(f"{self.path}: {e} - " + ("keeping the previous sequence"
                                              if self.good else
                                              "NOT USED, plain slideshow"))
        return self.config


def scene_entries(block, size, load=scene140.load_background):
    """Consecutive scene steps -> (start s, Scene) entries on one timeline
    (endless when a step repeats forever). Repetitions of a step follow each
    other directly; a step's first scene starts `timing` s after the end of
    the scene before (not before that one's start). One background for the
    block: the first step's, unless a later step sets its own."""
    pictures = {}

    def picture(path):
        if path not in pictures:
            pictures[path] = load(path, size)
        return pictures[path]

    scenes = []
    for i, step in enumerate(block):
        a = step.args
        path = (a.background if i == 0 or getattr(a, "background_set", True)
                else block[0].args.background)
        scenes.append((a, scene140.Scene(size, a.travel, a.pause, a.explode,
                                         a.seed, background=picture(path),
                                         color=a.color, start=a.starting,
                                         text=a.text)))

    def timeline():
        end = start = 0.0
        for i, (a, scene) in enumerate(scenes):
            for k in itertools.count() if a.repeat == 0 else range(a.repeat):
                if i and not k:
                    start = max(start, end + a.timing)
                else:
                    start = end
                yield start, scene
                end = start + scene.total
    return scenes, timeline()


def run(display, config_file, overlay, rng, paused=lambda: False,
        clock=time.monotonic, sleep=time.sleep, rounds=None, once=False,
        log=fbs.log, opened_with=None):
    """Plays the steps, round after round (rounds=None: forever, unless the
    config says loop: false). When the config file changes, the current
    slideshow step ends after its picture and a new round starts with the
    new config. once: every picture only once (testing). opened_with: the
    display_key() the display was opened with; a config whose show_args
    change it raises RestartNeeded."""
    playlists = {}   # (directory, shuffle) -> iterator, continues across steps
    last = None      # frame on the display
    for _ in itertools.count() if rounds is None else range(rounds):
        cfg = config_file.get()
        if opened_with is not None and config_file.display_key() != opened_with:
            raise RestartNeeded()
        steps = cfg.steps
        i = 0
        interrupted = False
        while i < len(steps):
            step = steps[i]
            a = step.args
            if step.kind == "scene":  # with the scenes right after it
                j = i
                while j < len(steps) and steps[j].kind == "scene":
                    j += 1
                block, i = steps[i:j], j
                if len(steps) > 1:
                    log("step: " + " + ".join(s.text for s in block))
                display.overlay = None  # no clock over the scene
                scenes, entries = scene_entries(block, display.size)
                scene140.play_many(display, entries, clock=clock, sleep=sleep,
                                   stop=config_file.changed)
                last = scenes[-1][1].background
                if config_file.changed():
                    interrupted = True
                    break
                continue
            i += 1
            if len(steps) > 1:
                log(f"step: {step.text}")
            display.overlay = overlay if a.clock else None
            key = (a.directory, a.shuffle)
            if key not in playlists:
                playlists[key] = fbs.playlist(a.directory, a.shuffle, rng,
                                              loop=not once)
            end = None if step.seconds is None else clock() + step.seconds

            def stop(end=end):
                return (end is not None and clock() >= end) or config_file.changed()
            last, unused = fbs.slideshow(
                display, playlists[key], a.hold, a.duration, a.fill,
                a.transitions, rng, sleep=sleep, clock=clock, paused=paused,
                easing=fbs.EASINGS[a.easing],
                refresh=display.refresh if display.overlay else None,
                stop=stop, start=last, pile=a.pile,
                pile_turn=a.pile_turn)
            if unused:  # preloaded, not shown: they come next time
                playlists[key] = itertools.chain(unused, playlists[key])
            if config_file.changed():
                interrupted = True
                break  # new round with the new config
        if not interrupted:
            if not cfg.loop:
                return
            continue
        if once:
            return


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--config", default=os.environ.get("CONFIG", DEFAULT_CONFIG))
    own, base_argv = ap.parse_known_args(argv)
    if "-h" in base_argv or "--help" in base_argv:
        print(__doc__)
        return 0
    base = fbs.parse_args(base_argv)  # exits with a message if invalid
    if base.check:
        if os.path.exists(own.config):
            try:
                with open(own.config, encoding="utf-8") as f:
                    cfg = parse_config(f.read(), base_argv)
            except (OSError, UnicodeDecodeError, ConfigError) as e:
                print(f"{own.config}: {e}", file=sys.stderr)
                return 2
            print(f"{own.config} ok: " + ", ".join(s.text for s in cfg.steps)
                  + (" (loop)" if cfg.loop else " (once)"))
        print("options ok")
        return 0
    if base.testcard:
        return fbs.main(base_argv)
    config_file = ConfigFile(own.config, base_argv)
    config_file.get()
    base = config_file.display_args()  # with show_args from the config
    opened = fbs.open_display(base)
    if opened is None:
        return 2
    display, overlay = opened
    paused = fbs.install_signals()
    rng, seed = fbs.make_rng(base)
    fbs.log("Sequence " + fbs.describe(base, display, overlay, seed))
    try:
        run(display, config_file, overlay, rng, paused.is_set,
            rounds=1 if base.once else None, once=base.once,
            opened_with=display_key(base))
    except RestartNeeded:
        fbs.log(f"{own.config}: display options in show_args changed - restarting")
        return RESTART_EXIT
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        display.blank()
        display.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
