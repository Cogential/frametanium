#!/usr/bin/env python3
"""Port Manager: installs and updates Steam Frame apps and ports from their GitHub releases, like Obtainium.

A small web service (standard library only) that runs on the Frame. It tracks a list of GitHub repos: the built-in
ports in catalog.json plus any repo you add. For each it reads the releases, picks the download meant for the Frame
(ARM64 Linux), unpacks it and either
  - adds it to the Steam library (mode "game": ~/devkit-game/<gameid>, a run.sh launcher, library art), or
  - runs the project's own installer (mode "installer": install.sh, contrib/install.sh or setup.sh), for tools like
    SteamVR overlays that set themselves up.
ROMs and saves are kept, and the previous install is kept for a one-step rollback.

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
import tarfile
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
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "port-manager"
APPS_DIR = DATA_DIR / "apps"          # installs of "installer" apps (their installers may point back into them)
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
INSTALLERS = ["install.sh", "contrib/install.sh", "scripts/install.sh", "setup.sh"]
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


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
    """The built-in ports plus the apps the user added. A user entry with the id of a built-in one changes it, and
    {"id": ..., "hidden": true} takes a built-in one off the list."""
    apps = {}
    for p in load_json(APP_DIR / "catalog.json", []):
        apps[p["id"]] = {**p, "builtin": True}
    for p in load_json(USER_CATALOG, []):
        if "id" in p:
            apps[p["id"]] = {**apps.get(p["id"], {}), **p}
    return {k: v for k, v in apps.items() if not v.get("hidden") and v.get("repo")}


def update_user_entry(pid, **fields):
    """Changes (or with remove=True deletes) the user's entry for an app, creating one for a built-in app."""
    entries = load_json(USER_CATALOG, [])
    entry = next((e for e in entries if e.get("id") == pid), None)
    if entry is None:
        entry = {"id": pid}
        entries.append(entry)
    entry.update(fields)
    save_json(USER_CATALOG, entries)


def suggestions():
    """Apps worth adding: the built-in ports (so a removed one can come back) and the ones in suggestions.json."""
    ports = [{"repo": p["repo"], "name": p["name"], "note": "Built-in port"}
             for p in load_json(APP_DIR / "catalog.json", [])]
    return ports + load_json(APP_DIR / "suggestions.json", [])


def unhide_builtin(repo):
    """Puts a removed built-in port back on the list; returns its id, or None if the repo isn't one."""
    port = next((p for p in load_json(APP_DIR / "catalog.json", []) if p["repo"].lower() == repo.lower()), None)
    if port is None:
        return None
    entries = load_json(USER_CATALOG, [])
    save_json(USER_CATALOG, [e for e in entries if not (e.get("id") == port["id"] and e.get("hidden"))])
    return port["id"]


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
            raise RuntimeError("Repo not found. Check the address; if it's private, add a GitHub token in "
                               "Settings.") from e
        if e.code in (401, 403, 429):
            raise RuntimeError("GitHub refused the request (token or rate limit). Try again later.") from e
        raise RuntimeError(f"GitHub error {e.code}") from e
    except OSError as e:
        raise RuntimeError("Can't reach GitHub. Is the Frame online?") from e
    except ValueError as e:  # e.g. a token with a newline or other characters a header can't hold
        raise RuntimeError("The GitHub token isn't valid. Check it in Settings.") from e


def parse_repo(text):
    """owner/repo from "owner/repo", "github.com/owner/repo" or any github.com URL inside a repo."""
    t = (text or "").strip()
    t = re.sub(r"^(https?://)?(www\.)?github\.com/", "", t, flags=re.I)
    t = re.split(r"[?#]", t)[0]
    parts = [p for p in t.split("/") if p]
    if len(parts) < 2:
        raise RuntimeError("Enter a GitHub repo like owner/name or github.com/owner/name.")
    repo = parts[0] + "/" + re.sub(r"\.git$", "", parts[1])
    if not REPO_RE.match(repo):
        raise RuntimeError("That doesn't look like a GitHub repo address.")
    return repo


# ---- choosing the download

ARCHIVES = [(".tar.gz", "tar"), (".tgz", "tar"), (".tar.xz", "tar"), (".txz", "tar"), (".tar.bz2", "tar"),
            (".tar.zst", "tar"), (".tar", "tar"), (".zip", "zip"), (".appimage", "appimage")]
