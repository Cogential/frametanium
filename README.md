# Port Manager for the Steam Frame

Installs and updates native Steam Frame ports straight from their GitHub releases, from the headset or a browser.

- **Install, update, or pick any version** of each port in the catalog. Pre-releases count.
- **Keeps your stuff:** saves live outside the install folder, and ROMs placed next to `run.sh` are carried over.
- **One-step rollback** to the version you had before (the previous install is kept).
- Fixes executable bits, unpacks AppImages (no FUSE needed), writes a launcher when a port needs one,
  registers the game with Steam (`steam-client-create-shortcut`, Steam Linux Runtime 4 arm64) and adds library art.
- Won't touch a game that's running.

## Use it
- **In the headset:** open **Port Manager** in the library. D-pad to choose, **A** install/update, **Y** choose a
  version, **X** previous version, **Start** check for updates, **B** close.
- **From a phone or PC:** `http://<frame-ip>:8643`.

Updates are checked every 6 hours and on demand.

## Catalog
`catalog.json` lists the built-in ports:

| Port | Repo | Download |
|---|---|---|
| Ocarina of Time (Ship of Harkinian) | Cogential/Shipwright-frame | `soh-steam-frame-arm64.zip` |
| Majora's Mask (2 Ship 2 Harkinian) | Cogential/2ship2harkinian-frame | `*arm64.appimage` (unpacked, launcher written) |

Add your own in `~/.config/port-manager/catalog.json` on the Frame (same fields; the same `id` overrides a built-in
entry). A port's release needs a zip whose top folder holds `run.sh`, or an AppImage plus a `run_sh` launcher in the
catalog.

## Private repos
GitHub only shows releases of private repos to signed-in users. Either make the port repos public (they contain no
ROMs), or save a token in the web page's Settings: a fine-grained token limited to those repos with read-only
**Contents** access is enough. It's stored in `~/.config/port-manager/config.json` (mode 600) and never sent back
to the page.

## Install
From a computer that can SSH into the Frame:

```sh
app/build-arm64.sh       # cross-compiles the headset app into dist/app
./deploy.sh [host]       # host defaults to frame.local
```

Service only, on the Frame: copy this folder over and run `sh install.sh`.
Uninstall: `systemctl --user disable --now frame-port-manager.service` and remove Port Manager from the library.

## Notes
- The service listens on port 8643 on your network. Set `password` in `~/.config/port-manager/config.json` to require
  one in the browser (the headset app connects locally and doesn't need it).
- Standard-library Python only. The headset app is C + SDL2 with the public-domain
  [font8x8](https://github.com/dhepper/font8x8).
- Testing on another architecture: `PORT_MANAGER_EXEC_WRAPPER=qemu-aarch64-static` (with `QEMU_LD_PREFIX`) runs
  ARM64 AppImages for unpacking; `PORT_MANAGER_GAMES` changes the install folder.
