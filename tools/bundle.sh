#!/bin/sh
# Puts together what install.sh installs: tools/bundle.sh <built headset app folder> <out folder>
# (the app folder is what app/build-arm64.sh makes: frametanium, run.sh, lib/).
set -e
cd "$(dirname "$0")/.."
APPDIR="$1" OUT="$2"
[ -x "$APPDIR/frametanium" ] || { echo "No built headset app in $APPDIR" >&2; exit 1; }
rm -rf "$OUT" && mkdir -p "$OUT/tools"
cp manager.py page.html catalog.json suggestions.json frametanium.service install.sh README.md "$OUT/"
cp tools/find_appid.py "$OUT/tools/"
cp -r art "$OUT/art"
cp -r "$APPDIR" "$OUT/app"
