"""Render the app's ACTUAL spread-spectrum plaintext watermark geometry onto
realistic screenshots and run the BugPatrol CLI decoder + the watcher->runner
pipeline against them — end to end.

Consumes the artifacts exported by the fived jest harness
(app/lib/__tests__/verify-watermark-e2e.test.ts):
  $TMPDIR/wm-e2e-payload.json  — the real dev-mode plaintext payload (with uid)
  $TMPDIR/wm-e2e-paths.json    — the ACTUAL darkPath/lightPath the app renders

The path strings are parsed chip-by-chip and drawn at the SAME nominal
1080×2340 coordinates the app's full-screen SVG (viewBox 0 0 1080 2340)
uses, so this exercises the real app geometry (LCG-spread H2 pairs carrying
RS(255,135)×2) against the real BugPatrol extractor — not a re-derivation.

Five channel forms cover the production pipeline (Lark downscales screenshots
to 1080 wide + re-encodes JPEG q85):
  -png            nominal PNG            (exact geometry, no compression)
  -q85.jpg        nominal JPEG q85       (Lark compression)
  -ui.png         nominal PNG + UI bars  (partial chip occlusion)
  -ui-q85.jpg     nominal + UI + q85     (worst realistic case)
  -native-q85.jpg native 1170×2532 q85   (Lark 1080-downscale round-trip:
                                          the extractor LANCZOS-resizes back to
                                          nominal before reading)
"""
import io
import json
import os
import random
import re
import subprocess
import sys

from bugpatrol.watermark.types import PAYLOAD_REQUIRED_FIELDS

TMP = os.environ.get("TMPDIR", "/tmp/claude-501").rstrip("/")
NOMINAL_W = 1080
NOMINAL_H = 2340
NATIVE_W = 1170
NATIVE_H = 2532
CELL = 3
REQUIRED_FIELDS = PAYLOAD_REQUIRED_FIELDS  # canonical contract (uid is dev-only)

EXPECTED_PAYLOAD = json.load(open(f"{TMP}/wm-e2e-payload.json"))
PATHS = json.load(open(f"{TMP}/wm-e2e-paths.json"))

_CELL_RE = re.compile(r"M(\d+) (\d+)h3v3h-3z")


def parse_cells(path: str) -> list[tuple[int, int]]:
    """Chip top-left corners (nominal coords) from a path string."""
    return [(int(m.group(1)), int(m.group(2))) for m in _CELL_RE.finditer(path)]


def render_screenshot(
    *,
    native: bool = False,
    occlude: bool = False,
    quality: int | None = None,
) -> bytes:
    """Render the real TS dark/light chips onto a screenshot.

    ``native`` scales the nominal TS geometry to a native resolution (the
    app's real screenshot before Lark downscales). ``occlude`` paints a few
    UI bars over the carrier (the failure mode RS ECC must survive).
    ``quality`` set -> JPEG re-encode (Lark's).
    """
    from PIL import Image, ImageDraw

    if native:
        W, H = NATIVE_W, NATIVE_H
        sx, sy = W / NOMINAL_W, H / NOMINAL_H
    else:
        W, H = NOMINAL_W, NOMINAL_H
        sx = sy = 1.0
    img = Image.new("RGB", (W, H), (245, 245, 248))
    d = ImageDraw.Draw(img, "RGBA")
    # Page-like noise first — the app renders the carrier as a top-most
    # overlay, so the chips must paint last (over any background).
    rnd = random.Random(7)
    for _ in range(220):
        x0, y0 = rnd.randint(0, W), rnd.randint(0, H)
        d.rectangle((x0, y0, x0 + 90, y0 + 12), fill=(230, 231, 236, 255))
    dark = parse_cells(PATHS["darkPath"])
    light = parse_cells(PATHS["lightPath"])
    for x, y in dark:
        px, py = round(x * sx), round(y * sy)
        d.rectangle((px, py, px + CELL - 1, py + CELL - 1), fill=(0, 0, 0, 46))
    for x, y in light:
        px, py = round(x * sx), round(y * sy)
        d.rectangle((px, py, px + CELL - 1, py + CELL - 1), fill=(255, 255, 255, 46))
    if occlude:
        # A dark sidebar at the screen edge plus text-like runs crossing the
        # carrier; inverted-edges and full-occlusion cells corrupt reads that
        # the pair-vote + RS budget must absorb.
        d.rectangle((0, 0, 60, H), fill=(40, 42, 50, 255))
        d.rectangle((40, int(H * 0.42), 400, int(H * 0.42) + 28), fill=(90, 92, 100, 255))
        d.rectangle((40, int(H * 0.58), 520, int(H * 0.58) + 24), fill=(70, 72, 80, 255))
    out = io.BytesIO()
    if quality is not None:
        img.save(out, format="JPEG", quality=quality)
    else:
        img.save(out, format="PNG")
    return out.getvalue()


