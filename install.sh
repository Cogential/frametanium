#!/bin/sh
# Installs the Port Manager service on a Steam Frame for the current user. Run it on the Frame from this folder.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.local/share/frame-port-manager"
mkdir -p "$dest" "$HOME/.config/systemd/user"
cp "$here/manager.py" "$here/page.html" "$here/catalog.json" "$here/suggestions.json" "$dest/"
rm -rf "$dest/art" && cp -r "$here/art" "$dest/art"
cp "$here/frame-port-manager.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable frame-port-manager.service >/dev/null 2>&1
systemctl --user restart frame-port-manager.service
sleep 1
ip=$(python3 -c 'import socket;s=socket.socket(2,2);s.connect(("192.0.2.1",9));print(s.getsockname()[0])' 2>/dev/null || hostname)
echo "Port Manager is running: http://$ip:8643"
echo "Uninstall: systemctl --user disable --now frame-port-manager.service"
