"""Experimental OProof bridge — REFERENCE ONLY, NOT WIRED INTO THE COMPILER.

The rewrite hook in bytecoding.py that called try_opreof_rewrite_expr() was
removed: it miscompiled cpyte's floor `//` (mapped to OProof's truncating
`divs`) and trusted an OProof builtin rewrite that folded `x % 1` to `x`.
CPYTE_OPROOF/OPROOF_OPT no longer affect codegen.  OProof equivalence checking
now lives in test/test_opreof_proofs.py as a test-only oracle.

The translation helpers below are kept for reference/experimentation.  They
still have known semantic gaps: signed floor-division vs `divs`, logical `not`
vs bitwise complement, shift definedness, 8/16-bit literal widths on rewrite,
and an unverified/under-axiomatised e-graph candidate.  Do not reconnect them
without an independent bit-exact check and differential tests.

Conservative rules that remain true:
- Only integer base types in the supported set (no float/big/str/dyn/pointers).
- No function calls, no side effects.
"""
from __future__ import annotations

import os
import sys
from typing import Optional, Any

try:
    from .astparse import BinOp, Number, UnaryOp, Variable
    from .lexar import TokenType
except Exception:
    from astparse import BinOp, Number, UnaryOp, Variable  # type: ignore
    from lexar import TokenType  # type: ignore


_enabled: Optional[bool] = None
_kernel = None
_opreof = None
_opreof_path_ready = False


def enable(flag: bool = True) -> None:
    global _enabled
    _enabled = flag


def is_enabled() -> bool:
    global _enabled
    if _enabled is not None:
        return _enabled
    v = os.environ.get("CPYTE_OPROOF") or os.environ.get("OPROOF_OPT")
    _enabled = bool(v and v not in ("0", "false", "False", "no", "NO", ""))
    return _enabled


def _ensure_opreof():
    global _opreof, _kernel, _opreof_path_ready
    if _kernel is not None:
        return _opreof, _kernel
    opath = os.environ.get("OPROOF_PATH", "/Users/main/PycharmProjects/WEW-OProof")
    if not _opreof_path_ready:
        if opath and opath not in sys.path and os.path.isdir(opath):
            sys.path.insert(0, opath)
        _opreof_path_ready = True
    import opreof as _op  # type: ignore

    _opreof = _op
    _kernel = _op.Kernel()
    return _opreof, _kernel


_INT_TYPE_MAP_BASE = (
    "bool",
    "char",
    "uchar",
    "int8",
    "uint8",
    "int16",
    "uint16",
    "int",
    "int32",
    "uint32",
    "int64",
    "uint64",
    "size_t",
    "uintptr_t",
    "intptr_t",
    "ssize_t",
    "ptrdiff_t",
)


def _base_type(ty_name: Optional[str]) -> Optional[str]:
    if not ty_name or not isinstance(ty_name, str):
        return None
    base = ty_name.rstrip("*").rstrip("[]").strip()
    return base if base in _INT_TYPE_MAP_BASE else None


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


def _infer_expr_type(node: Any) -> Optional[str]:
    t = getattr(node, "inferred_type", None)
    if t is None:
        t = getattr(node, "_inferred_type", None)
    return _base_type(t)


def _const_int(node: Any) -> Optional[int]:
    try:
        from .optimizations import _const_int_value  # type: ignore

        return _const_int_value(node)
    except Exception:
        try:
            from optimizations import _const_int_value  # type: ignore

            return _const_int_value(node)
        except Exception:
            return None


def _ast_node_count(node: Any, limit: int = 24) -> int:
    if node is None:
        return 0
    if isinstance(node, (Number, Variable)):
        return 1
    if isinstance(node, UnaryOp):
        return 1 + _ast_node_count(node.operand, limit)
    if isinstance(node, BinOp):
        c = 1 + _ast_node_count(node.left, limit) + _ast_node_count(node.right, limit)
        if c > limit:
            return limit + 1
        return c
    return limit + 1


