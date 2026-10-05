"""Prints the Steam app id of a devkit shortcut, read from shortcuts.vdf. Usage: python3 - <DevkitGameID>"""
import glob
import os
import struct
import sys

want = sys.argv[1]


def parse(data):
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
        while True:
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
                raise ValueError(f"unknown type {t}")

    pos = 1
    cstr()
    return obj()


for path in glob.glob(os.path.expanduser("~/.local/share/Steam/userdata/*/config/shortcuts.vdf")):
    try:
        shortcuts = parse(open(path, "rb").read())
    except (OSError, ValueError, IndexError):
        continue
    for s in shortcuts.values():
        if s.get("DevkitGameID") == want:
            print(s.get("appid"))
            sys.exit(0)
sys.exit(1)
