#!/usr/bin/env python3
"""Test-only OProof equivalence oracle for cpyte's integer rewrite rules.

OProof is used here strictly as an *oracle*, never as a code generator.  The
experimental rewrite hook in bytecoding.py was removed after it miscompiled
cpyte (floor `//` mapped to OProof's truncating `divs`; `x % 1` folded to `x`).
This test cross-checks the identities cpyte's _try_algebraic_simplify relies on
against (a) OProof's prover and (b) an independent, exhaustive bit-exact
evaluator over int8.  The independent evaluator is the final authority: OProof
0.1.0's built-in rewriter has had a soundness bug (rems(x, 1) -> x), so a bare
`status == "proven"` is not enough.

Runs under pytest, or directly:  python3.14 test/test_opreof_proofs.py
Set OPROOF_PATH to an OProof checkout, or `pip install opreof`.  If OProof is
unavailable the test is skipped, not failed — it is a dev/CI oracle, not a
runtime dependency of the compiler.
"""

from __future__ import annotations

import itertools
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

# Prefer an explicit OPROOF_PATH.  Without one, prefer the local development
# checkout when present so a stale globally installed 0.1.0 cannot make the
# oracle fail; otherwise fall back to a pip-installed copy.  OProof 0.1.1+ ships
# the real `opreof/` package; 0.1.0 had a flat module layout and is not suitable
# for this oracle.
_preferred = os.environ.get("OPROOF_PATH")
if not (_preferred and Path(_preferred).is_dir()):
    _local_checkout = "/Users/main/PycharmProjects/WEW-Oproof"
    _preferred = _local_checkout if Path(_local_checkout).is_dir() else None
if _preferred and _preferred not in sys.path:
    sys.path.insert(0, _preferred)

try:
    import opreof as o
except ImportError as exc:  # pragma: no cover - depends on dev environment
    o = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


def _require_opreof():
    if o is None:
        raise unittest.SkipTest(
            f"OProof not available ({_IMPORT_ERROR}); set OPROOF_PATH or pip install opreof"
        )
    return o


# ---------------------------------------------------------------------------
# Independent concrete evaluator (the safety net against prover bugs)
# ---------------------------------------------------------------------------


def _ty(expr, default):
    return getattr(expr, "type", None) or default


