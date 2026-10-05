#!/usr/bin/env python3
"""Port Manager: installs and updates native Steam Frame ports from their GitHub releases.

A small web service (standard library only) that runs on the Frame. It reads each port's releases, downloads the
chosen one, unpacks it into ~/devkit-game/<gameid>, keeps ROMs and saves, fixes executable bits, registers the
game with Steam and gives it library art. The previous install is kept for a one-step rollback.

Control it from the headset app (app/) or from a browser at http://<frame>:8643.
"""

import fnmatch
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "port-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
INSTALLED_FILE = CONFIG_DIR / "installed.json"
USER_CATALOG = CONFIG_DIR / "catalog.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "port-manager"
GAMES_DIR = Path(os.environ.get("PORT_MANAGER_GAMES", HOME / "devkit-game"))
CREATE_SHORTCUT = HOME / "devkit-utils" / "steam-client-create-shortcut"
STEAM_USERDATA = HOME / ".local/share/Steam/userdata"

DEFAULT_CONFIG = {
    "port": 8643,
    "password": "",       # for the web page; empty means anyone on the network can use it
    "github_token": "",   # needed only for private repos (read-only access to their releases is enough)
    "compat_tool": "SteamLinuxRuntime_4-arm64",
    "check_hours": 6,
}
CHUNK = 1024 * 1024
RELEASES_TTL = 600


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def config():
    return {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}


def catalog():
    """The built-in ports, plus the user's own entries (same id replaces a built-in one)."""
    ports = {p["id"]: p for p in load_json(APP_DIR / "catalog.json", [])}
    for p in load_json(USER_CATALOG, []):
        if "id" in p:
            ports[p["id"]] = {**ports.get(p["id"], {}), **p}
    return ports


# ---------------------------------------------------------------- GitHub


