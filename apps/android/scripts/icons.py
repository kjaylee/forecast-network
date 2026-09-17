"""Render the Forecast app icon: a confidence ring on navy with the wordmark's period.

The mark is drawn in code so every density and the store artwork stay identical.
Run from apps/android: python3 scripts/icons.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "android/app/src/main/res"
ASSETS = ROOT / "assets"
FAVICON = ROOT.parent / "web/public/favicon.svg"

NAVY = (15, 23, 42, 255)
BLUE = (36, 86, 237, 255)
TRACK = (255, 255, 255, 56)
WHITE = (255, 255, 255, 255)
FILL = 0.70               # the ring shows a 70% probability
SCALE = 4                 # supersampling factor
MARK_SPREAD = 0.60
ADAPTIVE_SIZE_DP = 108
ADAPTIVE_VIEWPORT_DP = 72
LEGACY_SIZE_DP = 48
ADAPTIVE_SPREAD = MARK_SPREAD * ADAPTIVE_VIEWPORT_DP / ADAPTIVE_SIZE_DP

DENSITIES = {"ldpi": 36, "mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def mark(size: int, spread: float, ring_color=BLUE, dot_color=WHITE, background=None) -> Image.Image:
    """The ring + dot, its outer diameter = spread * size, centred on an optional background."""
    n = size * SCALE
    image = Image.new("RGBA", (n, n), background or (0, 0, 0, 0))
    cx = cy = n / 2
    radius = n * spread / 2
    width = radius * 0.38
    box = [cx - radius, cy - radius, cx + radius, cy + radius]
    # The faint track is translucent; composite it so it blends instead of replacing pixels.
    track = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(track).arc(box, start=0, end=360, fill=TRACK, width=int(width))
    image = Image.alpha_composite(image, track)
    draw = ImageDraw.Draw(image)
    start, end = -90, -90 + FILL * 360
    draw.arc(box, start=start, end=end, fill=ring_color, width=int(width))
    for angle in (start, end):
        a = math.radians(angle)
        px, py = cx + (radius - width / 2) * math.cos(a), cy + (radius - width / 2) * math.sin(a)
        draw.ellipse([px - width / 2, py - width / 2, px + width / 2, py + width / 2], fill=ring_color)
    dot = radius * 0.28
    draw.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=dot_color)
    return image.resize((size, size), Image.LANCZOS)


def rounded(image: Image.Image, radius_ratio: float) -> Image.Image:
    n = image.width
    mask = Image.new("L", (n * SCALE, n * SCALE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, n * SCALE, n * SCALE], radius=int(n * SCALE * radius_ratio), fill=255)
    out = image.copy()
    out.putalpha(mask.resize((n, n), Image.LANCZOS))
    return out


def circle(image: Image.Image) -> Image.Image:
    n = image.width
    mask = Image.new("L", (n * SCALE, n * SCALE), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, n * SCALE, n * SCALE], fill=255)
    out = image.copy()
    out.putalpha(mask.resize((n, n), Image.LANCZOS))
    return out


def adaptive_xml(themed: bool = False) -> str:
    monochrome = '\n    <monochrome android:drawable="@mipmap/ic_launcher_monochrome" />' if themed else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <background android:drawable="@mipmap/ic_launcher_background" />\n'
        '    <foreground android:drawable="@mipmap/ic_launcher_foreground" />'
        f'{monochrome}\n</adaptive-icon>\n'
    )


def favicon_svg() -> str:
    # SVG strokes straddle their radius; Pillow's arcs draw inward from the box.
    outer_radius = 64 * MARK_SPREAD / 2
    width = outer_radius * 0.38
    radius = outer_radius - width / 2
    circumference = math.tau * radius
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'
        '  <rect width="64" height="64" rx="14" fill="#0f172a"/>\n'
        f'  <circle cx="32" cy="32" r="{radius:.4f}" fill="none" stroke="#fff" '
        f'stroke-opacity="{TRACK[3] / 255:.4f}" stroke-width="{width:.4f}"/>\n'
        f'  <circle cx="32" cy="32" r="{radius:.4f}" fill="none" stroke="#2456ed" '
        f'stroke-width="{width:.4f}" stroke-linecap="round" '
        f'stroke-dasharray="{circumference * FILL:.4f} {circumference:.4f}" '
        'transform="rotate(-90 32 32)"/>\n'
        f'  <circle cx="32" cy="32" r="{outer_radius * 0.28:.4f}" fill="#fff"/>\n'
        '</svg>\n'
    )


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    store = mark(1024, MARK_SPREAD, background=NAVY)
    store.save(ASSETS / "icon-only.png")
    mark(1024, ADAPTIVE_SPREAD).save(ASSETS / "icon-foreground.png")
    Image.new("RGBA", (1024, 1024), NAVY).save(ASSETS / "icon-background.png")
    for density, size in DENSITIES.items():
        folder = RES / f"mipmap-{density}"
        folder.mkdir(exist_ok=True)
        # The full 108dp layer includes overscan around a 72dp viewport. Keep the
        # visible mark identical to legacy launchers and inside the 66dp safe zone.
        adaptive_size = round(size * ADAPTIVE_SIZE_DP / LEGACY_SIZE_DP)
        mark(adaptive_size, ADAPTIVE_SPREAD).save(folder / "ic_launcher_foreground.png")
        mark(adaptive_size, ADAPTIVE_SPREAD, ring_color=WHITE).save(folder / "ic_launcher_monochrome.png")
        Image.new("RGBA", (adaptive_size, adaptive_size), NAVY).save(folder / "ic_launcher_background.png")
        legacy = mark(size, MARK_SPREAD, background=NAVY)
        rounded(legacy, 0.18).save(folder / "ic_launcher.png")
        circle(legacy).save(folder / "ic_launcher_round.png")
    for version, themed in ((26, False), (33, True)):
        folder = RES / f"mipmap-anydpi-v{version}"
        folder.mkdir(exist_ok=True)
        for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
            (folder / name).write_text(adaptive_xml(themed))
    FAVICON.write_text(favicon_svg())
    # Splash screens: keep each existing size, navy ground, mark at ~30% of the short edge.
    for splash in RES.glob("drawable*/splash*.png"):
        current = Image.open(splash)
        w, h = current.size
        image = Image.new("RGBA", (w, h), NAVY)
        size = int(min(w, h) * 0.30)
        glyph = mark(size, 1.0)
        image.alpha_composite(glyph, ((w - size) // 2, (h - size) // 2))
        image.save(splash)
    for name in ("splash.png", "splash-dark.png"):
        target = ASSETS / name
        if target.exists():
            w, h = Image.open(target).size
            image = Image.new("RGBA", (w, h), NAVY)
            size = int(min(w, h) * 0.30)
            image.alpha_composite(mark(size, 1.0), ((w - size) // 2, (h - size) // 2))
            image.save(target)
    print("icons and splash screens written to", ASSETS, "and", RES)


if __name__ == "__main__":
    main()
