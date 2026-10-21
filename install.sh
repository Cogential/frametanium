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
if [ -f "$here/VERSION" ]; then cp "$here/VERSION" "$dest/"; else rm -f "$dest/VERSION"; fi
rm -rf "$dest/art" && cp -r "$here/art" "$dest/art"
cp "$here/frametanium.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable frametanium.service >/dev/null 2>&1

# The headset app, when the bundle has it (releases and deploy.sh do): files are replaced rather than overwritten,
# so a running copy keeps going until it's closed.
if [ -x "$here/app/frametanium" ]; then
    game="$HOME/devkit-game/Frametanium"
    mkdir -p "$game"
    rm -rf "$game/frametanium" "$game/run.sh" "$game/lib"
    cp -r "$here/app/." "$game/"
    chmod +x "$game/frametanium" "$game/run.sh"
    # The app used to be called Port Manager: remove its old folder and library tile.
    if [ -e "$HOME/devkit-game/Port_Manager" ]; then
        python3 "$HOME/devkit-utils/steamos-delete" --delete-title Port_Manager >/dev/null 2>&1 || true
    fi
    if [ -f "$HOME/devkit-utils/steam-client-create-shortcut" ]; then
        python3 "$HOME/devkit-utils/steam-client-create-shortcut" --parms "{\"gameid\": \"Frametanium\", \"directory\": \"$game\", \"argv\": [\"./run.sh\"], \"env\": {}, \"settings\": {\"compat_tool\": \"SteamLinuxRuntime_4-arm64\", \"steam_play\": \"0\"}}" >/dev/null 2>&1 || true
        # Library art, without replacing art already there.
        appid=""
        for i in 1 2 3 4 5 6 7 8 9 10; do
            appid=$(python3 "$here/tools/find_appid.py" Frametanium 2>/dev/null) && [ -n "$appid" ] && break
            sleep 2
        done
        if [ -n "$appid" ]; then
            for grid in "$HOME"/.local/share/Steam/userdata/[1-9]*/config; do
                [ -d "$grid" ] || continue
                mkdir -p "$grid/grid"
                for pair in portrait.png:p.png wide.png:.png hero.png:_hero.png logo.png:_logo.png icon.png:_icon.png; do
                    [ -e "$grid/grid/$appid${pair##*:}" ] || cp "$here/art/app/${pair%%:*}" "$grid/grid/$appid${pair##*:}"
                done
            done
        fi
    fi
fi

# When Frametanium updates itself it runs this script from the service, and restarting the service now would stop
# this script and the service before they finish: restart it a few seconds later from a unit of its own instead.
if [ "$(systemctl --user show -p MainPID --value frametanium.service 2>/dev/null)" = "$PPID" ]; then
    systemd-run --user --on-active=3 --collect systemctl --user restart frametanium.service >/dev/null
    echo "Frametanium restarts in a few seconds."
    exit 0
fi
systemctl --user restart frametanium.service
sleep 1
ip=$(python3 -c 'import socket;s=socket.socket(2,2);s.connect(("192.0.2.1",9));print(s.getsockname()[0])' 2>/dev/null || hostname)
echo "Frametanium is running: http://$ip:8643"
echo "Uninstall: systemctl --user disable --now frametanium.service"