def _eval(expr, env, ty):
    """Evaluate an OProof Expr with machine-integer semantics.  Pure Python."""
    if isinstance(expr, o.Lit):
        return expr.value
    if isinstance(expr, o.Var):
        return env[expr.name]
    if isinstance(expr, o.Select):
        branch = expr.then_expr if _eval(expr.cond, env, getattr(o, "BOOL", ty)) else expr.else_expr
        return _eval(branch, env, ty)

    if not isinstance(expr, o.Op):
        raise AssertionError(f"unsupported expression: {expr!r}")

    op = expr.op
    args = [_eval(a, env, _ty(a, ty)) for a in expr.args]
    result_ty = _ty(expr, ty) or ty

    def wrap(v):
        return result_ty.wrap(v)

    if op in ("add", "sub", "mul", "and", "or", "xor", "shl", "shru", "shrs",
              "divu", "divs", "remu", "rems"):
        a, b = args
        bits = result_ty.width
        mask = result_ty.mask
        if op == "add":
            return wrap(a + b)
        if op == "sub":
            return wrap(a - b)
        if op == "mul":
            return wrap(a * b)
        if op == "and":
            return wrap(a & b)
        if op == "or":
            return wrap(a | b)
        if op == "xor":
            return wrap(a ^ b)
        if op == "shl":
            return wrap(a << b)
        if op == "shru":
            return wrap((a % (mask + 1)) >> b)
        if op == "shrs":
            return wrap(a >> b)
        if op == "divu":
            d = b % (mask + 1)
            if d == 0:
                raise ZeroDivisionError
            return wrap((a % (mask + 1)) // d)
        q = abs(a) // abs(b)  # divs/rems: truncate toward zero
        if (a < 0) != (b < 0):
            q = -q
        if op == "divs":
            return wrap(q)
        return wrap(a - b * q)  # rems: sign follows the dividend

    if op == "neg":
        return wrap(-args[0])
    if op == "not":
        return wrap(~args[0])  # OProof `not` is bitwise complement
    if op in ("extz", "exts", "trunc"):
        return wrap(args[0])

    if op in ("eq", "ne", "ltu", "lteu", "gtu", "gteu", "lts", "ltes", "gts", "gtes"):
        a, b = args
        cmp_map = {
            "eq": a == b, "ne": a != b,
            "ltu": a < b, "lteu": a <= b, "gtu": a > b, "gteu": a >= b,
            "lts": a < b, "ltes": a <= b, "gts": a > b, "gtes": a >= b,
        }
        return int(cmp_map[op])

    raise AssertionError(f"unsupported operation in oracle: {op}")


def _domain(ty):
    return range(ty.min_value, ty.max_value + 1)


# ---------------------------------------------------------------------------
# Identities mirrored from cpyte's _try_algebraic_simplify
# ---------------------------------------------------------------------------


def _identity_cases(o):
    """(label, lhs, rhs, variables, prover_must_succeed)."""
    i8 = o.int8
    x = o.Var("x", i8)
    y = o.Var("y", i8)
    z = o.Lit(0, i8)
    one = o.Lit(1, i8)
    two = o.Lit(2, i8)

    def op(name, *args):
        return o.Op(name, args, i8)

    return [
        ("x + 0 == x", op("add", x, z), x, (x,), True),
        ("0 + x == x", op("add", z, x), x, (x,), True),
        ("x - 0 == x", op("sub", x, z), x, (x,), True),
        ("0 - x == -x", op("sub", z, x), op("neg", x), (x,), False),
        ("x - x == 0", op("sub", x, x), z, (x,), True),
        ("x ^ x == 0", op("xor", x, x), z, (x,), True),
        ("x & x == x", op("and", x, x), x, (x,), True),
        ("x | x == x", op("or", x, x), x, (x,), True),
        ("x * 0 == 0", op("mul", x, z), z, (x,), False),
        ("x * 1 == x", op("mul", x, one), x, (x,), False),
        ("x << 1 == x * 2", op("shl", x, one), op("mul", x, two), (x,), True),
        ("x * 2 == x << 1", op("mul", x, two), op("shl", x, one), (x,), True),
        ("(y - x) + x == y", op("add", op("sub", y, x), x), y, (x, y), False),
        ("x % 1 == 0", op("rems", x, one), z, (x,), True),
    ]


def test_cpyte_identities_are_bit_exact():
    o = _require_opreof()
    kernel = o.Kernel()  # fresh, isolated store: no axiom persistence side effects
    inputs = {"x": o.int8, "y": o.int8}
    values = _domain(o.int8)

    for label, lhs, rhs, variables, prover_must_succeed in _identity_cases(o):
        # Ground truth: exhaustive concrete evaluation over every int8 input.
        for point in itertools.product(values, repeat=len(variables)):
            env = {v.name: value for v, value in zip(variables, point)}
            assert _eval(lhs, env, o.int8) == _eval(rhs, env, o.int8), (
                f"{label} is not bit-exact at {env}"
            )

        # Cross-check with OProof.  `not_proven` is allowed (the prover is
        # incomplete), but a `proven` verdict must carry a complete check.
        proof = kernel.prove_expr(lhs, rhs, inputs, generalize=False)
        if proof.status == "proven":
            assert proof.checked in (None, "complete", "sampled"), (label, proof)
        if prover_must_succeed:
            assert proof.status == "proven", (label, proof)


def test_false_identities_are_not_proven():
    o = _require_opreof()
    kernel = o.Kernel()
    i8, i32 = o.int8, o.int32
    x32 = o.Var("x", i32)
    x8 = o.Var("x", i8)

    # Regression for the OProof builtin_rewrite bug fixed in core.py: the
    # generic `divisor == 1 -> dividend` rule used to shadow the rem rule.
    bad_rem = kernel.prove_expr(
        o.Op("rems", (x8, o.Lit(1, i8)), i8), x8, {"x": i8}, generalize=False
    )
    assert bad_rem.status == "not_proven", bad_rem

    # Signed div by 2**k is NOT an arithmetic shift for negative operands;
    # cpyte's own pow2 theory uses a sign-corrected form instead.
    bad_div = kernel.prove_expr(
        o.Op("divs", (x32, o.Lit(2, i32)), i32),
        o.Op("shrs", (x32, o.Lit(1, i32)), i32),
        {"x": i32},
        generalize=False,
    )
    assert bad_div.status == "not_proven", bad_div


def test_semantic_gaps_that_forbid_the_old_bridge_mapping():
    """These are *not* OProof failures: they are cpyte/OProof semantic gaps.

    They document why signed `//` and logical `not` must never be translated
    directly into OProof `divs` / `not` by an automatic rewrite.
    """
    o = _require_opreof()
    i8 = o.int8
    x = o.Var("x", i8)

    # cpyte `//` floors; OProof divs truncates toward zero.
    trunc = _eval(o.Op("divs", (o.Lit(7, i8), o.Lit(-2, i8)), i8), {}, i8)
    floor = 7 // -2
    assert trunc != floor and floor == -4, (trunc, floor)

    # cpyte `not` is logical (returns 0/1); OProof `not` is bitwise complement.
    bitwise = _eval(o.Op("not", (o.Lit(2, i8),), i8), {}, i8)
    logical = 0  # `not 2` in cpyte
    assert bitwise != logical and bitwise == -3, (bitwise, logical)


def test_no_prover_rewrite_hook_in_codegen():
    """Guard: prover output must not change emitted IR until semantics match."""
    source = (ROOT / "source" / "cpyte" / "bytecoding.py").read_text(encoding="utf-8")
    assert "try_opreof_rewrite_expr" not in source, (
        "an OProof rewrite was reintroduced into codegen; see test_opreof_proofs.py"
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except unittest.SkipTest as exc:
            print(f"SKIP {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - report every failing oracle
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    sys.exit(1 if failures else 0)
