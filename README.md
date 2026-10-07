# Frametanium for the Steam Frame

Installs and updates Steam Frame apps and ports straight from their GitHub releases, like Obtainium: add any repo,
and Frametanium keeps it up to date. Works from the headset or a browser.

- **Add any GitHub repo** by typing `owner/name` (or pasting a link) on the headset's on-screen keyboard, from the
  browser page, or by picking a suggestion made for the Frame. The built-in ports (Ship of Harkinian, 2 Ship 2
  Harkinian) are there from the start.
- **Picks the right download:** ARM64 Linux builds (`aarch64`/`arm64`, "steamframe"); checksums, Windows, macOS,
  Android and x86 files are passed over. You can choose another one when adding. Zips, tarballs (gz/xz/bz2/zst),
  AppImages and single programs all work.
- **Two ways to install,** picked from the download unless you choose:
  - *Library app:* unpacked into `~/devkit-game/<name>`, given a `run.sh` (its own, or one that starts the ARM64
    program it finds), added to the Steam library with art (the repo's GitHub card and the owner's avatar).
  - *Its installer:* when the download has `install.sh` (or `contrib/install.sh`, `setup.sh`), it's unpacked into
    `~/.local/share/frametanium/apps/<id>` and that script runs, for tools such as SteamVR overlays. Removing the app
    runs its uninstaller when it has one.
- **Install, update, update all, or pick any version.** Pre-releases are skipped unless you turn them on for an app
  (they're on for apps that only have pre-releases).
- **Keeps your stuff:** saves live outside the install folder, ROMs placed next to `run.sh` are carried over, and
  the previous install is kept for a **one-step rollback**.
- Won't touch an app that's running. Only add apps you trust: their programs and installers run as your user.

## Use it
- **In the headset:** open **Frametanium** in the library. D-pad to choose, **A** open an app (install/update,
  versions, previous version, pre-releases, remove), **Y** add an app, **Select** update all, **Start** check for
  updates, **B** back/close.
- **From a phone or PC:** `http://<frame-ip>:8643` (add apps, edit name/install style/program, remove).

Updates are checked every 6 hours and on demand.

## Files
- `catalog.json`: the built-in ports.
- `suggestions.json`: apps offered on the Add screen.
- `~/.config/frametanium/catalog.json` on the Frame: the apps you added and changes to built-in ones
  (`{"id": "soh", "hidden": true}` hides one; adding its repo again brings it back).
- `~/.config/frametanium/installed.json`: installed versions.

## Private repos
GitHub only shows releases of private repos to signed-in users. Either make the port repos public (they contain no
ROMs), or save a token in the web page's Settings: a fine-grained token limited to those repos with read-only
**Contents** access is enough. It's stored in `~/.config/frametanium/config.json` (mode 600) and never sent back
to the page.

## Install
From a computer that can SSH into the Frame:

```sh
app/build-arm64.sh       # cross-compiles the headset app into dist/app
./deploy.sh [host]       # host defaults to frame.local
```

Service only, on the Frame: copy this folder over and run `sh install.sh`.
Uninstall: `systemctl --user disable --now frametanium.service` and remove Frametanium from the library.

## Notes
- The service listens on port 8643 on your network. Set `password` in `~/.config/frametanium/config.json` to require
  one in the browser (the headset app connects locally and doesn't need it).
- Standard-library Python only. The headset app is C + SDL2 with the public-domain
  [font8x8](https://github.com/dhepper/font8x8).
- Testing on another architecture: `FRAMETANIUM_EXEC_WRAPPER=qemu-aarch64-static` (with `QEMU_LD_PREFIX`) runs
  ARM64 AppImages for unpacking; `FRAMETANIUM_GAMES` changes the install folder.
