# Frametanium for the Steam Frame

Installs and updates Steam Frame apps and ports straight from their GitHub releases, like Obtainium: add any repo,
and Frametanium keeps it up to date. Works from the headset or a browser.

- **Add any GitHub repo** by typing `owner/name` (or pasting a link) on the headset's on-screen keyboard, from the
  browser page, or by picking a suggestion made for the Frame. The built-in ports (Ship of Harkinian, 2 Ship 2
  Harkinian, Starship, Lighthouse, OpenCE) are there from the start.
- **Discover new Frame apps:** every 12 hours (or on demand) it searches GitHub for Steam Frame projects (the
  `steam-frame` topic, "steam frame" in names and descriptions, and the community's
  [awesome-steam-frame](https://github.com/kosantosbik/awesome-steam-frame) list) and shows the ones whose latest
  release has a download it can install, with ones you haven't seen marked **New**. Pick one to look at and add it,
  or hide it. Without a GitHub token, GitHub allows 60 lookups an hour, so each search checks a batch (keeping some
  for update checks) and the next search carries on; results are cached and only rechecked when a repo changes.
- **Picks the right download:** ARM64 Linux builds (`aarch64`/`arm64`, "steamframe"); checksums, Windows, macOS,
  Android and x86 files are passed over. You can choose another one when adding. Zips, tarballs (gz/xz/bz2/zst),
  AppImages and single programs all work.
- **Two ways to install,** picked from the download unless you choose:
  - *Library app:* unpacked into `~/devkit-game/<name>`, given a `run.sh` (its own, or one that starts the ARM64
    program it finds), added to the Steam library with art (the repo's GitHub card, and the project's icon or the owner's avatar),
    fetched when it's installed.
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
  versions, previous version, pre-releases, remove), **Y** add an app, **X** discover new apps, **Select** update all, **Start** check for
  updates, **B** back/close.
- **From a phone or PC:** `http://<frame-ip>:8643` (add apps, edit name/install style/program, remove).

Updates are checked every 6 hours, each time you open the headset app, and on demand.

**Frametanium updates itself:** when a check finds a newer [release](https://github.com/Cogential/frametanium/releases)
of Frametanium, it installs it without asking (unless something else is installing; the next check tries again):
it reinstalls the service and the headset app and restarts the service a few seconds later. The headset app then
says to close and reopen it to get the new version. Turn this off with `"self_update": false` in
`~/.config/frametanium/config.json` (you can still update it from its row in the list). Its version comes from the
`VERSION` file the bundle carries (`tools/bundle.sh`; releases set it from the tag).

## Files
- `catalog.json`: the built-in ports.
- `suggestions.json`: apps offered on the Add screen.
- `~/.config/frametanium/discover.json`: discovered projects you've seen or hidden; `~/.cache/frametanium/discover.json`
  caches what each search found.
- `~/.config/frametanium/catalog.json` on the Frame: the apps you added and changes to built-in ones
  (`{"id": "soh", "hidden": true}` hides one; adding its repo again brings it back).
- `~/.config/frametanium/installed.json`: installed versions.

## Private repos
GitHub only shows releases of private repos to signed-in users. Either make the port repos public (they contain no
ROMs), or save a token in the web page's Settings: a fine-grained token limited to those repos with read-only
**Contents** access is enough. It's stored in `~/.config/frametanium/config.json` (mode 600) and never sent back
to the page.

## Install
On the Frame: download `frametanium-steam-frame-arm64.tar.gz` from the latest release, unpack it and run
`sh install.sh`. It installs the service and adds the Frametanium app to the library.

Or from a computer that can SSH into the Frame:

```sh
app/build-arm64.sh       # cross-compiles the headset app into dist/app
./deploy.sh [host]       # host defaults to frame.local
```

Releases: push a tag `v*`; `.github/workflows/release.yml` builds the headset app on an ARM64 runner and publishes
the bundle (`tools/bundle.sh`).
Uninstall: `systemctl --user disable --now frametanium.service` and remove Frametanium from the library.

## Notes
- The service listens on port 8643 on your network, and every request needs a secret: the browser page asks for the
  password (made up on first start and shown under the list in the headset app; change `password` in
  `~/.config/frametanium/config.json` if you like), and the headset app sends `app_key` from the same file (mode 600).
  Requests addressed to unknown host names or sent from other sites' pages are refused.
- Standard-library Python only. The headset app is C + SDL2 with the public-domain
  [font8x8](https://github.com/dhepper/font8x8).
- Testing on another architecture: `FRAMETANIUM_EXEC_WRAPPER=qemu-aarch64-static` (with `QEMU_LD_PREFIX`) runs
  ARM64 AppImages for unpacking; `FRAMETANIUM_GAMES` changes the install folder.
