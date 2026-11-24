#!/bin/sh
# Fetches the slideshow pictures from Nextcloud onto the stick (issue #25):
# the .jpg/.jpeg files of $SRC become an exact copy in $DEST (new ones are
# fetched, deleted ones removed; other files on the stick stay untouched).
#
# Deletion guard: if a run would delete more than $MAX_DELETE pictures (e.g.
# the share was emptied or removed), it aborts BEFORE changing anything.
# --max-delete is passed to rclone as well, as a second safety net.
#
# Run by diashow-sync.service (timer: 2 min after boot, then hourly); offline
# it fails before touching the stick, the next run tries again.
#
# Only one way: cloud -> stick. Nothing is uploaded; a picture that is only
# on the stick (not in the cloud folder) is deleted from the stick.
#
# Off unless config.yaml on the stick says so (default: nothing is fetched):
#   cloud:
#     address: https://cloud.example.org   # public address
#     user: diashow
#     folder: bilder
#     pull: true
# The password is only in rclone.conf on the SD card (make diashow-setup).
# With DIASHOW_CONFIG empty, config.yaml is not read and $DIASHOW_SRC is
# fetched (tests, by hand).
set -euf  # -f: the filter patterns are not file names

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${DIASHOW_SRC:-nc:bilder}
CONFIG=${DIASHOW_CONFIG-/mnt/stick/config.yaml}  # empty: not read
PYTHON=${PYTHON:-python3}
DEST=${DIASHOW_DEST:-/mnt/stick/bilder}
MAX_DELETE=${DIASHOW_MAX_DELETE:-20}
RCLONE=${RCLONE:-rclone}
STICK=${DIASHOW_STICK-/mnt/stick}  # empty: no check (tests)

# The stick must really be there. With x-systemd.automount in /etc/fstab,
# /mnt/stick is always a mount point (autofs), even without the stick; the
# access below mounts it if it is plugged in, otherwise only autofs is left.
if [ -n "$STICK" ]; then
    ls "$STICK" >/dev/null 2>&1 || true
    fstype=$(findmnt -n -o FSTYPE -M "$STICK" 2>/dev/null | tail -n 1)
    if [ -z "$fstype" ] || [ "$fstype" = autofs ]; then
        echo "error: no stick mounted on $STICK - nothing done" >&2
        exit 1
    fi
fi

# cloud: from config.yaml (rclone reads RCLONE_CONFIG_NC_* as settings of the
# remote "nc", in front of rclone.conf)
if [ -n "$CONFIG" ]; then
    rc=0
    cloud=$("$PYTHON" "$HERE/../sequence.py" --config "$CONFIG" --cloud) || rc=$?
    if [ "$rc" -eq 1 ]; then
        echo "pull-down off: no cloud: section in $CONFIG - nothing done"
        exit 0
    elif [ "$rc" -ne 0 ]; then
        echo "error: cloud: section in $CONFIG not usable - nothing done" >&2
        exit 1
    fi
    if [ "$(printf '%s\n' "$cloud" | sed -n 4p)" != true ]; then
        echo "pull-down off (cloud: pull: true in $CONFIG switches it on) - nothing done"
        exit 0
    fi
    RCLONE_CONFIG_NC_URL=$(printf '%s\n' "$cloud" | sed -n 1p)
    RCLONE_CONFIG_NC_USER=$(printf '%s\n' "$cloud" | sed -n 2p)
    export RCLONE_CONFIG_NC_URL RCLONE_CONFIG_NC_USER
    SRC="nc:$(printf '%s\n' "$cloud" | sed -n 3p)"
    echo "cloud from $CONFIG: $RCLONE_CONFIG_NC_URL $SRC"
fi

# only pictures in the folder itself, any case (.jpg .JPG .jpeg ...)
FILTER="--include *.{jpg,jpeg} --ignore-case --max-depth 1"
NET="--contimeout 30s --timeout 2m --retries 3 --low-level-retries 5"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# shellcheck disable=SC2086  # options are meant to be split
if ! $RCLONE lsf "$SRC" --files-only $FILTER $NET >"$tmp/src"; then
    echo "error: cannot list $SRC (offline?) - stick unchanged" >&2
    exit 1
fi
mkdir -p "$DEST"
find "$DEST" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' \) \
    -exec basename {} \; >"$tmp/dest"
sort -o "$tmp/src" "$tmp/src"
sort -o "$tmp/dest" "$tmp/dest"
gone=$(comm -13 "$tmp/src" "$tmp/dest" | wc -l)
new=$(comm -23 "$tmp/src" "$tmp/dest" | wc -l)
if [ "$gone" -gt "$MAX_DELETE" ]; then
    echo "error: would delete $gone of $(wc -l <"$tmp/dest") pictures" \
         "(more than $MAX_DELETE; $SRC has $(wc -l <"$tmp/src")) - aborted," \
         "stick unchanged" >&2
    exit 1
fi
# shellcheck disable=SC2086
if ! $RCLONE sync "$SRC" "$DEST" $FILTER $NET --max-delete "$MAX_DELETE" \
        --stats 0 -v; then
    echo "error: sync incomplete ($SRC -> $DEST), next run tries again" >&2
    exit 1
fi
echo "sync ok: $(wc -l <"$tmp/src") pictures ($new new, $gone deleted)"
