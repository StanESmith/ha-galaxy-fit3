#!/usr/bin/env python3
"""Draw the integration's icon: a generic smartwatch with a pulse line (original artwork).

Home Assistant (2026.3+) shows ``custom_components/galaxy_fit3/brand/icon.png`` for the integration.
Writes the light and dark icons at 256 px and 512 px (``@2x``) with a transparent background. No
Samsung or Home Assistant artwork is used.

  uv run --with pillow scripts/make_brand_images.py [--out DIR]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

CANVAS = 1024  # drawn large, then scaled down for smooth edges
PALETTES = {
    "": {"band": "#455A64", "body": "#1E88E5", "screen": "#FFFFFF", "pulse": "#E53935"},
    "dark_": {"band": "#90A4AE", "body": "#64B5F6", "screen": "#1B1F23", "pulse": "#FF6E6B"},
}
PULSE = [(318, 512), (420, 512), (468, 392), (540, 646), (596, 462), (632, 512), (706, 512)]


def draw(colors: dict[str, str]) -> Image.Image:
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((352, 0, 672, 330), radius=64, fill=colors["band"])  # upper band
    d.rounded_rectangle((352, 694, 672, CANVAS), radius=64, fill=colors["band"])  # lower band
    d.rounded_rectangle((226, 196, 798, 828), radius=140, fill=colors["body"])  # case
    d.rounded_rectangle((286, 256, 738, 768), radius=84, fill=colors["screen"])  # display
    for a, b in zip(PULSE, PULSE[1:]):  # heartbeat
        d.line([a, b], fill=colors["pulse"], width=44)
    for x, y in PULSE:  # round the joints and ends
        d.ellipse((x - 22, y - 22, x + 22, y + 22), fill=colors["pulse"])
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "custom_components" / "galaxy_fit3" / "brand")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for prefix, colors in PALETTES.items():
        art = draw(colors)
        for suffix, size in (("", 256), ("@2x", 512)):
            path = args.out / f"{prefix}icon{suffix}.png"
            art.resize((size, size), Image.LANCZOS).save(path, optimize=True)
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
