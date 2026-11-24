#!/bin/sh
# Lets other users on the Pi control the show with "show <command>"
# (show status, show restart, ...). Run once on hydra as root:
#   make show-setup USERS="richard"     (from itaka)
#   sudo sh ~/fb-slideshow/ci/setup-show.sh richard
# More users later: sudo usermod -aG slideshow <name> (then log in again).
#
# Installs:
#   /usr/local/bin/show                ci/show, runs show.sh as its owner
#   /etc/sudoers.d/fb-slideshow-show   members of the group "slideshow" may
#                                      run show.sh (only that) as the owner,
#                                      without a password
# and adds the given users to the group "slideshow".
#
# The owner is the account the show runs under (owner of show.sh). Running it
# again is harmless (updates both files, keeps the group).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT=$HERE/show.sh
GROUP=${SHOW_GROUP:-slideshow}
BIN=${SHOW_BIN:-/usr/local/bin/show}
RULE=${SHOW_RULE:-/etc/sudoers.d/fb-slideshow-show}
OWNER=${SHOW_OWNER:-$(stat -c %U "$SCRIPT")}
VISUDO=${VISUDO:-visudo}
INSTALL_OWNER=${INSTALL_OWNER--o root -g root}  # empty: tests

if [ "${SHOW_TEST:-}" != 1 ] && [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo sh $0 <user> ..." >&2
    exit 1
fi

if [ "${SHOW_TEST:-}" != 1 ]; then
    groupadd -f "$GROUP"
    for u in "$@"; do
        usermod -aG "$GROUP" "$u"
        echo "$u -> group $GROUP (active after the next login)"
    done
fi

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

# shellcheck disable=SC2086  # INSTALL_OWNER is two options
sed -e "s|@SCRIPT@|$SCRIPT|" -e "s|@OWNER@|$OWNER|" "$HERE/show" >"$tmp"
install -m 755 $INSTALL_OWNER "$tmp" "$BIN"

# checked before it is installed: a broken file in sudoers.d breaks sudo
printf '%%%s ALL=(%s) NOPASSWD: %s\n' "$GROUP" "$OWNER" "$SCRIPT" >"$tmp"
"$VISUDO" -cqf "$tmp"
# shellcheck disable=SC2086
install -m 440 $INSTALL_OWNER "$tmp" "$RULE"

echo "installed $BIN and $RULE (group $GROUP runs show.sh as $OWNER)"
