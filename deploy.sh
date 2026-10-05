#!/bin/sh
# Installs or updates Port Manager on a Steam Frame from this computer over SSH: the service, and the
# "Port Manager" app in the Steam library with its art.
#
# Usage: ./deploy.sh [host]        host defaults to frame.local
# Env:   SSH_KEY (default ~/.ssh/frame_devkit), SSH_USER (default steamos)
# Build the app first with app/build-arm64.sh.
set -e
cd "$(dirname "$0")"
HOST="${1:-frame.local}"
KEY="${SSH_KEY:-$HOME/.ssh/frame_devkit}"
USER_AT="${SSH_USER:-steamos}@$HOST"
SSH="ssh -i $KEY -o ConnectTimeout=10"
GAMEID=Port_Manager

[ -x dist/app/port-manager ] || { echo "Build the app first: app/build-arm64.sh"; exit 1; }
$SSH "$USER_AT" true || { echo "Can't reach $HOST over SSH. Is the Frame on and on this network?"; exit 1; }

echo "== Service"
$SSH "$USER_AT" 'rm -rf ~/.cache/frame-port-manager-src && mkdir -p ~/.cache/frame-port-manager-src'
rsync -a -e "$SSH" manager.py page.html catalog.json frame-port-manager.service install.sh art "$USER_AT:.cache/frame-port-manager-src/"
$SSH "$USER_AT" 'sh ~/.cache/frame-port-manager-src/install.sh'

echo "== Headset app"
rsync -a --delete -e "$SSH" dist/app/ "$USER_AT:devkit-game/$GAMEID/"
$SSH "$USER_AT" "python3 ~/devkit-utils/steam-client-create-shortcut --parms '{\"gameid\": \"$GAMEID\", \"directory\": \"/home/steamos/devkit-game/$GAMEID\", \"argv\": [\"./run.sh\"], \"env\": {}, \"settings\": {\"compat_tool\": \"SteamLinuxRuntime_4-arm64\", \"steam_play\": \"0\"}}'" >/dev/null

echo "== Library art"
APPID=""
for i in 1 2 3 4 5 6 7 8 9 10; do
    APPID=$($SSH "$USER_AT" "python3 - $GAMEID" < tools/find_appid.py) && [ -n "$APPID" ] && break
    sleep 2
done
if [ -n "$APPID" ]; then
    for USERDIR in $($SSH "$USER_AT" 'ls -d ~/.local/share/Steam/userdata/[1-9]*/config 2>/dev/null'); do
        $SSH "$USER_AT" "mkdir -p $USERDIR/grid"
        for pair in portrait.png:p.png wide.png:.png hero.png:_hero.png logo.png:_logo.png icon.png:_icon.png; do
            rsync -a -e "$SSH" "art/app/${pair%%:*}" "$USER_AT:$USERDIR/grid/${APPID}${pair##*:}"
        done
    done
    echo "Art added for app id $APPID (shows after Steam restarts)."
else
    echo "Couldn't find the shortcut's app id yet; run this again later to add the art."
fi
echo "Done. Open \"Port Manager\" from the library, or http://$HOST:8643 in a browser."
