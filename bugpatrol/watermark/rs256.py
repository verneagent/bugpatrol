"""Reed-Solomon RS(255, k) over GF(2^8) with generator polynomial 0x11d.

Self-contained errors-only codec used by the screenshot pixel carrier. The app
embeds the envelope with this codec (encoded in TypeScript, encode-only) and
BugPatrol decodes it here; both sides MUST agree on the field conventions
below or every screenshot decode fails.

NSYM=32 gives RS(255,223): up to 16 byte errors per 255-byte block can be
corrected. The carrier splits its payload across 5 blocks, so worst case is
~16 corrupted bytes per block before the whole envelope is lost — enough head
room for JPEG re-encoding plus a busy UI background at the app's 5% alpha.

Field conventions (mirror the canonical reedsolo library,
tomerfiliba-org/reedsolomon, src/reedsolo/reedsolo.py):

- Polynomials are lists with the LOWEST-degree coefficient first
  (``[c0, c1, ...]`` == ``c0 + c1*x + ...``). ``gf_poly_div`` is excepted
  (highest-degree first, used only inside the ported algorithms).
- Generator roots are ``alpha^(i + fcr)`` for i in 0..nsym-1 (fcr=0).
- ``rs_calc_syndromes`` prepends a 0 coefficient (syndrome shift), which the
  ported Berlekamp-Massey / Forney functions account for.
"""

from __future__ import annotations

FIELD_CHARAC = 255
_FCR = 0
_GENERATOR = 2

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _gf_init() -> None:
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_gf_init()


def gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def gf_pow(a: int, k: int) -> int:
    if a == 0:
        return 0
    return _GF_EXP[(_GF_LOG[a] * k) % FIELD_CHARAC]


def gf_inverse(a: int) -> int:
    if a == 0:
        raise ValueError("inverse of 0 is undefined in GF(2^8)")
    return gf_pow(a, FIELD_CHARAC - 1)


def _poly_scale(p: list[int], x: int) -> list[int]:
    return [gf_mul(c, x) for c in p]


def _poly_add(p: list[int], q: list[int]) -> list[int]:
    r = [0] * max(len(p), len(q))
    r[len(r) - len(p): len(r)] = p
    for i in range(len(q)):
        r[i + len(r) - len(q)] ^= q[i]
    return r


def _poly_mul(p: list[int], q: list[int]) -> list[int]:
    r = [0] * (len(p) + len(q) - 1)
    for j in range(len(q)):
        qj = q[j]
        if qj != 0:
            lq = _GF_LOG[qj]
            for i in range(len(p)):
                if p[i] != 0:
                    r[i + j] ^= _GF_EXP[_GF_LOG[p[i]] + lq]
    return r


def _poly_eval(poly: list[int], x: int) -> int:
    y = poly[0]
    for i in range(1, len(poly)):
        y = gf_mul(y, x) ^ poly[i]
    return y


def _poly_div(dividend: list[int], divisor: list[int]) -> tuple[list[int], list[int]]:
    """Highest-degree-first polynomial division (reedsolo convention)."""
    msg_out = list(dividend)
    divisor_len = len(divisor)
    for i in range(len(dividend) - (divisor_len - 1)):
        coef = msg_out[i]
        if coef != 0:
            for j in range(1, divisor_len):
                if divisor[j] != 0:
                    msg_out[i + j] ^= gf_mul(divisor[j], coef)
    separator = -(divisor_len - 1)
    return msg_out[:separator], msg_out[separator:]


def _rs_generator_poly(nsym: int, fcr: int = _FCR, generator: int = _GENERATOR) -> list[int]:
    g = [1]
    for i in range(nsym):
        g = _poly_mul(g, [1, gf_pow(generator, i + fcr)])
    return g


def rs_encode_msg(msg_in: bytes, nsym: int) -> bytes:
    """RS(255, 255-nsym) encode; returns msg + nsym parity bytes."""
    if len(msg_in) + nsym > FIELD_CHARAC:
        raise ValueError(f"message too long: {len(msg_in)}+{nsym} > {FIELD_CHARAC}")
    gen = _rs_generator_poly(nsym)
    msg_out = bytearray(msg_in) + bytearray(len(gen) - 1)
    lgen = [_GF_LOG[gen[j]] for j in range(len(gen))]
    for i in range(len(msg_in)):
        coef = msg_out[i]
        if coef != 0:
            lcoef = _GF_LOG[coef]
            for j in range(1, len(gen)):
                msg_out[i + j] ^= _GF_EXP[lcoef + lgen[j]]
    msg_out[:len(msg_in)] = msg_in
    return bytes(msg_out)


def _calc_syndromes(msg: bytes, nsym: int, fcr: int = _FCR, generator: int = _GENERATOR) -> list[int]:
    return [0] + [_poly_eval(list(msg), gf_pow(generator, i + fcr)) for i in range(nsym)]