def run_cli_decode(data: bytes, suffix: str) -> dict[str, object]:
    """Run the real CLI decoder; return its --json result dict."""
    path = f"{TMP}/wm-e2e-screenshot{suffix}"
    with open(path, "wb") as f:
        f.write(data)
    r = subprocess.run(
        [sys.executable, "-m", "bugpatrol", "watermark", "decode", "--image", path, "--json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise AssertionError(f"CLI decode failed (exit {r.returncode}): {r.stderr.strip()}")
    return json.loads(r.stdout)


def run_watcher_split(data: bytes, suffix: str) -> None:
    """Mirror the production watcher->runner pipeline on the same bytes.

    Watcher (no key): extract_plaintext_payload -> issue-body `- watermark:`
    line. Runner: extract_media_evidence parses the line back and renders it
    into triage context. Uses the actual pipeline functions, not a re-derivation.
    """
    from bugpatrol.triage_context import extract_media_evidence
    from bugpatrol.watermark.extractor import extract_plaintext_payload
    from bugpatrol.watermark.reporter import (
        NO_WATERMARK_NOTE,
        payload_to_compact_json,
        render_payload_summary,
        watermark_failure_note,
    )
    from bugpatrol.watermark.types import ERROR_BAD_ENVELOPE

    try:
        payload_bytes = extract_plaintext_payload(data)
    except Exception as exc:  # mirror resources._decode_watermark's handling
        line = watermark_failure_note(ERROR_BAD_ENVELOPE)
        print(f"  watcher extract raised {type(exc).__name__}: {exc}")
    else:
        line = NO_WATERMARK_NOTE if payload_bytes is None else payload_bytes.decode("utf-8")
    body_line = f"  - watermark: {line}"
    media = extract_media_evidence(f"- image: https://assets/x.png\n{body_line}\n")
    assert len(media) == 1, f"expected 1 media item, got {len(media)}"
    if line == NO_WATERMARK_NOTE:
        assert media[0].watermark == NO_WATERMARK_NOTE, "not-found note lost in parse"
        print(f"  watcher->runner: 未找到水印 (carrier absent)")
        return
    if line.startswith("水印解码失败"):
        print(f"  watcher->runner: {line}")
        return
    payload = json.loads(media[0].watermark)
    missing = [f for f in REQUIRED_FIELDS if not payload.get(f)]
    assert not missing, f"payload missing required fields: {missing}"
    assert payload.get("uid") == "42", f"expected dev uid=42, got {payload.get('uid')!r}"
    assert payload_to_compact_json(payload) == media[0].watermark, "round-trip mismatch"
    print(f"  watcher->runner: {render_payload_summary(payload)}")
    print(f"  required fields ({len(REQUIRED_FIELDS)}): all present; uid: dev-only, present")


def main() -> int:
    failures: list[str] = []
    forms = (
        (".png", render_screenshot()),
        ("-q85.jpg", render_screenshot(quality=85)),
        ("-ui.png", render_screenshot(occlude=True)),
        ("-ui-q85.jpg", render_screenshot(occlude=True, quality=85)),
        ("-native-q85.jpg", render_screenshot(native=True, quality=85)),
    )
    for suffix, data in forms:
        print(f"=== {suffix} ===")
        result = run_cli_decode(data, suffix)
        if not result.get("found"):
            failures.append(f"{suffix}: not found ({result.get('error')})")
            print(f"  CLI: NOT FOUND ({result.get('error')})")
        else:
            payload = result.get("payload") or {}
            ok = payload == EXPECTED_PAYLOAD
            if not ok:
                failures.append(f"{suffix}: payload mismatch")
            print(f"  CLI: found confidence={result.get('confidence')} payload_match={ok}")
        run_watcher_split(data, suffix)
        print()
    if failures:
        print(f"E2E FAILED: {len(failures)} form(s) failed")
        for msg in failures:
            print(f"  - {msg}")
        return 1
    print("E2E OK: 5/5 channel forms decoded, TS geometry == Python extractor, "
          "watcher->runner split round-trips the payload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
