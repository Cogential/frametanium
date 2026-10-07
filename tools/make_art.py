#!/usr/bin/env python3
"""Draws Frametanium's library art into art/app (needs Pillow and the DejaVu fonts). Run from the repo root."""
from PIL import Image, ImageDraw, ImageFont
import subprocess, math

NAME = "Frametanium"
bold = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:bold"], capture_output=True, text=True).stdout
reg = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans"], capture_output=True, text=True).stdout
BG = (15, 18, 22); ACC = (79, 163, 255); TXT = (232, 237, 242); MUT = (147, 160, 173)


def glow(W, H, c):
    im = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(im); R = int(max(W, H) * 0.6)
    for r in range(R, 0, -8):
        t = r / R; col = tuple(int(BG[i] * t + (25, 45, 75)[i] * (1 - t)) for i in range(3))
        d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=col)
    return im


def icon(d, cx, cy, s):
    """A cartridge with a circular update arrow."""
    lw = max(4, s // 16)
    w = int(s * 0.78); h = int(s * 0.9); x0 = cx - w // 2; y0 = cy - h // 2
    d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=s // 10, outline=TXT, width=lw)
    d.rectangle([x0 + w * 0.2, y0 + h * 0.12, x0 + w * 0.8, y0 + h * 0.42], outline=TXT, width=lw)
    for k in range(5):
        x = x0 + w * 0.18 + k * w * 0.16; d.line([(x, y0 + h * 0.84), (x, y0 + h * 0.96)], fill=TXT, width=lw)
    r = s * 0.2; ax, ay = cx, cy + h * 0.18
    d.arc([ax - r, ay - r, ax + r, ay + r], start=200, end=500, fill=ACC, width=lw)
    ang = math.radians(140); ex, ey = ax + r * math.cos(ang), ay + r * math.sin(ang)
    d.polygon([(ex - s * 0.08, ey - s * 0.02), (ex + s * 0.05, ey - s * 0.06), (ex + s * 0.01, ey + s * 0.08)], fill=ACC)


def make(W, H, c, s, title=None, txy=None, ts=0, sub=None):
    im = glow(W, H, c); d = ImageDraw.Draw(im); icon(d, c[0], c[1], s)
    if title:
        d.text(txy, title, font=ImageFont.truetype(bold, ts), fill=TXT, anchor="mm")
        if sub:
            d.text((txy[0], txy[1] + int(ts * 0.95)), sub, font=ImageFont.truetype(reg, int(ts * 0.38)), fill=MUT, anchor="mm")
    return im


make(600, 900, (300, 320), 280, NAME, (300, 620), 70, "Apps & ports from GitHub").save("art/app/portrait.png")
make(920, 430, (170, 215), 230, NAME, (600, 190), 64, "Updates from GitHub releases").save("art/app/wide.png")
make(1920, 620, (1500, 310), 330).save("art/app/hero.png")
im = Image.new("RGB", (256, 256), BG); icon(ImageDraw.Draw(im), 128, 128, 190); im.save("art/app/icon.png")
lg = Image.new("RGBA", (1000, 220), (0, 0, 0, 0))
ImageDraw.Draw(lg).text((500, 110), NAME, font=ImageFont.truetype(bold, 120), fill=TXT + (255,), anchor="mm")
lg.save("art/app/logo.png")