NOT_DOWNLOADS = re.compile(r"\.(sha\d*|sha\d+sum|md5|sig|asc|minisig|pem|txt|md|json|ya?ml|html|pdf|png|jpe?g|"
                           r"apk|aab|ipa|exe|msi|msix|dmg|pkg|deb|rpm|flatpak|snap|7z|rar|whl|jar|so|dll)$", re.I)
NOT_PROGRAMS = re.compile(r"(sums?$|checksum|readme|license|changelog|manifest|notes)", re.I)
OTHER_SYSTEMS = re.compile(r"(windows|win32|win64|\bwin\b|macos|darwin|osx|android|\bios\b|apple|mingw|msvc)", re.I)
OTHER_CPUS = re.compile(r"(x86[_-]?64|amd64|\bx64\b|i[36]86|\bx86\b|armv7|armhf|\barm32\b|riscv)", re.I)
OUR_CPU = re.compile(r"(aarch64|arm64)", re.I)


def asset_type(name):
    low = name.lower()
    for ext, kind in ARCHIVES:
        if low.endswith(ext):
            return kind
    # A file with no archive extension is a program on its own (e.g. tool-aarch64-linux-gnu).
    return None if NOT_DOWNLOADS.search(low) or re.search(r"\.[a-z][a-z0-9]{0,4}$", low) else "binary"


def score_asset(name):
    """How likely a release file is the Frame's download: None if it can't be one (checksums, other systems...)."""
    if NOT_DOWNLOADS.search(name) or NOT_PROGRAMS.search(name) or asset_type(name) is None \
            or OTHER_SYSTEMS.search(name):
        return None
    score = 0
    if OUR_CPU.search(name):
        score += 50
    elif OTHER_CPUS.search(name):
        score -= 60   # still offered (some run through FEX), but only when nothing else fits
    if re.search(r"steam[-_. ]?frame|frame", name, re.I):
        score += 20
    if re.search(r"linux", name, re.I):
        score += 10
    score += {"appimage": 6, "tar": 4, "zip": 3, "binary": 0}[asset_type(name)]
    return score


def asset_glob(name):
    """A pattern that finds the same download in later releases: version numbers become *."""
    pat = re.sub(r"v?\d+(\.\d+)+(-?(rc|beta|alpha)\.?\d*)?", "*", name)
    return re.sub(r"\*+", "*", pat)


def choose_asset(port, assets):
    pattern = port.get("asset")
    if pattern:
        hit = next((a for a in assets if fnmatch.fnmatch(a["name"], pattern)), None)
        if hit or not port.get("auto_asset"):
            return hit
    scored = [(score_asset(a["name"]), a) for a in assets]
    scored = [(s, a) for s, a in scored if s is not None and s > -30]
    return max(scored, key=lambda x: x[0])[1] if scored else None


_releases_cache = {}


def releases(port, force=False):
    """Releases newest first, each with the download chosen for the Frame."""
    key = (port["repo"], port.get("asset"))
    hit = _releases_cache.get(key)
    if hit and not force and time.time() - hit[0] < RELEASES_TTL:
        return hit[1]
    out = []
    for rel in gh_json(f"https://api.github.com/repos/{port['repo']}/releases?per_page=30"):
        if rel.get("draft"):
            continue
        asset = choose_asset(port, rel.get("assets", []))
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


def latest_release(port, rels):
    """The release an update goes to: the newest one, skipping pre-releases unless the app takes them."""
    if port.get("prereleases"):
        return rels[0] if rels else None
    return next((r for r in rels if not r["prerelease"]), None)


def nice_name(repo_name):
    words = re.split(r"[-_ ]+", repo_name)
    if repo_name == repo_name.lower():
        words = [w[:1].upper() + w[1:] for w in words]
    return " ".join(w for w in words if w)


