#!/bin/sh
# Installs or updates Frametanium on a Steam Frame from this computer over SSH: the service, and the
# "Frametanium" app in the Steam library with its art. (Once it's installed, it can also update itself from
# its GitHub releases.)
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

[ -x dist/app/frametanium ] || { echo "Build the app first: app/build-arm64.sh"; exit 1; }
$SSH "$USER_AT" true || { echo "Can't reach $HOST over SSH. Is the Frame on and on this network?"; exit 1; }

tools/bundle.sh dist/app dist/bundle
$SSH "$USER_AT" 'rm -rf ~/.cache/frametanium-src && mkdir -p ~/.cache/frametanium-src'
rsync -a -e "$SSH" dist/bundle/ "$USER_AT:.cache/frametanium-src/"
$SSH "$USER_AT" 'sh ~/.cache/frametanium-src/install.sh'
echo "Done. Open \"Frametanium\" from the library, or http://$HOST:8643 in a browser."
