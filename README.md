# fb-slideshow

Small slideshow for the 480x320 SPI display (fbtft, `/dev/fb1`) of a
Raspberry Pi. Shows the `*.jpg` / `*.jpeg` files of a directory in natural
order, with a hard cut or a transition. Writes directly into the framebuffer
(mmap), without X11 and without fbi.

**Documentation (German):** <https://fritzthekid.github.io/fb-slideshow/>,
with how to write `config.yaml` (and many examples), how to set up the
Nextcloud and how picture providers upload (source in `docs/`).

## Requirements

- Raspberry Pi with SPI TFT, framebuffer `/dev/fb1`
- Display overlay matching the panel. Tested: Waveshare 3.5" IPS (B)
  with `dtoverlay=waveshare35b-v2:bgr=1,rotate=270` in `/boot/firmware/config.txt`
- Python 3, `pip install -r requirements.txt` (numpy, Pillow, pytest)
- Access to `/dev/fb1` (group `video`), no `sudo` needed

## Usage

```bash
python3 fb-slideshow.py                       # /mnt/stick/bilder, 10 s per image
python3 fb-slideshow.py ~/pictures --hold 20 --shuffle
```

Geometry, color depth and stride are read from sysfs
(`/sys/class/graphics/fbN/`), inversion from the device tree
(`--invert auto`). Supported: RGB565 (16 bpp) and BGRA (32 bpp).

## Options