def inspect_repo(text):
    """What adding a repo would install: its name, the newest usable release and every download it could use."""
    repo = parse_repo(text)
    info = gh_json(f"https://api.github.com/repos/{repo}")
    repo = info.get("full_name") or repo
    rels = [r for r in gh_json(f"https://api.github.com/repos/{repo}/releases?per_page=15") if not r.get("draft")]
    if not rels:
        raise RuntimeError(f"{repo} has no releases to install.")
    usable = [r for r in rels if any(score_asset(a["name"]) is not None for a in r.get("assets", []))]
    if not usable:
        raise RuntimeError(f"{repo}'s releases have no Linux downloads (only source code or other systems).")
    stable = next((r for r in usable if not r.get("prerelease")), None)
    rel = stable or usable[0]
    assets = sorted(({"name": a["name"], "size": a.get("size", 0), "score": score_asset(a["name"])}
                     for a in rel["assets"] if score_asset(a["name"]) is not None),
                    key=lambda a: -a["score"])
    for a in assets:
        a["arm64"] = bool(OUR_CPU.search(a["name"])) or not OTHER_CPUS.search(a["name"])
    existing = next((pid for pid, p in catalog().items() if p["repo"].lower() == repo.lower()), None)
    return {
        "repo": repo,
        "name": nice_name(info.get("name") or repo.split("/")[1]),
        "description": re.sub(r"\s+", " ", (info.get("description") or "")).strip(),
        "tag": rel["tag_name"],
        "date": (rel.get("published_at") or "")[:10],
        "prereleases": stable is None,
        "assets": assets,
        "existing": existing,
    }


def add_app(repo_text, asset=None, name=None, mode="auto", prereleases=None):
    """Adds a repo to the list and returns its id (an existing one's id if it's already there)."""
    builtin = unhide_builtin(parse_repo(repo_text))
    if builtin:
        return builtin
    info = inspect_repo(repo_text)
    if info["existing"]:
        return info["existing"]
    names = [a["name"] for a in info["assets"]]
    if asset and asset not in names:
        raise RuntimeError(f"{asset} isn't in the latest release.")
    asset = asset or names[0]
    base = re.sub(r"[^a-z0-9]+", "-", info["repo"].split("/")[1].lower()).strip("-") or "app"
    pid, n = base, 2
    taken = set(catalog()) | {e.get("id") for e in load_json(USER_CATALOG, [])}
    while pid in taken:
        pid, n = f"{base}-{n}", n + 1
    title = (name or info["name"]).strip()[:80] or info["name"]
    gameid = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_") or "App"
    if gameid[0].isdigit():
        gameid = "App_" + gameid
    used = {p.get("gameid") for p in catalog().values()}
    g, n = gameid, 2
    while g in used:
        g, n = f"{gameid}_{n}", n + 1
    entries = load_json(USER_CATALOG, [])
    entries.append({
        "id": pid, "name": title, "repo": info["repo"], "asset": asset_glob(asset), "auto_asset": True,
        "mode": mode if mode in ("auto", "game", "installer") else "auto",
        "prereleases": info["prereleases"] if prereleases is None else bool(prereleases),
        "gameid": g, "description": info["description"][:300], "added": int(time.time()),
    })
    save_json(USER_CATALOG, entries)
    _releases_cache.clear()
    return pid


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


def fetch(url, dest, limit=8 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": "frame-port-manager"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(limit)
    if not data.startswith(b"\x89PNG") and not data.startswith(b"\xff\xd8"):
        raise OSError("not an image")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def art_folder(port):
    """The app's library art: the built-in ports ship theirs, added apps get their GitHub preview card and the
    owner's avatar (fetched once)."""
    if port.get("builtin"):
        return APP_DIR / "art" / port["id"]
    art = DATA_DIR / "art" / port["id"]
    if not (art / "wide.png").exists():
        owner, name = port["repo"].split("/")
        try:
            fetch(f"https://opengraph.githubassets.com/1/{owner}/{name}", art / "wide.png")
            shutil.copyfile(art / "wide.png", art / "hero.png")
            fetch(f"https://github.com/{owner}.png?size=256", art / "icon.png")
        except OSError:
            pass
    return art


def add_art(port):
    """Copies the app's library art into Steam's grid folder, without replacing art the user set."""
    art = art_folder(port)
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
    flatten(dest)


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



def flatten(dest):
    """A single top-level folder (e.g. soh-steam-frame/) becomes the install folder itself."""
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        inner = dest / ".pm-inner"
        os.replace(entries[0], inner)
        for e in list(inner.iterdir()):
            shutil.move(str(e), dest / e.name)
        inner.rmdir()


def extract_tar(archive, dest):
    if archive.name.lower().endswith(".tar.zst"):
        r = subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(dest), "--no-same-owner"],
                           capture_output=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError("Couldn't unpack the .tar.zst download.")
    else:
        with tarfile.open(archive) as t:
            root = dest.resolve()
            for m in t.getmembers():
                target = (dest / m.name).resolve()
                if root not in target.parents and target != root:
                    raise RuntimeError("The download contains unsafe paths.")
                if m.issym() or m.islnk():
                    link = (target.parent / m.linkname).resolve() if m.issym() else (dest / m.linkname).resolve()
                    if root not in link.parents:
                        raise RuntimeError("The download contains unsafe links.")
                if m.isdev():
                    raise RuntimeError("The download contains device files.")
            if hasattr(tarfile, "data_filter"):
                t.extractall(dest, filter="data")
            else:
                t.extractall(dest)
    flatten(dest)