def _ast_to_opreof(node: Any, opreof_mod, opty):
    """Translate a restricted cpyte int expr to OProof Expr, or None."""
    from opreof.core import Lit as OLit, Op as OOp, Var as OVar  # type: ignore

    if isinstance(node, Number):
        if getattr(node, "is_bool", False):
            try:
                v = int(node.value) != 0
            except Exception:
                v = str(node.value).lower() not in ("0", "false")
            return OLit(1 if v else 0, opreof_mod.BOOL)
        try:
            return OLit(int(node.value, 0), opty)
        except Exception:
            return None

    if isinstance(node, Variable):
        vt = _infer_expr_type(node) or _base_type(getattr(node, "inferred_type", None))
        if vt is None:
            # assume same as result context
            vt_base = _base_type(str(getattr(opty, "name", ""))) if hasattr(opty, "name") else None
            if vt_base is None:
                return None
            vty = _to_opreof_type(opreof_mod, vt_base)
        else:
            vty = _to_opreof_type(opreof_mod, vt)
        if vty is None:
            return None
        return OVar(node.name, vty)

    if isinstance(node, UnaryOp):
        inner = _ast_to_opreof(node.operand, opreof_mod, opty)
        if inner is None:
            return None
        if node.op == TokenType.MINUS:
            return OOp("neg", (inner,), opty)
        if node.op == TokenType.NOT:
            return OOp("not", (inner,), opty)
        return None

    if isinstance(node, BinOp):
        lt = _infer_expr_type(node.left)
        rt = _infer_expr_type(node.right)
        # prefer node inferred type as result
        nty = _infer_expr_type(node)
        res_base = nty or lt or rt
        lopt = _to_opreof_type(opreof_mod, res_base) if res_base else opty
        if lopt is None:
            lopt = opty
        left_e = _ast_to_opreof(node.left, opreof_mod, lopt)
        right_e = _ast_to_opreof(node.right, opreof_mod, lopt)
        if left_e is None or right_e is None:
            return None
        if node.op == TokenType.PLUS:
            return OOp("add", (left_e, right_e), lopt)
        if node.op == TokenType.MINUS:
            return OOp("sub", (left_e, right_e), lopt)
        if node.op == TokenType.STAR:
            return OOp("mul", (left_e, right_e), lopt)
        if node.op == TokenType.AMPERSAND:
            return OOp("and", (left_e, right_e), lopt)
        if node.op == TokenType.PIPE:
            return OOp("or", (left_e, right_e), lopt)
        if node.op == TokenType.CARET:
            return OOp("xor", (left_e, right_e), lopt)
        if node.op == TokenType.SHL:
            return OOp("shl", (left_e, right_e), lopt)
        if node.op == TokenType.SHR:
            signed = getattr(lopt, "signed", True)
            opname = "shru" if not signed else "shrs"
            return OOp(opname, (left_e, right_e), lopt)
        if node.op == TokenType.SLASH_SLASH:
            signed = getattr(lopt, "signed", True)
            opname = "divu" if not signed else "divs"
            return OOp(opname, (left_e, right_e), lopt)
        if node.op == TokenType.PERCENT:
            signed = getattr(lopt, "signed", True)
            opname = "remu" if not signed else "rems"
            return OOp(opname, (left_e, right_e), lopt)
        return None

    return None


def _op_name_to_token(opname: str):
    return {
        "add": TokenType.PLUS,
        "sub": TokenType.MINUS,
        "mul": TokenType.STAR,
        "and": TokenType.AMPERSAND,
        "or": TokenType.PIPE,
        "xor": TokenType.CARET,
        "shl": TokenType.SHL,
        "shru": TokenType.SHR,
        "shrs": TokenType.SHR,
        "divu": TokenType.SLASH_SLASH,
        "divs": TokenType.SLASH_SLASH,
        "remu": TokenType.PERCENT,
        "rems": TokenType.PERCENT,
    }.get(opname)


