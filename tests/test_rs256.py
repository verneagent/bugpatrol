"""Reed-Solomon RS(255, 135) codec unit tests.

The screenshot pixel carrier splits its plaintext payload across 2 RS(255,135)
blocks (NSYM=120), so each block tolerates up to 60 corrupted bytes before the
whole payload is lost. These tests pin the codec's error-correction contract
and field conventions (mirrored from the canonical reedsolo library) — both
the app-side TypeScript encoder and the BugPatrol Python decoder share them.
"""

from __future__ import annotations

import unittest

from bugpatrol.watermark.rs256 import (
    _GF_EXP,
    _GF_LOG,
    FIELD_CHARAC,
    gf_inverse,
    gf_mul,
    gf_pow,
    rs_correct_msg,
    rs_encode_msg,
)


def _deterministic_bytes(seed: int, length: int) -> bytes:
    """Deterministic pseudo-random bytes (no `random` dependency)."""
    state = (seed * 0x9E3779B1) & 0xFFFFFFFF
    out = bytearray()
    for _ in range(length):
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        out.append((state >> 24) & 0xFF)
    return bytes(out)


class Rs256CodecTest(unittest.TestCase):
    NSYM = 120
    DATA_LEN = 255 - NSYM  # 135

    def _code(self, seed: int = 1) -> bytes:
        return rs_encode_msg(_deterministic_bytes(seed, self.DATA_LEN), self.NSYM)

    def _corrupt(self, codeword: bytes, n: int, seed: int) -> bytes:
        """Flip the bits of ``n`` distinct bytes of ``codeword``."""
        out = bytearray(codeword)
        state = seed * 7919
        for _ in range(n):
            state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
            idx = state % len(out)
            out[idx] ^= (state >> 16) & 0xFF or 1
        return bytes(out)

    def test_clean_codeword_passes_through(self) -> None:
        code = self._code(2)
        self.assertEqual(rs_correct_msg(code, self.NSYM), code)

    def test_corrects_up_to_60_byte_errors_per_block(self) -> None:
        # t = nsym/2 = 60: the codec MUST correct any 0..60 corrupted bytes.
        code = self._code(3)
        for nerr in range(0, 61):
            corrupted = self._corrupt(code, nerr, seed=100 + nerr)
            decoded = rs_correct_msg(corrupted, self.NSYM)
            self.assertEqual(decoded, code, msg=f"{nerr} errors should correct")

    def test_300_random_corruption_cases_within_budget(self) -> None:
        for trial in range(300):
            code = self._code(trial)
            nerr = trial % 61  # 0..60
            corrupted = self._corrupt(code, nerr, seed=trial * 31 + 7)
            decoded = rs_correct_msg(corrupted, self.NSYM)
            self.assertEqual(decoded, code, msg=f"trial {trial} ({nerr} errors)")

    def test_way_over_budget_returns_none(self) -> None:
        code = self._code(4)
        for nerr in (90, 120, 200):
            corrupted = self._corrupt(code, nerr, seed=99)
            self.assertIsNone(rs_correct_msg(corrupted, self.NSYM), msg=f"{nerr} errors")

    def test_encode_changes_data_but_preserves_prefix(self) -> None:
        # RS encoding keeps the message bytes in place and appends parity.
        data = _deterministic_bytes(5, self.DATA_LEN)
        code = rs_encode_msg(data, self.NSYM)
        self.assertEqual(len(code), FIELD_CHARAC)
        self.assertEqual(code[: self.DATA_LEN], data)

    def test_field_conventions(self) -> None:
        # x^8+x^4+x^3+x^2+1: alpha^8 = 0x1d, alpha^255 = 1 (wraps to alpha^0).
        self.assertEqual(gf_pow(2, 8), 0x1D)
        self.assertEqual(gf_pow(2, 255), 1)
        # Multiplication is invertible for nonzero elements.
        a, b = 0x4D, 0x57
        product = gf_mul(a, b)
        self.assertEqual(gf_mul(product, gf_inverse(b)), a)
        # The log/exp tables are consistent inverses.
        for x in range(1, 256):
            self.assertEqual(_GF_EXP[_GF_LOG[x]], x)


if __name__ == "__main__":
    unittest.main()