def elf_info(path):
    """(machine, is_program) for an ELF file, else None. Programs have an interpreter or are static executables;
    shared libraries have neither."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if len(head) < 64 or head[:4] != b"\x7fELF" or head[4] != 2:
                return None
            e_type, machine = struct.unpack_from("<HH", head, 16)
            phoff = struct.unpack_from("<Q", head, 32)[0]
            phentsize, phnum = struct.unpack_from("<HH", head, 54)
            f.seek(phoff)
            ph = f.read(phentsize * phnum)
    except (OSError, struct.error):
        return None
    interp = any(struct.unpack_from("<I", ph, i * phentsize)[0] == 3 for i in range(phnum)
                 if (i + 1) * phentsize <= len(ph))
    return machine, interp or e_type == 2


AARCH64 = 0xB7
SKIP_DIRS = {"lib", "lib64", "libs", "vendor", "contrib", "tools", "third_party", "licenses", "share", "include",
             "docs", "doc", "resources", "data", ".git"}
NOT_LAUNCHERS = re.compile(r"(install|uninstall|setup|update|updater|build|test|config|ctl|guard|importer|helper)",
                           re.I)
GENERIC_WORDS = {"steam", "frame", "steamframe", "arm64", "aarch64", "linux", "port", "native", "app", "the"}


def words(s):
    return {w for w in re.split(r"[^a-z0-9]+", s.lower()) if w and w not in GENERIC_WORDS}


def find_program(root, port):
    """The file that starts the app: the best-named ARM64 program (or start script) near the top of the folder."""
    want = words(port["repo"].split("/")[1]) | words(port.get("name", ""))
    best = None
    for dirpath, dirs, files in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        depth = len(rel_dir.parts)
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and depth < 3]
        for name in files:
            p = Path(dirpath) / name
            if p.is_symlink() or ".so" in name:
                continue
            info = elf_info(p)
            score = 0
            if info:
                machine, program = info
                if not program:
                    continue
                score += 40 if machine == AARCH64 else -40
            elif name.endswith(".sh") and depth == 0:
                if not re.match(r"(run|start|launch|play)", name, re.I):
                    continue
                score += 25
            else:
                continue
            if NOT_LAUNCHERS.search(name) and not name.endswith(".sh"):
                score -= 30
            stem = re.sub(r"\.(sh|x86_64|arm64|aarch64|bin|elf)$", "", name, flags=re.I)
            got = words(stem)
            score += 15 * len(got & want) + (20 if got and got == want else 0) - 4 * depth
            if best is None or score > best[0]:
                best = (score, p)
    return best[1] if best else None


def write_launcher(staging, port):
    """Makes sure the folder has the run.sh Steam starts: the port's own, one from the catalog, or one that starts
    the program found in the download."""
    launch = staging / port.get("launch", "run.sh")
    if port.get("run_sh"):
        launch.write_text("\n".join(port["run_sh"]) + "\n")
        return None
    if launch.exists():
        return None
    if port.get("program"):
        target = staging / port["program"]
        if not target.exists():
            raise RuntimeError(f"The download has no {port['program']} (the program set for this app).")
    elif (staging / "AppRun").exists():
        target = staging / "AppRun"
    else:
        target = find_program(staging, port)
        if target is None:
            raise RuntimeError("Couldn't find a program to start in this download. Set one in the web page "
                               "(Edit), or install it with its installer instead.")
    rel = target.relative_to(staging)
    folder = "/" + str(rel.parent) if str(rel.parent) != "." else ""
    launch.write_text(
        "#!/bin/sh\n"
        f"# Starts {port['name']} (written by Port Manager).\n"
        'D="$(cd "$(dirname "$0")" && pwd)"\n'
        f'chmod +x "$D/{rel}" 2>/dev/null\n'
        f'cd "$D{folder}" && exec "./{rel.name}" "$@"\n')
    return str(rel)


def find_installer(root, port):
    for name in ([port["installer"]] if port.get("installer") else []) + INSTALLERS:
        if (root / name).is_file():
            return root / name
    return None


def run_installer(folder, port, uninstall=False):
    """Runs the app's own installer (or its uninstaller) and returns its last lines. Raises if it fails."""
    script = find_installer(folder, port)
    if script is None:
        raise RuntimeError("The download has no install.sh.")
    args = [str(script)]
    if uninstall:
        if (script.parent / "uninstall.sh").is_file():
            args = [str(script.parent / "uninstall.sh")]
        elif "--uninstall" in script.read_text(errors="replace"):
            args.append("--uninstall")
        else:
            return ""
    log = CACHE_DIR / f"{port['id']}-{'uninstall' if uninstall else 'install'}.log"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
    with open(log, "wb") as out:
        try:
            r = subprocess.run((["bash"] if args[0].endswith(".sh") else []) + args, cwd=script.parent,
                               stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, timeout=900)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Its installer didn't finish in 15 minutes (log: {log}).") from e
    lines = [l.strip() for l in log.read_text(errors="replace").splitlines() if l.strip()]
    tail = " ".join(lines[-2:])[:200]
    if r.returncode != 0:
        raise RuntimeError(f"Its installer failed: {tail or 'no output'} (log: {log})")
    return tail


