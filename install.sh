#!/bin/sh
# Installs the Frametanium service on a Steam Frame for the current user. Run it on the Frame from this folder.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.local/share/frametanium/service"
mkdir -p "$HOME/.config/systemd/user"

# Frametanium used to be called Port Manager: stop the old service and carry its settings, installed
# versions and added apps over to the new folders.
if [ -f "$HOME/.config/systemd/user/frame-port-manager.service" ]; then
    systemctl --user disable --now frame-port-manager.service >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/frame-port-manager.service"
fi
rm -rf "$HOME/.local/share/frame-port-manager" "$HOME/.cache/port-manager"
for base in "$HOME/.config" "$HOME/.local/share"; do
    if [ -d "$base/port-manager" ] && [ ! -e "$base/frametanium" ]; then
        mv "$base/port-manager" "$base/frametanium"
    fi
done

mkdir -p "$dest"
cp "$here/manager.py" "$here/page.html" "$here/catalog.json" "$here/suggestions.json" "$dest/"
rm -rf "$dest/art" && cp -r "$here/art" "$dest/art"
cp "$here/frametanium.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable frametanium.service >/dev/null 2>&1
systemctl --user restart frametanium.service
sleep 1
ip=$(python3 -c 'import socket;s=socket.socket(2,2);s.connect(("192.0.2.1",9));print(s.getsockname()[0])' 2>/dev/null || hostname)
echo "Frametanium is running: http://$ip:8643"
echo "Uninstall: systemctl --user disable --now frametanium.service"
