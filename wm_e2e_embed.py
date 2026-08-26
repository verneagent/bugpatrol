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
GRID_W = 768  # viewBox width: 128 cols × 2 cells × 3px
GRID_H = 864  # viewBox height: 288 rows × 3px (6-block carrier grid)
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
        origins.append((W - GRID_W - OFFSET, H - GRID_H - OFFSET))
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


def run_split_flow(data: bytes, suffix: str) -> None:
    """Watcher-extract -> issue body line -> runner-decrypt, end to end.

    Mirrors the production split: the relay watcher has NO private key, so it
    only extracts the encrypted envelope candidates into the issue body; the
    triage runner decrypts them with the GH Actions key and GCM auth picks the
    clean candidate. Uses the actual pipeline functions (not a re-derivation).
    """
    from bugpatrol.triage_context import extract_media_evidence, resolve_media_watermarks
    from bugpatrol.watermark.extractor import extract_envelope_candidates
    from bugpatrol.watermark.keys import WatermarkKeyStore
    from bugpatrol.watermark.reporter import (
        NO_WATERMARK_NOTE,
        candidates_to_compact_json,
        render_payload_summary,
    )
    from bugpatrol.watermark.types import DEFAULT_KEY_ID, PAYLOAD_REQUIRED_FIELDS

    candidates = extract_envelope_candidates(data)
    if candidates:
        line = candidates_to_compact_json(candidates)
        body_line = f"  - watermark-candidates: {line}"
    else:
        line = NO_WATERMARK_NOTE
        body_line = f"  - watermark: {NO_WATERMARK_NOTE}"
    print(f"=== {suffix}: split flow ===")
    print(f"watcher extract -> {len(candidates)} candidate(s); issue-body line: {body_line[:72]}...")
    media = extract_media_evidence(f"- image: https://assets/x.png\n{body_line}\n")
    assert len(media) == 1, f"expected 1 media item, got {len(media)}"
    resolved = resolve_media_watermarks(
        media, key_store=WatermarkKeyStore(keys={DEFAULT_KEY_ID: PRIV})
    )
    payload = json.loads(resolved[0].watermark)
    missing = [f for f in PAYLOAD_REQUIRED_FIELDS if not payload.get(f)]
    assert not missing, f"payload missing required fields: {missing}"
    print(f"runner decrypted payload: {render_payload_summary(payload)}")
    print(f"required fields ({len(PAYLOAD_REQUIRED_FIELDS)}): all present")
    print()


if __name__ == "__main__":
    from PIL import Image

    png = render_screenshot((1080, 2340))
    jp = io.BytesIO()
    Image.open(io.BytesIO(png)).save(jp, format="JPEG", quality=85)
    ui = with_obstructing_ui(png)
    jp2 = io.BytesIO()
    Image.open(io.BytesIO(ui)).save(jp2, format="JPEG", quality=85)

    for label, data in (
        (".png", png),
        ("-q85.jpg", jp.getvalue()),
        ("-ui-edge.png", ui),
        ("-ui-edge-q85.jpg", jp2.getvalue()),
    ):
        run_decode(data, label)
        run_split_flow(data, label)