class Jobs:
    """One install at a time; state is shown by both front ends."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {}  # app id -> {"status", "progress", "message"}

    def set(self, pid, status, progress=0.0, message=""):
        self.state[pid] = {"status": status, "progress": progress, "message": message}

    def busy(self):
        return any(s["status"] in ("downloading", "installing", "waiting") for s in self.state.values())


jobs = Jobs()


def app_mode(port):
    return port.get("mode") or "game"


def target_dir(port, mode=None):
    mode = mode or app_mode(port)
    return GAMES_DIR / port["gameid"] if mode == "game" else APPS_DIR / port["id"]


def install(pid, tag=None):
    port = catalog()[pid]
    staging = archive = None
    try:
        rels = releases(port, force=True)
        if not rels:
            raise RuntimeError("This repo has no release with a download for the Frame yet.")
        rel = latest_release(port, rels) if tag is None else next((r for r in rels if r["tag"] == tag), None)
        if rel is None:
            raise RuntimeError(f"Release {tag} not found." if tag else "Only pre-releases so far. Turn on "
                               "pre-releases for this app, or pick a version.")

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

        # Unpack into a staging folder (moved into place with a rename once it's ready)
        jobs.set(pid, "installing", 1.0, "Unpacking")
        stage_root = DATA_DIR / "staging"
        stage_root.mkdir(parents=True, exist_ok=True)
        staging = stage_root / pid
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        kind = port.get("type") or asset_type(rel["asset_name"])
        if kind == "appimage":
            moved = staging / archive.name
            shutil.move(str(archive), moved)
            extract_appimage(moved, staging)
        elif kind == "tar":
            extract_tar(archive, staging)
        elif kind == "binary":
            shutil.move(str(archive), staging / rel["asset_name"])
        else:
            extract_zip(archive, staging)
        archive.unlink(missing_ok=True)
        make_executables(staging)

        mode = app_mode(port)
        if mode == "auto":
            mode = "installer" if find_installer(staging, port) and not port.get("run_sh") else "game"
            update_user_entry(pid, mode=mode)
            port["mode"] = mode
        final = target_dir(port, mode)
        if final.exists() and game_running(final):
            raise RuntimeError("Close the app first.")
        program = write_launcher(staging, port) if mode == "game" else None

        # Carry over files the user put in the old install (ROMs next to run.sh)
        if final.exists():
            for pattern in port.get("keep", []):
                for f in final.glob(pattern):
                    if f.is_file() and not (staging / f.name).exists():
                        shutil.copy2(f, staging / f.name)
        make_executables(staging)

        # Swap: the old install becomes the "previous" one. Staging is on the same filesystem as the games folder
        # on the Frame (both in the home directory); a move covers the case where it isn't.
        final.parent.mkdir(parents=True, exist_ok=True)
        previous = final.parent / f".{final.name}.previous"
        if final.exists():
            if game_running(final):
                raise RuntimeError("Close the app first.")
            shutil.rmtree(previous, ignore_errors=True)
            os.replace(final, previous)
        shutil.move(str(staging), final)
        staging = None

        if mode == "game":
            jobs.set(pid, "installing", 1.0, "Adding to Steam")
            note = register_with_steam(port, final)
            add_art(port)
            message = note or f"Installed {rel['tag']}. It's in your library."
            if program and not note:
                message += f" Starts {program}."
        else:
            jobs.set(pid, "installing", 1.0, "Running its installer")
            try:
                tail = run_installer(final, port)
            except RuntimeError:
                # Put back what was there before, so the list shows what's really installed.
                shutil.rmtree(final, ignore_errors=True)
                if previous.exists():
                    os.replace(previous, final)
                raise
            message = f"Installed {rel['tag']}." + (f" {tail}" if tail else "")

        installed = load_json(INSTALLED_FILE, {})
        prev = installed.get(pid, {}).get("tag")
        installed[pid] = {"tag": rel["tag"], "previous": prev, "time": int(time.time()), "mode": mode}
        save_json(INSTALLED_FILE, installed)

        data = Path(os.path.expanduser(port.get("data", "")))
        if port.get("rom_hint") and port.get("data") and not any(data.glob("*.o2r")) and not any(data.glob("*.z64")):
            message += " " + port["rom_hint"]
        jobs.set(pid, "done", 1.0, message)
    except Exception as e:  # report every failure on screen rather than dying
        jobs.set(pid, "error", 0.0, str(e))
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if archive is not None:
            archive.unlink(missing_ok=True)


def rollback(pid):
    port = catalog()[pid]
    try:
        installed = load_json(INSTALLED_FILE, {})
        cur = installed.get(pid, {})
        mode = cur.get("mode") or app_mode(port)
        final = target_dir(port, mode)
        previous = final.parent / f".{final.name}.previous"
        if not previous.exists():
            raise RuntimeError("There's no previous version to go back to.")
        if final.exists() and game_running(final):
            raise RuntimeError("Close the app first.")
        swap = final.parent / f".{final.name}.swap"
        if final.exists():
            os.replace(final, swap)
        os.replace(previous, final)
        if swap.exists():
            os.replace(swap, previous)
        if mode == "installer":
            jobs.set(pid, "installing", 1.0, "Running its installer")
            run_installer(final, port)
        installed[pid] = {"tag": cur.get("previous"), "previous": cur.get("tag"), "time": int(time.time()),
                          "mode": mode}
        save_json(INSTALLED_FILE, installed)
        jobs.set(pid, "done", 1.0, f"Back to {cur.get('previous') or 'the previous version'}.")
    except Exception as e:
        jobs.set(pid, "error", 0.0, str(e))


def remove(pid, delete_files=True):
    """Takes an app off the list. Its files go too (an installer app's uninstaller runs first); saves and settings
    outside its folder stay."""
    port = catalog()[pid]
    try:
        installed = load_json(INSTALLED_FILE, {})
        mode = installed.get(pid, {}).get("mode") or app_mode(port)
        note = ""
        if delete_files and mode != "auto":
            final = target_dir(port, mode)
            if final.exists() and game_running(final):
                raise RuntimeError("Close the app first.")
            if mode == "installer" and final.exists():
                jobs.set(pid, "installing", 1.0, "Running its uninstaller")
                run_installer(final, port, uninstall=True)
            if final.exists() and mode == "game":
                note = " Remove its tile from the library too: Manage > Remove non-Steam game."
            shutil.rmtree(final, ignore_errors=True)
            shutil.rmtree(final.parent / f".{final.name}.previous", ignore_errors=True)
        installed.pop(pid, None)
        save_json(INSTALLED_FILE, installed)
        entries = [e for e in load_json(USER_CATALOG, []) if e.get("id") != pid]
        if port.get("builtin"):
            entries.append({"id": pid, "hidden": True})
        save_json(USER_CATALOG, entries)
        jobs.state.pop(pid, None)
        _latest.pop(pid, None)
        jobs.set("_list", "done", 1.0, f"Removed {port['name']}.{note}")
    except Exception as e:
        jobs.set(pid, "error", 0.0, str(e))


def update_all(_pid=None):
    """Updates every installed app that has a newer release, one after another."""
    jobs.set("_list", "installing", 0.0, "Updating everything")
    count = 0
    for row in app_state():
        if row["status"] == "update available":
            install(row["id"])
            count += 1
    jobs.set("_list", "done", 1.0, f"Updated {count} app{'s' if count != 1 else ''}." if count
             else "Everything is up to date.")


def start(fn, pid, *args):
    with jobs.lock:
        if jobs.busy():
            return False
        jobs.set(pid, "installing", 0.0, "Starting")
    threading.Thread(target=fn, args=(pid, *args), daemon=True).start()
    return True


# ---------------------------------------------------------------- state


_latest = {}  # app id -> (tag, error)


def refresh(force=False):
    for pid, port in catalog().items():
        try:
            rels = releases(port, force=force)
            rel = latest_release(port, rels)
            _latest[pid] = (rel["tag"] if rel else None,
                            None if rel else ("Only pre-releases so far" if rels else "No releases for the Frame yet"))
        except RuntimeError as e:
            _latest[pid] = (None, str(e))


def app_state():
    installed = load_json(INSTALLED_FILE, {})
    out = []
    for pid, port in catalog().items():
        mode = installed.get(pid, {}).get("mode") or app_mode(port)
        folder = target_dir(port, mode) if mode != "auto" else None
        present = bool(folder and folder.exists())
        tag = installed.get(pid, {}).get("tag") if present else None
        if present and not tag:
            tag = "unknown"
        if pid not in _latest:
            refresh_one(pid, port)
        latest, err = _latest.get(pid, (None, None))
        job = jobs.state.get(pid, {})
        if job.get("status") in ("downloading", "installing", "waiting"):
            status = job["status"]
        elif err and not latest and not present:
            status = "error"
        elif not present:
            status = "not installed"
        elif latest and tag != latest:
            status = "update available"
        else:
            status = "up to date"
        out.append({
            "id": pid, "name": port["name"], "installed": tag, "latest": latest, "status": status,
            "progress": job.get("progress", 0.0), "message": job.get("message") or err or "",
            "job": job.get("status", ""),
            "can_rollback": bool(folder and (folder.parent / f".{folder.name}.previous").exists()),
            "repo": port["repo"], "mode": mode, "prereleases": bool(port.get("prereleases")),
            "builtin": bool(port.get("builtin")), "description": port.get("description", ""),
            "program": port.get("program", ""),
        })
    return out


def refresh_one(pid, port):
    """Fills in the latest version of an app that hasn't been checked yet (just added), in the background."""
    _latest[pid] = (None, None)

    def work():
        try:
            rels = releases(port)
            rel = latest_release(port, rels)
            _latest[pid] = (rel["tag"] if rel else None, None if rel else "Only pre-releases so far")
        except RuntimeError as e:
            _latest[pid] = (None, str(e))
    threading.Thread(target=work, daemon=True).start()


def tsv(rows, keys):
    """Tab-separated lines for the headset app (no JSON parser needed there)."""
    def clean(v):
        if v is None:
            return "-"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float):
            return f"{v:.3f}"
        return re.sub(r"[\t\r\n]+", " ", str(v)) or "-"
    return "\n".join("\t".join(clean(r.get(k)) for k in keys) for r in rows) + "\n"