def gh_request(url, accept="application/vnd.github+json"):
    headers = {"Accept": accept, "User-Agent": "frame-port-manager", "X-GitHub-Api-Version": "2022-11-28"}
    token = config().get("github_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def gh_json(url):
    try:
        with urllib.request.urlopen(gh_request(url), timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("Repo not found. If it's private, add a GitHub token in Settings.") from e
        if e.code in (401, 403):
            raise RuntimeError("GitHub refused the request (token or rate limit). Try again later.") from e
        raise RuntimeError(f"GitHub error {e.code}") from e
    except OSError as e:
        raise RuntimeError("Can't reach GitHub. Is the Frame online?") from e
    except ValueError as e:  # e.g. a token with a newline or other characters a header can't hold
        raise RuntimeError("The GitHub token isn't valid. Check it in Settings.") from e


_releases_cache = {}


def releases(port, force=False):
    """Releases newest first, with the asset that matches the port's pattern."""
    key = port["repo"]
    hit = _releases_cache.get(key)
    if hit and not force and time.time() - hit[0] < RELEASES_TTL:
        return hit[1]
    out = []
    for rel in gh_json(f"https://api.github.com/repos/{port['repo']}/releases?per_page=30"):
        if rel.get("draft"):
            continue
        asset = next((a for a in rel.get("assets", []) if fnmatch.fnmatch(a["name"], port["asset"])), None)
        if asset is None:
            continue
        out.append({
            "tag": rel["tag_name"],
            "name": rel.get("name") or rel["tag_name"],
            "date": (rel.get("published_at") or "")[:10],
            "prerelease": bool(rel.get("prerelease")),
            "asset_name": asset["name"],
            "asset_url": asset["url"],  # API URL: works for private repos with a token
            "size": asset.get("size", 0),
            "notes": (rel.get("body") or "")[:4000],
        })
    _releases_cache[key] = (time.time(), out)
    return out


# ---------------------------------------------------------------- Steam


def find_appid(gameid):
    """The Steam app id of a devkit shortcut, from shortcuts.vdf (binary VDF)."""
    for path in STEAM_USERDATA.glob("*/config/shortcuts.vdf"):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for s in parse_vdf(data).values():
            if isinstance(s, dict) and s.get("DevkitGameID") == gameid:
                return s.get("appid")
    return None


def parse_vdf(data):
    pos = 0

    def cstr():
        nonlocal pos
        end = data.index(b"\0", pos)
        s = data[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return s

    def obj():
        nonlocal pos
        out = {}
        while pos < len(data):
            t = data[pos]
            pos += 1
            if t == 8:
                return out
            key = cstr()
            if t == 0:
                out[key] = obj()
            elif t == 1:
                out[key] = cstr()
            elif t == 2:
                out[key] = struct.unpack("<I", data[pos:pos + 4])[0]
                pos += 4
            else:
                raise ValueError(f"unknown VDF type {t}")
        return out

    try:
        pos = 1
        cstr()
        return obj()
    except (ValueError, IndexError):
        return {}


def register_with_steam(port, folder):
    if not CREATE_SHORTCUT.exists():
        return "Installed. Add it to Steam with FrameDrop or Add a Non-Steam Game (pick run.sh)."
    parms = {
        "gameid": port["gameid"],
        "directory": str(folder),
        "argv": ["./" + port.get("launch", "run.sh")],
        "env": {},
        "settings": {"compat_tool": config()["compat_tool"], "steam_play": "0"},
    }
    r = subprocess.run([sys.executable, str(CREATE_SHORTCUT), "--parms", json.dumps(parms)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or '"success"' not in r.stdout:
        return "Installed, but Steam didn't accept the shortcut. Restart Steam and try again."
    return None


def add_art(port):
    """Copies the port's library art into Steam's grid folder, without replacing art the user set."""
    art = APP_DIR / "art" / port["id"]
    if not art.is_dir():
        return
    appid = None
    for _ in range(10):  # Steam writes the shortcut to shortcuts.vdf shortly after registering it
        appid = find_appid(port["gameid"])
        if appid:
            break
        time.sleep(2)
    if not appid:
        return
    names = {"portrait.png": f"{appid}p.png", "wide.png": f"{appid}.png", "hero.png": f"{appid}_hero.png",
             "logo.png": f"{appid}_logo.png", "icon.png": f"{appid}_icon.png"}
    for grid in STEAM_USERDATA.glob("[1-9]*/config"):
        (grid / "grid").mkdir(exist_ok=True)
        for src, dst in names.items():
            if (art / src).exists() and not (grid / "grid" / dst).exists():
                shutil.copyfile(art / src, grid / "grid" / dst)


# ---------------------------------------------------------------- install


def game_running(folder):
    folder = str(folder.resolve()) + os.sep
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        if exe.startswith(folder):
            return True
    return False


def make_executables(root):
    """Marks ELF programs and #! scripts executable (Windows-made zips lose the bit)."""
    for dirpath, _, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            try:
                if os.path.islink(p):
                    continue
                with open(p, "rb") as f:
                    head = f.read(4)
                if head == b"\x7fELF" or head[:2] == b"#!":
                    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass


def extract_zip(archive, dest):
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise RuntimeError("The download contains unsafe paths.")
            z.extract(info, dest)
            mode = (info.external_attr >> 16) & 0o777
            if mode and not info.is_dir():
                os.chmod(target, mode)
    # A single top-level folder (e.g. soh-steam-frame/) becomes the install folder itself.
    entries = [e for e in dest.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for e in list(inner.iterdir()):
            shutil.move(str(e), dest / e.name)
        inner.rmdir()


def extract_appimage(archive, dest):
    os.chmod(archive, 0o755)
    # PORT_MANAGER_EXEC_WRAPPER (e.g. qemu-aarch64-static) lets tests on another architecture run the AppImage.
    wrapper = os.environ.get("PORT_MANAGER_EXEC_WRAPPER", "").split()
    try:
        r = subprocess.run(wrapper + [str(archive), "--appimage-extract"], cwd=dest, capture_output=True, timeout=600)
    except OSError as e:
        raise RuntimeError(f"Couldn't run the AppImage to unpack it ({e.strerror}).") from e
    root = dest / "squashfs-root"
    if r.returncode != 0 or not root.is_dir():
        raise RuntimeError("Couldn't unpack the AppImage.")
    for e in list(root.iterdir()):
        shutil.move(str(e), dest / e.name)
    root.rmdir()
    archive.unlink()


class Jobs:
    """One install at a time; state is shown by both front ends."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {}  # port id -> {"status", "progress", "message"}

    def set(self, pid, status, progress=0.0, message=""):
        self.state[pid] = {"status": status, "progress": progress, "message": message}

    def busy(self):
        return any(s["status"] in ("downloading", "installing") for s in self.state.values())


jobs = Jobs()


def install(pid, tag=None):
    port = catalog()[pid]
    try:
        rels = releases(port, force=True)
        if not rels:
            raise RuntimeError("This repo has no release with a matching download yet.")
        rel = rels[0] if tag is None else next((r for r in rels if r["tag"] == tag), None)
        if rel is None:
            raise RuntimeError(f"Release {tag} not found.")
        final = GAMES_DIR / port["gameid"]
        if final.exists() and game_running(final):
            raise RuntimeError("Close the game first.")

        # Download
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        archive = CACHE_DIR / rel["asset_name"]
        if shutil.disk_usage(CACHE_DIR).free < rel["size"] * 3 + 512 * 1024 * 1024:
            raise RuntimeError("Not enough free space.")
        jobs.set(pid, "downloading", 0.0, f"Downloading {rel['tag']}")
        req = gh_request(rel["asset_url"], accept="application/octet-stream")
        with urllib.request.urlopen(req, timeout=60) as r, open(archive, "wb") as f:
            total = int(r.headers.get("Content-Length") or rel["size"] or 0)
            done = 0
            while True:
                block = r.read(CHUNK)
                if not block:
                    break
                f.write(block)
                done += len(block)
                if total:
                    jobs.set(pid, "downloading", done / total, f"Downloading {rel['tag']}")

        # Unpack into a staging folder next to the final one (same filesystem, so the swap is a rename)
        jobs.set(pid, "installing", 1.0, "Unpacking")
        GAMES_DIR.mkdir(parents=True, exist_ok=True)
        staging = GAMES_DIR / f".{port['gameid']}.staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        if port.get("type", "zip") == "appimage":
            moved = staging / archive.name
            shutil.move(str(archive), moved)
            extract_appimage(moved, staging)
        else:
            extract_zip(archive, staging)
            archive.unlink(missing_ok=True)
        if port.get("run_sh"):
            launcher = staging / port.get("launch", "run.sh")
            launcher.write_text("\n".join(port["run_sh"]) + "\n")
        if not (staging / port.get("launch", "run.sh")).exists():
            raise RuntimeError(f"The download has no {port.get('launch', 'run.sh')}.")

        # Carry over files the user put in the old install (ROMs next to run.sh)
        if final.exists():
            for pattern in port.get("keep", []):
                for f in final.glob(pattern):
                    if f.is_file() and not (staging / f.name).exists():
                        shutil.copy2(f, staging / f.name)
        make_executables(staging)

        # Swap: the old install becomes the "previous" one
        previous = GAMES_DIR / f".{port['gameid']}.previous"
        if final.exists():
            if game_running(final):
                raise RuntimeError("Close the game first.")
            shutil.rmtree(previous, ignore_errors=True)
            os.replace(final, previous)
        os.replace(staging, final)

        jobs.set(pid, "installing", 1.0, "Adding to Steam")
        note = register_with_steam(port, final)
        add_art(port)

        installed = load_json(INSTALLED_FILE, {})
        prev = installed.get(pid, {}).get("tag")
        installed[pid] = {"tag": rel["tag"], "previous": prev, "time": int(time.time())}
        save_json(INSTALLED_FILE, installed)

        message = note or f"Installed {rel['tag']}."
        data = Path(os.path.expanduser(port.get("data", "")))
        if port.get("rom_hint") and port.get("data") and not any(data.glob("*.o2r")) and not any(data.glob("*.z64")):
            message += " " + port["rom_hint"]
        jobs.set(pid, "done", 1.0, message)
    except Exception as e:  # report every failure on screen rather than dying
        shutil.rmtree(GAMES_DIR / f".{port['gameid']}.staging", ignore_errors=True)
        jobs.set(pid, "error", 0.0, str(e))


def rollback(pid):
    port = catalog()[pid]
    final = GAMES_DIR / port["gameid"]
    previous = GAMES_DIR / f".{port['gameid']}.previous"
    try:
        if not previous.exists():
            raise RuntimeError("There's no previous version to go back to.")
        if final.exists() and game_running(final):
            raise RuntimeError("Close the game first.")
        swap = GAMES_DIR / f".{port['gameid']}.swap"
        if final.exists():
            os.replace(final, swap)
        os.replace(previous, final)
        if swap.exists():
            os.replace(swap, previous)
        installed = load_json(INSTALLED_FILE, {})
        cur = installed.get(pid, {})
        installed[pid] = {"tag": cur.get("previous"), "previous": cur.get("tag"), "time": int(time.time())}
        save_json(INSTALLED_FILE, installed)
        jobs.set(pid, "done", 1.0, f"Back to {cur.get('previous') or 'the previous version'}.")
    except Exception as e:
        jobs.set(pid, "error", 0.0, str(e))


def start(fn, pid, *args):
    with jobs.lock:
        if jobs.busy():
            return False
        jobs.set(pid, "installing", 0.0, "Starting")
    threading.Thread(target=fn, args=(pid, *args), daemon=True).start()
    return True


# ---------------------------------------------------------------- state


_latest = {}  # port id -> (tag, error)


def refresh(force=False):
    for pid, port in catalog().items():
        try:
            rels = releases(port, force=force)
            _latest[pid] = (rels[0]["tag"] if rels else None, None if rels else "No releases yet")
        except RuntimeError as e:
            _latest[pid] = (None, str(e))


def port_state():
    installed = load_json(INSTALLED_FILE, {})
    out = []
    for pid, port in catalog().items():
        folder = GAMES_DIR / port["gameid"]
        tag = installed.get(pid, {}).get("tag") if folder.exists() else None
        if folder.exists() and not tag:
            tag = "unknown"
        latest, err = _latest.get(pid, (None, None))
        job = jobs.state.get(pid, {})
        if job.get("status") in ("downloading", "installing"):
            status = job["status"]
        elif err and not latest:
            status = "error"
        elif not folder.exists():
            status = "not installed"
        elif latest and tag != latest:
            status = "update available"
        else:
            status = "up to date"
        out.append({
            "id": pid, "name": port["name"], "installed": tag, "latest": latest, "status": status,
            "progress": job.get("progress", 0.0), "message": job.get("message") or err or "",
            "job": job.get("status", ""),
            "can_rollback": (GAMES_DIR / f".{port['gameid']}.previous").exists(),
        })
    return out


def tsv(rows, keys):
    """Tab-separated lines for the headset app (no JSON parser needed there)."""
    def clean(v):
        if v is None:
            return "-"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float):
            return f"{v:.3f}"
        return re.sub(r"[\t\r\n]+", " ", str(v))
    return "\n".join("\t".join(clean(r.get(k)) for k in keys) for r in rows) + "\n"


# ---------------------------------------------------------------- web


class Handler(BaseHTTPRequestHandler):
    server_version = "PortManager/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def reply(self, body, ctype="application/json", code=200):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, q):
        password = config().get("password") or ""
        # The headset app talks to us over loopback and never needs the password.
        if not password or self.client_address[0] == "127.0.0.1":
            return True
        return (self.headers.get("X-Key") or q.get("key", [""])[0]) == password

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            self.reply((APP_DIR / "page.html").read_bytes(), "text/html")
            return
        if not self.allowed(q):
            self.reply({"error": "password"}, code=401)
            return
        fmt = q.get("fmt", ["json"])[0]
        if url.path == "/api/state":
            rows = port_state()
            if fmt == "tsv":
                self.reply(tsv(rows, ["id", "name", "installed", "latest", "status", "progress", "message",
                                      "can_rollback"]), "text/plain")
            else:
                self.reply({"ports": rows, "token_set": bool(config().get("github_token")),
                            "busy": jobs.busy()})
        elif url.path == "/api/releases":
            pid = q.get("id", [""])[0]
            port = catalog().get(pid)
            if not port:
                self.reply({"error": "unknown port"}, code=404)
                return
            try:
                rels = releases(port)
            except RuntimeError as e:
                self.reply({"error": str(e)} if fmt != "tsv" else f"!\t{e}\n", "application/json" if fmt != "tsv" else "text/plain")
                return
            if fmt == "tsv":
                self.reply(tsv(rels, ["tag", "name", "date", "prerelease"]), "text/plain")
            else:
                self.reply({"releases": rels})
        else:
            self.reply({"error": "not found"}, code=404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if not self.allowed(q):
            self.reply({"error": "password"}, code=401)
            return
        pid = q.get("id", [""])[0]
        if url.path == "/api/refresh":
            threading.Thread(target=refresh, kwargs={"force": True}, daemon=True).start()
            self.reply({"ok": True})
        elif url.path in ("/api/install", "/api/rollback"):
            if pid not in catalog():
                self.reply({"error": "unknown port"}, code=404)
                return
            tag = q.get("tag", [None])[0] or None
            ok = start(install, pid, tag) if url.path == "/api/install" else start(rollback, pid)
            self.reply({"ok": ok, "error": None if ok else "Another install is running."})
        elif url.path == "/api/token":
            length = int(self.headers.get("Content-Length") or 0)
            token = self.rfile.read(min(length, 4096)).decode().strip()
            cfg = load_json(CONFIG_FILE, {})
            cfg["github_token"] = token
            save_json(CONFIG_FILE, cfg)
            os.chmod(CONFIG_FILE, 0o600)
            _releases_cache.clear()
            threading.Thread(target=refresh, kwargs={"force": True}, daemon=True).start()
            self.reply({"ok": True})
        else:
            self.reply({"error": "not found"}, code=404)


def checker():
    while True:
        refresh(force=True)
        time.sleep(max(1, float(config().get("check_hours", 6))) * 3600)


def main():
    cfg = config()
    if not CONFIG_FILE.exists():
        save_json(CONFIG_FILE, cfg)
        os.chmod(CONFIG_FILE, 0o600)
    threading.Thread(target=checker, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", int(cfg["port"])), Handler)
    server.daemon_threads = True
    print(f"Port Manager on port {cfg['port']}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
