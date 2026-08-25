"""Render the APP's actual 3× interleaved path geometry onto a realistic
screenshot and run the BugPatrol CLI decoder against it — end-to-end.

Consumes the artifacts exported by the fived jest harness
(lib/__tests__/verify-watermark-e2e.test.ts):
  $TMPDIR/wm-e2e-envelope.json  — the real prod-mode encrypted envelope
  $TMPDIR/wm-e2e-private.pem    — its matching private key
  $TMPDIR/wm-e2e-paths.json     — the ACTUAL darkPath/lightPath the app renders

The path strings are parsed cell-by-cell and drawn at the same corners and
offset the app's root overlay uses, so this exercises the real app geometry
(3× bit-interleaved) against the real BugPatrol extractor — not a re-derivation.
"""
import io, json, os, random, re, subprocess, sys

TMP = os.environ.get("TMPDIR", "/tmp/claude-501").rstrip("/")
CELL = 3
GRID = 768  # viewBox size: 128 cols × 2 cells × 3px
OFFSET = 18
ENVELOPE = json.load(open(f"{TMP}/wm-e2e-envelope.json"))
PRIV = open(f"{TMP}/wm-e2e-private.pem").read()
PATHS = json.load(open(f"{TMP}/wm-e2e-paths.json"))

_CELL_RE = re.compile(r"M(\d+) (\d+)h3v3h-3z")


def parse_cells(path: str) -> list[tuple[int, int]]:
    return [(int(m.group(1)), int(m.group(2))) for m in _CELL_RE.finditer(path)]


def render_screenshot(size: tuple[int, int], corner: str = "both") -> bytes:
    from PIL import Image, ImageDraw
    W, H = size
    img = Image.new("RGB", (W, H), (245, 245, 248))
    d = ImageDraw.Draw(img, "RGBA")
    # Page-like noise first — the real app renders the carrier as a top-most
    # overlay, so the cells must paint last (over any background).
    rnd = random.Random(7)
    for _ in range(180):
        x0, y0 = rnd.randint(0, W), rnd.randint(0, H)
        d.rectangle((x0, y0, x0 + 90, y0 + 12), fill=(230, 231, 236, 255))
    dark = parse_cells(PATHS["darkPath"])
    light = parse_cells(PATHS["lightPath"])
    origins = []
    if corner in ("top_left", "both"):
        origins.append((OFFSET, OFFSET))
    if corner in ("bottom_right", "both"):
        origins.append((W - GRID - OFFSET, H - GRID - OFFSET))
    for ox, oy in origins:
        for x, y in dark:
            d.rectangle((ox + x, oy + y, ox + x + CELL - 1, oy + y + CELL - 1), fill=(0, 0, 0, 13))
        for x, y in light:
            d.rectangle((ox + x, oy + y, ox + x + CELL - 1, oy + y + CELL - 1), fill=(255, 255, 255, 13))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def run_decode(data: bytes, suffix: str) -> None:
    path = f"{TMP}/wm-e2e-screenshot{suffix}"
    with open(path, "wb") as f:
        f.write(data)
    env = dict(os.environ)
    env["FIVED_WATERMARK_PRIVATE_KEY_PEM"] = PRIV
    r = subprocess.run(
        [sys.executable, "-m", "bugpatrol", "watermark", "decode", "--image", path, "--json"],
        capture_output=True, text=True, env=env,
    )
    print(f"=== {suffix} (exit {r.returncode}) ===")
    print(r.stdout.strip() or r.stderr.strip())
    print()


def with_obstructing_ui(png_bytes: bytes) -> bytes:
    """Overlay a UI edge across the top-left carrier: a vertical dark bar +
    text-like rows whose edges invert cell polarity (the failure mode the 3×
    bit-interleave + byte majority was built to survive)."""
    from PIL import Image, ImageDraw
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")
    # A tall sidebar at the screen edge that the carrier sits beside.
    d.rectangle((0, 0, 60, 2340), fill=(40, 42, 50, 255))
    # Text-like runs crossing the top-left carrier rows.
    d.rectangle((40, 140, 400, 168), fill=(90, 92, 100, 255))
    d.rectangle((40, 180, 520, 204), fill=(70, 72, 80, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    png = render_screenshot((1080, 2340))
    run_decode(png, ".png")
    # JPEG q85 — the pipeline's convert_images_to_jpeg transcode
    from PIL import Image
    jp = io.BytesIO()
    Image.open(io.BytesIO(png)).save(jp, format="JPEG", quality=85)
    run_decode(jp.getvalue(), "-q85.jpg")
    # UI obstruction across the carrier (majority-vote recovery check)
    run_decode(with_obstructing_ui(png), "-ui-edge.png")
    jp2 = io.BytesIO()
    Image.open(io.BytesIO(with_obstructing_ui(png))).save(jp2, format="JPEG", quality=85)
    run_decode(jp2.getvalue(), "-ui-edge-q85.jpg")