# ---------------------------------------------------------------- web


class Handler(BaseHTTPRequestHandler):
    server_version = "PortManager/2.0"

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

    def fail(self, message, fmt, code=400):
        if fmt == "tsv":
            self.reply(f"!\t{message}\n", "text/plain")
        else:
            self.reply({"error": message}, code=code)

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
        arg = lambda k: q.get(k, [""])[0]
        if url.path == "/api/state":
            rows = app_state()
            notice = jobs.state.get("_list", {})
            if fmt == "tsv":
                head = f"#\t{notice.get('status', '')}\t{notice.get('message', '')}\n" if notice else ""
                self.reply(head + tsv(rows, ["id", "name", "installed", "latest", "status", "progress", "message",
                                             "can_rollback", "repo", "mode", "prereleases", "builtin", "job"]),
                           "text/plain")
            else:
                self.reply({"apps": rows, "token_set": bool(config().get("github_token")), "busy": jobs.busy(),
                            "notice": notice})
        elif url.path == "/api/releases":
            port = catalog().get(arg("id"))
            if not port:
                self.fail("Unknown app.", fmt, 404)
                return
            try:
                rels = releases(port)
            except RuntimeError as e:
                self.fail(str(e), fmt)
                return
            if fmt == "tsv":
                self.reply(tsv(rels, ["tag", "name", "date", "prerelease"]), "text/plain")
            else:
                self.reply({"releases": rels})
        elif url.path == "/api/inspect":
            try:
                info = inspect_repo(arg("repo"))
            except RuntimeError as e:
                self.fail(str(e), fmt)
                return
            if fmt == "tsv":
                head = tsv([info], ["repo", "name", "description", "tag", "date", "prereleases", "existing"])
                self.reply("=\t" + head + tsv(info["assets"], ["name", "size", "arm64"]), "text/plain")
            else:
                self.reply(info)
        elif url.path == "/api/suggestions":
            have = {p["repo"].lower() for p in catalog().values()}
            rows = [{**s, "added": s["repo"].lower() in have} for s in suggestions()]
            if fmt == "tsv":
                self.reply(tsv(rows, ["repo", "name", "note", "added"]), "text/plain")
            else:
                self.reply({"suggestions": rows})
        else:
            self.reply({"error": "not found"}, code=404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if not self.allowed(q):
            self.reply({"error": "password"}, code=401)
            return
        fmt = q.get("fmt", ["json"])[0]
        arg = lambda k: q.get(k, [""])[0]
        pid = arg("id")
        if url.path == "/api/refresh":
            _releases_cache.clear()
            threading.Thread(target=refresh, kwargs={"force": True}, daemon=True).start()
            self.reply({"ok": True})
        elif url.path == "/api/update_all":
            ok = start(update_all, "_list")
            self.reply({"ok": ok, "error": None if ok else "Something else is installing."})
        elif url.path in ("/api/install", "/api/rollback", "/api/remove"):
            if pid not in catalog():
                self.fail("Unknown app.", fmt, 404)
                return
            if url.path == "/api/install":
                ok = start(install, pid, arg("tag") or None)
            elif url.path == "/api/rollback":
                ok = start(rollback, pid)
            else:
                ok = start(remove, pid, arg("keep_files") != "1")
            self.reply({"ok": ok, "error": None if ok else "Something else is installing."})
        elif url.path == "/api/add":
            if jobs.busy():
                self.fail("Wait for the current install to finish.", fmt)
                return
            try:
                new = add_app(arg("repo"), arg("asset") or None, arg("name") or None, arg("mode") or "auto")
            except RuntimeError as e:
                self.fail(str(e), fmt)
                return
            if arg("install") == "1":
                start(install, new)
            self.reply(f"=\t{new}\n" if fmt == "tsv" else {"ok": True, "id": new},
                       "text/plain" if fmt == "tsv" else "application/json")
        elif url.path == "/api/settings":
            port = catalog().get(pid)
            if not port:
                self.fail("Unknown app.", fmt, 404)
                return
            fields = {}
            if "prereleases" in q:
                fields["prereleases"] = arg("prereleases") == "1"
            if arg("name").strip():
                fields["name"] = arg("name").strip()[:80]
            if arg("mode") in ("auto", "game", "installer"):
                fields["mode"] = arg("mode")
            if "program" in q:
                fields["program"] = arg("program").strip().lstrip("/")
            if "asset" in q and arg("asset").strip() and not port.get("builtin"):
                fields["asset"] = arg("asset").strip()
            update_user_entry(pid, **fields)
            _releases_cache.clear()
            _latest.pop(pid, None)
            self.reply({"ok": True})
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