| Option | Default | Meaning |
|---|---|---|
| `directory` | `/mnt/stick/bilder` | image directory (env `IMAGE_DIR`, legacy `BILDER_DIR`) |
| `--fb` | `/dev/fb1` | framebuffer device (env `FB_DEVICE`) |
| `--hold S` | 10 | seconds per image (env `HOLD`) |
| `--transitions LIST` | `cut` | comma-separated, see [Transitions](#transitions) (env `TRANSITIONS`) |
| `--duration S` | 2.0 | transition duration (env `DURATION`) |
| `--easing E` | `ease-in-out` | speed curve: `ease-in-out`, `ease-out` or `linear` (env `EASING`) |
| `--seed N` | new each start | random seed for `--shuffle` and transition variations; logged at start so a run can be repeated (env `SEED`) |
| `--shuffle` | off | random order |
| `--fill` | off | image fills the display (cropped) instead of being fitted |
| `--invert auto\|on\|off` | auto | panel inversion |
| `--size WxH`, `--bpp N` | from sysfs | manual override of the framebuffer values |
| `--gamma G` | 1.0 | gamma correction (env `GAMMA`) |
| `--limit N[,N,N]` | 255 | upper brightness limit per channel (env `LIMIT`) |
| `--black R,G,B` | 0 | black point per channel (env `BLACK`) |
| `--dither` | off | Bayer dithering during RGB565 conversion |
| `--testcard [bars\|ramps]` | – | show a test card instead of the slideshow |
| `--clock`, `--no-clock` | on | 7-segment clock in the lower left corner, see [Clock](#clock) (env `CLOCK=off`) |
| `--clock-hours H` | 48 | keep the clock this long after the last time sync (env `CLOCK_HOURS`) |
| `--clock-sync-file F` | `/run/systemd/timesync/synchronized` | file touched on every time sync (env `CLOCK_SYNC_FILE`) |
| `--diagonal IN` | 3.5 | display diagonal in inches, sets the clock's size (env `DIAGONAL`) |
| `--once` | off | only one round, then exit |
| `--check` | – | only check the options, then exit (display untouched) |

`--gamma`, `--limit`, `--black` and `--dither` are corrections for
panels with a wrong initialization. With the matching overlay they are
not needed.

Unreadable images are skipped. If the directory is empty, the program
waits and checks again.

## Transitions

| Name | Effect |
|---|---|
| `cut` | hard cut, the next picture replaces the current one at once (default) |
| `stack` | the pictures lie on a stack: the current one slides off to the left, right, top or bottom, with a slight sideways drift and a soft shadow; the next one already lies still underneath. When it is the only transition the pictures lie as a visible pile of prints, see [Photo pile](#photo-pile) |
| `fade`, `wipe`, `push`, `blinds`, `dissolve`, `iris`, `zoom`, `split` | older effects, available but not part of the default |

With several names, one is picked at random for each picture, never the same
one twice in a row. The next picture is loaded and scaled while the current
one is shown, so a transition never waits for it. Transitions are time-based:
if frames are slow, frames are dropped and the duration stays the same.

### Configuring

| Setting | Option | Env | Default |
|---|---|---|---|
| which transition(s) | `--transitions cut\|stack\|...` | `TRANSITIONS` | `cut` |
| duration in seconds | `--duration S` | `DURATION` | 2.0 |
| speed curve | `--easing ease-in-out\|ease-out\|linear` | `EASING` | `ease-in-out` |
| random seed (direction and drift of each `stack`) | `--seed N` | `SEED` | new each start, logged |
| photo pile | `--pile` / `--no-pile` | – | on when the transitions are only `stack` |
| turn of the print below in the pile | `--pile-turn DEG` | `PILE_TURN` | 0 (straight) |

Examples:

```bash
# directly
python3 fb-slideshow.py --transitions stack --duration 3 --easing ease-out

# on hydra, from itaka (kept across reboots and deploys, restarts the show)
make config SHOW_ARGS="--duration 4"    # on top of the service defaults
make config SHOW_ARGS=                  # back to the service defaults

# from GitHub
gh workflow run show.yml -f action=config -f show_args="--transitions stack"
```

On hydra the service always starts the show with
`--transitions stack --duration 3 --easing ease-out` (`DEFAULT_ARGS` in
`ci/fb-slideshow.service`, changed by editing that file and `make deploy`).
Options saved with `make config` come after them, so they add to these
defaults and win where they set the same option (e.g. `--transitions cut`).
Simpler: put them into `config.yaml` on the stick as `show_args` (see
[Sequence](#sequence-configyaml)); those win over both.
The program's own defaults in the table above apply when it is started by hand.

To repeat a run exactly, take the seed from the start line in the log
(`make status`) and pass it with `--seed`.

### Photo pile

With `--transitions stack` alone (as on hydra) the pictures lie as a pile of
prints on a dark table (issue #20). Each picture keeps its full size, with a
thin white border (1 % of the screen height, 3 px); the next one lies below
it, slightly shifted. It shows while the top print is drawn away (as
`stack`: left, right, up or down, with drift and a shadow) and moves into
its place, while the one after it appears below. At rest it peeks out only
beside pictures that are narrower or wider than the screen (where there
were black bars before; `--fill`: all pictures have the screen's shape).

`--pile-turn DEG` lets the print below lie turned by up to DEG degrees
(randomly left or right, at least 40 % of it), straightening up as it comes
to the top; e.g. `--pile-turn 4`. Default 0: straight. In a sequence step:
`pile_turn: 4`.

`--no-pile` switches it off (full-screen pictures, plain `stack`), `--pile`
switches it on with other transitions too; in a sequence step: `pile: false`.
Fixed in the code: `PILE_SCALE` (1.0 = full size), `PILE_BORDER`, `PILE_SHIFT`, `PILE_TABLE`,
`PILE_BORDER_COLOR`, `PILE_SHADOW`. Two pictures are loaded ahead (the one
below and the one after it), so no transition waits for loading. On hydra a
pile transition is as fast as the plain `stack` (~310 ms per frame, measured
the same way for both; the display is the limit).

Fixed in the code (not options): the exit directions of `stack` (left,
right, up, down), its maximum sideways drift (8 % of the display, constant
`STACK_MAX_DRIFT`), and the shadow (4 % of the display wide, 35 % darkening
at the edge, in `make_stack`).

Frame rate on hydra (Raspberry Pi 3 B, 480x320 RGB565 over SPI at 15 MHz,
measured with `make measure-fps`): a full frame takes ~164 ms on the SPI bus,
so the panel can show at most **6.1 frames/s**. A 2 s `stack` transition
reaches **~4.9 frames/s** on the panel (about 10 full frames). The CPU is not
the limit (the program renders ~8 frames/s); only a higher SPI clock would
make transitions smoother.

## Clock

A white 7-segment clock (`HH:MM`, 24 h, leading zero blanked) lies over the
pictures in the lower left corner: 0.5 cm high, 0.25 cm from the left and
the bottom edge (computed from the display diagonal, `--diagonal`). A soft dark
rim keeps it readable on bright pictures. It also stays in place during
transitions and while paused.

It is only shown while the time can be trusted. hydra has no hardware
clock, so after power-on the time is wrong until the first network sync:

- after power-on: no clock until systemd-timesyncd has synchronized the time
  (it touches `/run/systemd/timesync/synchronized`, in RAM, on every sync)
- sync lost (no network): the clock stays for 48 h after the last sync
  (`--clock-hours`), then it vanishes until the next sync

At each new minute only the clock's rows are rewritten, so the SPI bus sends
about a quarter of a frame. Switch it off with `--no-clock`, e.g.
`make config SHOW_ARGS="--no-clock"`.

## Scene

A scene (`scene140.py`, issue #11), standalone or as a step of a sequence
(see below). A tiny text (`--text`, default `XXX`; 5 pt, Ubuntu Bold)
appears at the clock's height, its left edge at a third of the width. It
rises in an arc (up first, then right) into the middle, growing until its
letters are half the screen high (a long text less, so that it fits the
width), its color
warming from white over yellow and orange to red. In the middle it bursts
into fragments that fly over the whole screen and fade out, so the picture
behind shows through again. About 10 s.

Background: the picture `/mnt/stick/background/scene-background.jpg`
(fills the screen, cropped), black if it is missing. A soft dark rim keeps
the text readable on bright pictures. Another picture:
`--background FILE`, black: `--background none`.

```bash
make scene                          # stops the show, plays the scene on hydra
make scene SCENE_ARGS="--repeat 3 --travel 8 --text 140"
make scene SCENE_ARGS="--repeat 0"  # loop until Ctrl+C
make start                          # the show again
make scene-preview                  # scene140.gif on itaka, to look at on a PC
make scene-preview SCENE_ARGS="--background background/scene-background.jpg"
```

| Option | Default | Meaning |
|---|---|---|
| `--text T` | `XXX` | the text that flies, 1..40 characters (e.g. `140`) |
| `--travel S` | 6 | seconds for the arc into the middle |
| `--pause S` | 0.6 | seconds in the middle before the burst |
| `--explode S` | 3 | seconds for the burst |
| `--repeat N` | 1 | times to play; `0` = loop until stopped (Ctrl+C) |
| `--seed N` | random | fragments of the burst |
| `--color R,G,B` | warm ramp to red | final color; the number starts white and blends into it, the fragments have it too |
| `--starting X,Y` | `33,4` | start in percent of the screen: left edge of the number from the left, its bottom from the bottom (the end stays the middle) |
| `--timing S` | 0 | only in a sequence: start S seconds after the end of the previous scene; negative = overlapping (see [Sequence](#sequence-configyaml)) |
| `--background FILE` | `/mnt/stick/background/scene-background.jpg` | picture behind the scene; `none` = black; black if missing (env `SCENE_BACKGROUND`) |
| `--preview GIF`, `--fps N` | – | render to an animated GIF instead of the display |

While the number is small only its rows are written, so the panel gets far
more frames than the 6 full frames/s SPI allows. Measured on hydra with the
background picture: ~16 frames/s for the rising number, ~3.5 during the
burst (it redraws the whole screen: SPI plus the blending of the fragments).
Two overlapping scenes run at the same rate.

The font is in `fonts/` (Ubuntu font family, Ubuntu Font Licence 1.0,
`fonts/LICENCE.txt`), because hydra does not have it installed.

## Sequence (config.yaml)

What hydra plays is set in `/mnt/stick/config.yaml`: a list of steps,
slideshow and scenes, played in order and then again from the top. Without
that file hydra shows the plain slideshow, as before. Example
(`config.example.yaml`):

```yaml
loop: true            # after the last step start again (false: play once)
show_args:            # options of fb-slideshow.py for the whole show
  transitions: stack
  duration: 3
  easing: ease-out
sequence:
  - scene:
      text: "140"
      repeat: 3
      travel: 8
  - slideshow:
      minutes: 10     # without minutes: forever (only as the last step)
      shuffle: true
      hold: 5
      transitions: stack
```

- `show_args` (issue #31): the options of `fb-slideshow.py` for the whole
  show, the same as on the command line; `--transitions cut` in the
  service's `DEFAULT_ARGS` and `transitions: cut` here have the same effect.
  They win over `DEFAULT_ARGS` and `make config`. Three ways to write them:

  ```yaml
  show_args:                      # name: value lines
    transitions: stack
    shuffle: true
  show_args:                      # a list, as in issue #31
    - transitions stack
    - duration: 3
    - shuffle
  show_args: --transitions stack --shuffle   # one line as on the command line
  ```

  Display options (`clock`, `gamma`, `limit`, `size`, `diagonal`, ...) can be
  set here too. They are used when the display is opened, so when they
  change, the show exits and systemd starts it again (a few seconds of
  black).
- Steps: `slideshow` (options of `fb-slideshow.py`: `minutes`, `hold`,
  `shuffle`, `transitions`, `duration`, `easing`, `fill`, `clock`, `pile`, `pile_turn`,
  `directory`) and `scene` (options of `scene140.py`: `text`, `repeat`, `travel`,
  `pause`, `explode`, `background`, `seed`, `color`, `starting`, `timing`).
  A step without options: `- scene`.
- Scenes right after each other play as one block over one background, the
  first scene's (unless a later one sets its own `background`); it stays on
  the whole time, no black in between. `timing: -3` starts a scene 3 s
  before the end of the one before (both run at once: the new number rises
  while the old fragments fade), `timing: 2` leaves 2 s of only the
  background. A step's repetitions (`repeat`) follow each other directly;
  `timing` is about its first one. Example (issue #17):

  ```yaml
  sequence:
    - scene:
        color: 30, 180, 10     # final color R,G,B
    - scene:
        timing: -3             # 3 s before the end of the previous scene
        starting: 70, 10       # percent from the left, from the bottom
    - slideshow:
        minutes: 10
  ```
- A step's options apply to that step only; options it does not set come
  from `show_args` and the service (`DEFAULT_ARGS`, `make config`). Display
  options (`gamma`, `limit`, `size`, ...) are not set per step but in
  `show_args`.
- The pictures continue where the previous slideshow step stopped. There is
  no clock over a scene.
- The file is checked while the show runs: a change takes effect after the
  current picture (or scene), without a restart. A broken file is logged
  (`make status`) and the last good sequence stays in use.

Usually: put the file on the stick (edit `/mnt/stick/config.yaml`, start
from `config.example.yaml`) and start the show:

```bash
make start                              # or make restart if it is running
make status                             # the log shows the sequence read
```

`make start` / `make restart` say nothing more when the file is fine; when
it is rejected they print the reason and fail, e.g. `error:
/mnt/stick/config.yaml: step 4 (scene): argument --starting: ... - NOT
USED, plain slideshow`. `make status` shows what was read in the log; a
broken file saved while the show runs is logged with `- keeping the previous
sequence`.
Check the file by hand on hydra:
`python3 ~/fb-slideshow/sequence.py --config /mnt/stick/config.yaml --check`.
Delete the file to go back to the plain slideshow.

Alternatively, from a file on itaka: `make sequence` uploads `config.yaml`
from the repo directory (it must exist there, `CONFIG=other.yaml` for
another file), checks it on hydra and only then **replaces**
`/mnt/stick/config.yaml`; a broken file is rejected with the reason and
nothing changes.

```bash
cp config.example.yaml config.yaml      # edit
make sequence
```

## Movie of a sequence

To check a sequence offline, `movie.py` renders a config file into a movie
(issue #23), 480x320 like the panel. It plays the sequence with the real
show code against a simulated display and clock, so steps, transitions,
scenes, the pile and the clock look as on hydra. Each frame takes about as
long as on hydra (a full frame ~0.22 s over SPI), so the movie is about as
jerky as the panel; `--smooth` renders every movie frame instead.

```bash
make movie                                      # config.yaml -> config.mp4
make movie SHOW_ARGS="--shuffle --hold 5"       # with the options set on hydra
make movie CONFIG=other.yaml MOVIE=other.mp4 MOVIE_ARGS="--smooth --seed 3"
```

- Pictures and background come from `bilder/` and `background/` in the
  repo directory (`/mnt/stick/...` is mapped there; `--stick DIR` for
  another copy of the stick).
- The options are the service defaults (`DEFAULT_ARGS`) plus `SHOW_ARGS`,
  which are not read from hydra: pass the ones set with `make config`. The
  `show_args` in the config file apply as on hydra.
- One round of the sequence (`--rounds N`); a step that never ends is cut
  after a minute (`--forever-minutes`). The clock starts at the current time
  (`--start HH:MM`) and is always shown in slideshow steps.
- Needs ffmpeg (`sudo apt install ffmpeg`); `-o name.gif` for a GIF.
  `config.yaml` and `*.mp4` are not part of the repo.

## Pictures from Nextcloud

hydra fetches the pictures itself (issue #25): the `.jpg`/`.jpeg` files in
a Nextcloud folder (by default `bilder` of the user `diashow`, an account of
its own) become an exact copy in `/mnt/stick/bilder`. New
pictures are fetched, deleted ones removed from the stick; other files there
stay untouched. Nothing listens on hydra: it pulls over WebDAV whenever it is
online (any network), with `rclone`.

- **When:** a user timer, `diashow-sync.timer`, 2 min after boot and then
  every hour. Offline, a run fails before touching the stick and the next one
  tries again. Without the stick mounted the service does not start
  (`ConditionPathIsMountPoint=/mnt/stick`), so nothing lands on the SD card.
- **Deletion guard:** a run that would delete more than 20 pictures (e.g. the
  folder was emptied or the share removed) aborts before changing anything;
  `rclone --max-delete 20` as a second safety net.
- **The show** rescans the folder every round, so new pictures appear in
  the next round at the latest.
- **Unplugging the stick:** with `x-systemd.automount` in its `/etc/fstab`
  line the stick is mounted again on the next access after plugging it in
  (the show finds its pictures by itself); without it, only at boot or with
  `sudo mount /mnt/stick`. The sync checks that the stick's file system is
  really mounted, so it never writes onto the SD card:

  ```
  UUID=6AD7-EA73  /mnt/stick  exfat  defaults,nofail,uid=1000,gid=1000,x-systemd.automount,x-systemd.device-timeout=5s  0  0
  ```
- **Where from:** the `cloud:` section of `config.yaml` on the stick: the
  public address, the user and the folder (no password):

  ```yaml
  cloud:
    address: https://cloud.example.org
    user: diashow
    folder: bilder        # default
  ```

  It wins over `DIASHOW_SRC` in the service; without it, the folder of
  `DIASHOW_SRC` with the address stored by `make diashow-setup` is used. A
  broken `cloud:` section stops the sync (nothing done) and is reported by
  `make start` like any other error in the file.
- **Login:** the app password of `diashow` (revocable in Nextcloud under
  Settings → Security), stored obscured in `~/.config/rclone/rclone.conf` on
  hydra as the remote `nc`. The Nextcloud address and user are kept there as
  well, not in this repo: the first `make diashow-setup` asks for them (or
  takes `DIASHOW_URL` / `DIASHOW_USER`); later runs for a new app password
  reuse them.

```bash
make diashow-setup     # once / new app password: asks for it on hydra (first time also the address), enables the timer
make diashow-sync      # fetch now, with the log of the run
make status            # "picture sync: sync ok: 59 pictures (0 new, 0 deleted) (next: ...)"
```

The fallback folder and the target are set in `ci/diashow-sync.service`
(`DIASHOW_SRC=nc:bilder`, `DIASHOW_DEST`, `DIASHOW_CONFIG`). rclone itself: `sudo apt install
rclone` on hydra. On hydra: `systemctl --user list-timers`,
`journalctl --user-unit diashow-sync`.

### Adding pictures from a phone (upload only)

Others add pictures through a Nextcloud **File drop** link (issue #27). Anyone
with the link and its password can upload into `bilder`. They cannot see,
change or delete what is already there, and they don't need an account or an
app, only the phone's browser. Uploads reach the show with the next sync, so
within an hour.

The link has its own password. Don't hand out an app password for this: an
app password gives full access to the `diashow` account, including deleting
pictures.

Set it up once in the Nextcloud web interface, logged in as `diashow`:

1. Files → `bilder` → share icon → **Create a new share link**.
2. In the link's settings (⋯ → *Customize link*):
   - permission **File drop (upload only)**;
   - **Set password**, a separate one used only for uploading;
   - no expiration date (or set one);
   - **Save**.
3. Copy the link and send link and password to the people who should add
   pictures, together with the guide
   <https://fritzthekid.github.io/fb-slideshow/bilder-hochladen.html> (German,
   source `docs/bilder-hochladen.html`).
   It explains the upload step by step for a phone. Nextcloud can also show
   the link as a QR code (⋯ → *Show QR code*), which phone cameras read
   directly.

To change the password, edit it in the same link settings; the link stays
the same. To lock everybody out, delete the link. Several links with
different passwords (e.g. one per family) are possible, and each can be
revoked on its own. Wrong passwords are slowed down by Nextcloud's
brute-force protection.

Notes:

- **Everything uploaded goes straight onto the show.** There is no review
  step. Remove an unwanted picture in `bilder` as `diashow`, and the next sync
  removes it from the stick as well.
- **Only `.jpg`/`.jpeg` files are synced**; other files stay in Nextcloud. When
  a phone uploads through the browser it normally sends JPEG, but an iPhone
  set to keep originals may send HEIC.
- **Large phone photos are fine.** The show scales them down while loading
  and applies their stored rotation. The upload limit is that of the
  Nextcloud server (`PHP_UPLOAD_LIMIT`, 512 MB by default in the docker image).

## Pause and exit

Pause and resume (toggles on every signal, the current image stays):

```bash
pkill -USR1 -f fb-slideshow.py
# or targeted:
kill -USR1 <PID>
```

Exit: `SIGTERM` (`pkill -f fb-slideshow.py`) or Ctrl+C. The display is
set to black.

When the show runs as a service (see below), prefer `make pause` /
`make resume` / `make stop` from itaka, or `systemctl --user stop fb-slideshow`
on hydra.

## Tests

```bash
python3 -m pytest -q
```

## Deployment and CI

The show runs on hydra as a systemd **user** service (`fb-slideshow.service`,
running `sequence.py`) and starts at boot (linger is enabled for the user, no `sudo` needed). It is
restarted automatically if it fails, e.g. while `/dev/fb1` is not ready yet
after boot. Logs go to the journal, which is kept in RAM on hydra.

Everything is driven from itaka with `make` (ssh/scp to hydra):

hydra is reached at the first address that answers over ssh: the WireGuard
VPN address `192.168.4.203` (works from any network), then the name `hydra`
(`/etc/hosts`, home LAN), then the Wi-Fi address `192.168.4.142`
(`HYDRA_CANDIDATES` in the Makefile). `make hydra-host` shows which one is
used; `make deploy HYDRA=<address>` forces one.

| Command | What it does |
|---|---|
| `make test` | tests on itaka, with hydra's Python / numpy / Pillow versions (`requirements-ci.txt`) |
| `make deploy` | copy to `hydra:~/fb-slideshow` and install/update the service |
| `make test-hydra` | tests on hydra, version check, verify the service file |
| `make start` / `stop` / `restart` | control the service; start and restart fail with the reason when `/mnt/stick/config.yaml` is rejected (NOT USED) |
| `make pause` / `resume` | pause or resume (`SIGUSR1`), the image stays |
| `make status` | state, boot setting, options, recent log |
| `make config SHOW_ARGS="--shuffle --hold 20"` | set the show's options on top of the service defaults, replacing the previously saved ones (kept across reboots and deploys; `SHOW_ARGS=` goes back to the service defaults). Invalid options are rejected and nothing changes |
| `make sequence` | upload the local `config.yaml` (`CONFIG=` another file), check it on hydra and make it the sequence, replacing `/mnt/stick/config.yaml`; see [Sequence](#sequence-configyaml) |
| `make scene` / `make scene-preview` | play the scene on hydra (the show stays stopped) / render it to a GIF |
| `make diashow-setup` / `make diashow-sync` | set up the picture sync from Nextcloud (asks for the app password on hydra) / fetch now, see [Pictures from Nextcloud](#pictures-from-nextcloud) |
| `make movie` | the sequence of `config.yaml` as a movie (`config.mp4`), offline, see [Movie of a sequence](#movie-of-a-sequence) |
| `make measure-fps` | frame rate of transitions on the real panel (stops the show meanwhile) |
| `make ci` | test + deploy + test-hydra + restart |
| `make hydra-host` | which address of hydra is used |

GitHub Actions runs the same steps on a self-hosted runner on itaka: the
tests on every push to a branch, and on pushes to `main` also deploy and
restart. There is no `pull_request` trigger, and the repo settings
(Settings → Actions → General → *Approval for running fork pull request
workflows from contributors*: **all external contributors**) keep
pull requests from forks off itaka. A fork could otherwise bring its own
workflow and run code on itaka, which can reach hydra. The show can also be controlled from GitHub:

```bash
gh workflow run show.yml -f action=pause
gh workflow run show.yml -f action=config -f show_args="--shuffle"
```

On hydra itself: `systemctl --user status fb-slideshow`,
`journalctl --user-unit fb-slideshow`.

The service file in the repo (`ci/fb-slideshow.service`) is installed
unchanged; `make deploy` copies it and reloads systemd when it changed. By
hand on hydra:

```bash
cp ~/fb-slideshow/ci/fb-slideshow.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart fb-slideshow.service
```

## License

Copyright 2026 Eduard Moser

Licensed under the [Apache License, Version 2.0](LICENSE).

Exception: the font in `fonts/` (Ubuntu font family, Copyright Canonical Ltd.)
is under the Ubuntu Font Licence 1.0, see `fonts/LICENCE.txt`.