def _find_error_locator(synd: list[int], nsym: int) -> list[int]:
    """Berlekamp-Massey; error locator polynomial, low-degree-first."""
    err_loc = [1]
    old_loc = [1]
    synd_shift = 0
    if len(synd) > nsym:
        synd_shift = len(synd) - nsym
    for i in range(nsym):
        K = i + synd_shift
        delta = synd[K]
        for j in range(1, len(err_loc)):
            delta ^= gf_mul(err_loc[-(j + 1)], synd[K - j])
        old_loc = old_loc + [0]
        if delta != 0:
            if len(old_loc) > len(err_loc):
                new_loc = _poly_scale(old_loc, delta)
                old_loc = _poly_scale(err_loc, gf_inverse(delta))
                err_loc = new_loc
            err_loc = _poly_add(err_loc, _poly_scale(old_loc, delta))
    for i, x in enumerate(err_loc):
        if x != 0:
            err_loc = err_loc[i:]
            break
    errs = len(err_loc) - 1
    if errs * 2 > nsym:
        raise ValueError("too many errors to correct")
    return err_loc


def _find_errata_locator(e_pos: list[int], generator: int = _GENERATOR) -> list[int]:
    e_loc = [1]
    for i in e_pos:
        e_loc = _poly_mul(e_loc, _poly_add([1], [gf_pow(generator, i), 0]))
    return e_loc


def _find_error_evaluator(synd: list[int], err_loc: list[int], nsym: int) -> list[int]:
    remainder = _poly_mul(synd, err_loc)
    remainder = remainder[len(remainder) - (nsym + 1):]
    return remainder


def _find_errors(err_loc: list[int], nmess: int, generator: int = _GENERATOR) -> list[int]:
    err_pos = []
    for i in range(nmess):
        if _poly_eval(err_loc, gf_pow(generator, i)) == 0:
            err_pos.append(nmess - 1 - i)
    if len(err_pos) != len(err_loc) - 1:
        raise ValueError("could not locate errors (Chien search count mismatch)")
    return err_pos


def _correct_errata(msg_in: bytes, synd: list[int], err_pos: list[int]) -> bytes:
    """Forney algorithm: compute and apply error magnitudes at err_pos."""
    msg = bytearray(msg_in)
    coef_pos = [len(msg) - 1 - p for p in err_pos]
    err_loc = _find_errata_locator(coef_pos)
    # reedsolo: err_eval = inverted(rs_find_error_evaluator(inverted(synd), ...)),
    # then evaluated as inverted(err_eval) — the two inversions cancel, so we
    # evaluate rs_find_error_evaluator(inverted(synd), ...) directly.
    err_eval = _find_error_evaluator(list(reversed(synd)), err_loc, len(err_loc) - 1)
    X = [gf_pow(_GENERATOR, -(FIELD_CHARAC - c)) for c in coef_pos]
    for i, xi in enumerate(X):
        xi_inv = gf_inverse(xi)
        err_loc_prime = 1
        for j in range(len(X)):
            if j != i:
                err_loc_prime = gf_mul(err_loc_prime, gf_mul(xi_inv, X[j]) ^ 1)
        if err_loc_prime == 0:
            raise ValueError("Forney: could not locate errors (locator derivative is 0)")
        y = _poly_eval(err_eval, xi_inv)
        y = gf_mul(gf_pow(xi, 1 - _FCR), y)
        magnitude = gf_mul(y, gf_inverse(err_loc_prime))
        msg[err_pos[i]] ^= magnitude
    return bytes(msg)


def _forney_syndromes(synd: list[int], pos: list[int], nmess: int) -> list[int]:
    erase_pos_reversed = [nmess - 1 - p for p in pos]
    fsynd = list(synd[1:])
    for i in range(len(pos)):
        x = gf_pow(_GENERATOR, erase_pos_reversed[i])
        for j in range(len(fsynd) - 1):
            fsynd[j] = gf_mul(fsynd[j], x) ^ fsynd[j + 1]
    return fsynd


def rs_correct_msg(msg_in: bytes, nsym: int) -> bytes | None:
    """RS decode; returns the corrected codeword (msg+parity), or None if
    uncorrectable. Raises ValueError if the message is longer than the field."""
    if len(msg_in) > FIELD_CHARAC:
        raise ValueError(f"message too long: {len(msg_in)} > {FIELD_CHARAC}")
    msg_out = bytearray(msg_in)
    synd = _calc_syndromes(msg_out, nsym)
    if max(synd) == 0:
        return bytes(msg_out)
    try:
        fsynd = _forney_syndromes(synd, [], len(msg_out))
        err_loc = _find_error_locator(fsynd, nsym)
        err_loc_inv = list(reversed(err_loc))
        err_pos = _find_errors(err_loc_inv, len(msg_out))
        msg_out = _correct_errata(bytes(msg_out), synd, err_pos)
    except ValueError:
        # Uncorrectable (too many errors for the locator / Chien mismatch).
        return None
    if max(_calc_syndromes(msg_out, nsym)) > 0:
        return None
    return bytes(msg_out)
