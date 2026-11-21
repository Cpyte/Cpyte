"""OProof-backed helpers for cpyte optimizations (optional).

Enable with environment variable CPYTE_OPROOF=1 (or CBYTE_OPROOF=1).
OProof library is expected at OPROOF_PATH (default: /Users/main/PycharmProjects/WEW-OProof).
"""

from __future__ import annotations

import os
import sys
from typing import Optional


def _env_truthy(name: str) -> bool:
    v = os.environ.get(name)
    return bool(v and v not in ("0", "false", "False", "no", "NO", ""))


def is_enabled() -> bool:
    return _env_truthy("CPYTE_OPROOF") or _env_truthy("OPROOF_OPT")


_OPROOF_PATH = os.environ.get("OPROOF_PATH", "/Users/main/PycharmProjects/WEW-OProof")
_kernel = None


def _ensure_opreof():
    global _kernel
    if _kernel is not None:
        return _kernel
    if _OPROOF_PATH and _OPROOF_PATH not in sys.path:
        if os.path.isdir(_OPROOF_PATH):
            sys.path.insert(0, _OPROOF_PATH)
        else:
            raise ImportError(f"OProof not found at {_OPROOF_PATH}")
    import opreof  # type: ignore

    _kernel = opreof.Kernel()
    return _kernel


def _cpyte_base_type(ty_name: Optional[str]) -> Optional[str]:
    if not ty_name:
        return None
    base = str(ty_name).rstrip("*").rstrip("[]").strip()
    return base


def _to_opreof_type(opreof_mod, ty_base: str):
    m = {
        "bool": opreof_mod.BOOL,
        "char": opreof_mod.int8,
        "uchar": opreof_mod.uint8,
        "int8": opreof_mod.int8,
        "uint8": opreof_mod.uint8,
        "int16": opreof_mod.int16,
        "uint16": opreof_mod.uint16,
        "int": opreof_mod.int32,
        "int32": opreof_mod.int32,
        "uint32": opreof_mod.uint32,
        "int64": opreof_mod.int64,
        "uint64": opreof_mod.uint64,
        "size_t": opreof_mod.uint64,
        "uintptr_t": opreof_mod.uint64,
        "intptr_t": opreof_mod.int64,
        "ssize_t": opreof_mod.int64,
        "ptrdiff_t": opreof_mod.int64,
    }
    return m.get(ty_base)


def fold_int_consts_with_opreof(
    op_name: str,
    left: int,
    right: int,
    ty_name: Optional[str],
) -> Optional[int]:
    """Try to fold (left op_name right) using OProof. op_name in {PLUS,MINUS,STAR,SLASH_SLASH,PERCENT,AMPERSAND,PIPE,CARET,SHL,SHR,POW,...} (TokenType.name)."""
    if not is_enabled():
        return None
    try:
        k = _ensure_opreof()
        import opreof  # type: ignore
        from opreof.core import Lit as OLit, Op as OOp  # type: ignore
    except Exception:
        return None

    base = _cpyte_base_type(ty_name)
    if base is None:
        return None
    opty = _to_opreof_type(opreof, base)
    if opty is None:
        return None

    # map op
    if op_name == "PLUS":
        opname = "add"
    elif op_name == "MINUS":
        opname = "sub"
    elif op_name == "STAR":
        opname = "mul"
    elif op_name == "AMPERSAND":
        opname = "and"
    elif op_name == "PIPE":
        opname = "or"
    elif op_name == "CARET":
        opname = "xor"
    elif op_name == "SHL":
        opname = "shl"
    elif op_name == "SHR":
        opname = "shru" if (getattr(opty, "signed", True) is False) else "shrs"
    elif op_name == "SLASH_SLASH":
        opname = "divu" if (getattr(opty, "signed", True) is False) else "divs"
    elif op_name == "PERCENT":
        opname = "remu" if (getattr(opty, "signed", True) is False) else "rems"
    elif op_name == "POW":
        # OProof core has no pow op in the same way; skip
        return None
    else:
        return None

    try:
        if opname == "shl":
            expr = OOp(opname, (OLit(int(left), opty), OLit(int(right), opreof.int32)), opty)
        elif opname in ("shru", "shrs"):
            expr = OOp(opname, (OLit(int(left), opty), OLit(int(right), opreof.int32)), opty)
        else:
            expr = OOp(opname, (OLit(int(left), opty), OLit(int(right), opty)), opty)
    except Exception:
        return None

    try:
        norm = k.normalize(expr)
    except Exception:
        return None

    from opreof.core import Lit as _Lit  # type: ignore

    if isinstance(norm, _Lit):
        return int(norm.value)
    return None