def _opreof_to_ast(expr: Any, inferred_ty: Optional[str], token=None):
    """Best-effort translate OProof Expr back to cpyte AST nodes (int subset). Returns None if not representable."""
    from opreof.core import Lit as OLit, Var as OVar, Op as OOp, Select  # type: ignore

    if isinstance(expr, OLit):
        # OProof Lit stores wrapped value; keep as decimal
        try:
            v = int(expr.value)
        except Exception:
            return None
        n = Number(str(v), token=token)
        n.inferred_type = inferred_ty
        return n

    if isinstance(expr, OVar):
        v = Variable(str(expr.name), token=token)
        v.inferred_type = inferred_ty
        return v

    if isinstance(expr, Select):
        # Not handled for integer expr rewriting in this pass
        return None

    if isinstance(expr, OOp):
        op = expr.op
        args = expr.args
        tok = _op_name_to_token(op)
        if tok is None:
            # unary?
            if op == "neg" and len(args) == 1:
                child = _opreof_to_ast(args[0], inferred_ty, token)
                if child is None:
                    return None
                u = UnaryOp(TokenType.MINUS, child, token=token)
                u.inferred_type = inferred_ty
                return u
            if op == "not" and len(args) == 1:
                child = _opreof_to_ast(args[0], inferred_ty, token)
                if child is None:
                    return None
                u = UnaryOp(TokenType.NOT, child, token=token)
                u.inferred_type = inferred_ty
                return u
            return None

        if len(args) != 2:
            return None
        # For shifts, right operand is often int32 in OProof; in cpyte the AST
        # for the shift amount can be any int expr; but our mapping produced
        # shift with same context. We translate both sides.
        a0 = _opreof_to_ast(args[0], inferred_ty, token)
        a1 = _opreof_to_ast(args[1], inferred_ty, token)
        if a0 is None or a1 is None:
            return None
        # For shl/shr/div/rem the result type is the operand type in practice
        # (same as inferred_ty). Also set child inferred types if they look like ints.
        a0.inferred_type = a0.inferred_type or inferred_ty
        a1.inferred_type = a1.inferred_type or inferred_ty
        bn = BinOp(a0, tok, a1, token=token)
        bn.inferred_type = inferred_ty
        return bn

    return None


def _node_cost(node: Any) -> int:
    if node is None:
        return 10**9
    if isinstance(node, (Number, Variable)):
        return 1
    if isinstance(node, UnaryOp):
        return 1 + _node_cost(node.operand)
    if isinstance(node, BinOp):
        # prefer shifting/mul->add? not directly; cost = nodes + small penalty
        return 1 + _node_cost(node.left) + _node_cost(node.right)
    return 10**9


def try_opreof_rewrite_expr(node: Any) -> Optional[Any]:
    """Experimental: normalize/rewrite a small int AST expr with OProof and
    return a translated AST if it is a literal OR appears better.

    Accepts:
    - any rewrite that folds to a Number
    - kernel.normalize results that are strictly smaller (node-cost) than input
    - e-graph normalized results that are strictly smaller (node-cost) than input
      (helps factoring like x*2+x*3 -> 5*x)
    """
    if not is_enabled():
        return None
    if not isinstance(node, BinOp):
        return None
    if _ast_node_count(node) > 18:
        return None
    ty = _infer_expr_type(node)
    if ty is None:
        return None
    try:
        opreof_mod, kernel = _ensure_opreof()
    except Exception:
        return None
    opty = _to_opreof_type(opreof_mod, ty)
    if opty is None:
        return None
    e = _ast_to_opreof(node, opreof_mod, opty)
    if e is None:
        return None

    candidates = []
    # 1) Kernel normalize
    try:
        norm = kernel.normalize(e)
        try:
            if norm.key() != e.key():
                candidates.append(norm)
        except Exception:
            candidates.append(norm)
    except Exception:
        norm = None

    # 2) E-graph normalize (better for algebraic factoring/canonicalization)
    try:
        from opreof import egraph_normalize  # type: ignore

        # Build var type map for symbols in e
        var_map = {}
        stack = [e]
        seen = set()
        while stack:
            cur = stack.pop()
            k = id(cur)
            if k in seen:
                continue
            seen.add(k)
            try:
                from opreof.core import Var as OVar  # type: ignore

                if isinstance(cur, OVar):
                    var_map[cur.name] = cur.type
            except Exception:
                pass
            if hasattr(cur, "args"):
                stack.extend(cur.args)
        if var_map:
            en = egraph_normalize(e, var_map)
            try:
                # accept even if key equal? usually not; but allow different repr
                candidates.append(en)
            except Exception:
                pass
    except Exception:
        pass

    best = None
    best_cost = _node_cost(node)
    for cand in candidates:
        try:
            if cand.key() == e.key():
                # still maybe translated differently; check
                pass
        except Exception:
            pass
        new_ast = _opreof_to_ast(cand, ty, token=getattr(node, "_token", None))
        if new_ast is None:
            continue
        if isinstance(new_ast, Number):
            return new_ast
        cst = _node_cost(new_ast)
        if cst < best_cost:
            best_cost = cst
            best = new_ast
    return best
