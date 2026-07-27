#!/usr/bin/env python3
"""Fit an icon's artwork into a square canvas so a circular avatar crop can't clip it.

Lark renders app avatars with a circular mask, so the constraint is the artwork's
farthest painted pixel from center -- NOT its bounding-box corners. A shield with
rounded shoulders sits well inside its own bbox corners, so sizing off the bbox
diagonal underestimates the safe size badly (69% vs the real 86% for this icon).
This script measures the true shape radius and reports the clip margin.

Usage:
    python3 fit_circle_safe.py SOURCE OUT --height-ratio 0.80 [--size 512]
    python3 fit_circle_safe.py SOURCE OUT --max            # largest safe size
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

BACKGROUND = (255, 255, 255)
# Pixels differing from the background by less than this are treated as background.
ALPHA_THRESHOLD = 12
# Keep a 2% slack against the circle so anti-aliased edges never touch the mask.
SAFETY = 0.98


def content_mask(image: Image.Image) -> Image.Image:
    flat = Image.new("RGB", image.size, BACKGROUND)
    diff = ImageChops.difference(image, flat).convert("L")
    return diff.point(lambda p: 255 if p > ALPHA_THRESHOLD else 0)


def max_shape_radius(mask: Image.Image) -> float:
    """Farthest painted pixel from the mask's center, in pixels."""
    width, height = mask.size
    pixels = mask.load()
    cx, cy = width / 2, height / 2
    longest = 0.0
    for y in range(height):
        for x in range(width):
            if pixels[x, y] > 40:
                longest = max(longest, math.hypot(x - cx, y - cy))
    return longest


def render(source: Path, out: Path, *, size: int, height_ratio: float, preview: Path | None):
    image = Image.open(source).convert("RGB")
    bbox = content_mask(image).getbbox()
    if bbox is None:
        raise SystemExit(f"{source}: no artwork found (image is uniform background)")
    art = image.crop(bbox)
    art_mask = content_mask(art)
    src_w, src_h = art.size

    max_ratio = (size / 2 * SAFETY) / max_shape_radius(art_mask) * src_h / size
    if height_ratio is None:
        height_ratio = max_ratio

    new_h = max(1, round(size * height_ratio))
    new_w = max(1, round(src_w * new_h / src_h))
    art = art.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGB", (size, size), BACKGROUND)
    offset = ((size - new_w) // 2, (size - new_h) // 2)
    canvas.paste(art, offset)
    canvas.save(out)

    scaled_mask = art_mask.resize((new_w, new_h), Image.LANCZOS)
    placed = Image.new("L", (size, size), 0)
    placed.paste(scaled_mask, offset)
    radius = max_shape_radius(placed)
    circle = size / 2
    verdict = "FITS" if radius < circle else "CLIPPED"

    print(f"{out}: artwork {new_w}x{new_h} = {100 * new_h / size:.0f}% of frame")
    print(f"  shape radius {radius:.0f}px vs circle {circle:.0f}px -> {verdict} (margin {circle - radius:.0f}px)")
    print(f"  largest safe height-ratio for this artwork: {max_ratio:.3f}")

    if preview:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        shot = Image.new("RGBA", (size, size), (230, 230, 235, 255))
        shot.paste(canvas.convert("RGBA"), (0, 0), mask)
        shot.convert("RGB").save(preview)
        print(f"  circular-clip preview -> {preview}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="artwork on a flat white background")
    parser.add_argument("out", type=Path, help="fitted square icon to write")
    parser.add_argument("--size", type=int, default=512, help="output edge length (default 512)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--height-ratio", type=float, help="artwork height as a fraction of the canvas, e.g. 0.80")
    group.add_argument("--max", action="store_true", help="use the largest circle-safe size")
    parser.add_argument("--preview", type=Path, help="also write a circular-clip preview here")
    args = parser.parse_args()

    render(
        args.source,
        args.out,
        size=args.size,
        height_ratio=None if args.max else args.height_ratio,
        preview=args.preview,
    )


if __name__ == "__main__":
    main()
