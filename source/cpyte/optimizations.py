"""Compiler optimizations for the cpyte backend.

Provides:
1. Fibonacci / linear-recurrence fast-doubling and matrix-exponentiation.
2. Constant-division strength reduction (power-of-two shifts, Barrett reduction).
3. Loop transformations: LICM, bounded unrolling, induction-variable canonicalisation.
4. Algebraic-identity folding (x*0, x^x, (x<<c1)<<c2, x/x, strength-reduced
   multiplication by small constants).
"""

from __future__ import annotations

from typing import Optional

from .astparse import (
    AddrOf,
    Assign,
    Attr,
    BinOp,
    BorrowExpr,
    Call,
    Deref,
    FuncDef,
    If,
    Index,
    InlineAsm,
    MoveExpr,
    NewExpr,
    Number,
    Return,
    VarDecl,
    Variable,
    While,
)
from .lexar import TokenType

# ---------------------------------------------------------------------------
# Constant folding / interpretation helpers
# ---------------------------------------------------------------------------


def _trunc_to_signed(value: int, width: int) -> int:
    """Wrap *value* into a signed integer of *width* bits (two's complement)."""
    mask = (1 << width) - 1
    value &= mask
    if value >= (1 << (width - 1)):
        value -= (1 << width)
    return value


def _fold_int_binop(op, left: int, right: int) -> Optional[int]:
    """Fold an integer BinOp at compile time, honouring cpyte's semantics:
    ``+ - * // % << >> & | ^ **``.  Division/remainder use C semantics
    (truncation toward zero); returns *None* for signed-div-by-zero and for
    unsupported operators so the caller can fall back to runtime code."""
    if op == TokenType.PLUS:
        return left + right
    if op == TokenType.MINUS:
        return left - right
    if op == TokenType.STAR:
        return left * right
    if op == TokenType.SLASH_SLASH:
        if right == 0:
            return None
        return left // right
    if op == TokenType.PERCENT:
        if right == 0:
            return None
        r = abs(left) % abs(right)
        if left < 0:
            r = -r
        return r
    if op == TokenType.SHL:
        if right < 0:
            return None
        return left << right
    if op == TokenType.SHR:
        if right < 0:
            return None
        return left >> right
    if op == TokenType.AMPERSAND:
        return left & right
    if op == TokenType.PIPE:
        return left | right
    if op == TokenType.CARET:
        return left ^ right
    if op == TokenType.POW:
        try:
            return left ** right
        except (OverflowError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Bit-manipulation helpers
# ---------------------------------------------------------------------------

def _is_power_of_2(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def _log2_floor(n: int) -> int:
    assert n > 0
    return n.bit_length() - 1


# ---------------------------------------------------------------------------
# 2. Constant-division strength reduction
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 1. Fibonacci / linear-recurrence detection + fast-doubling
# ---------------------------------------------------------------------------

def _detect_fibonacci_pattern(node: FuncDef):
    """Detect a standard recursive Fibonacci function.

    Recognises::

        def fib(n: int) -> int:
            if n <= 1: return n
            return fib(n-1) + fib(n-2)

    Returns ``(param_name, param_type, ret_type)`` on success, else *None*.
    """
    if not isinstance(node, FuncDef):
        return None
    if len(node.params) != 1:
        return None

    param_name = next(iter(node.params))
    param_type = node.params[param_name]
    ret_type = node.rettype or "int"
    body = list(node.body)
    if not body:
        return None

    has_base = False
    has_rec = False

    for stmt in body:
        if isinstance(stmt, If):
            cond = stmt.cond
            if isinstance(cond, BinOp) and cond.op in (
                TokenType.LESS_EQ, TokenType.LESS,
            ):
                # n <= 1 / n < 2  (or reversed)
                if (
                    isinstance(cond.left, Variable)
                    and cond.left.name == param_name
                    and isinstance(cond.right, Number)
                    and cond.right.value in ("1", "2")
                ) or (
                    isinstance(cond.right, Variable)
                    and cond.right.name == param_name
                    and isinstance(cond.left, Number)
                    and cond.left.value in ("1", "2")
                ):
                    # Base-case return: `return n` / `return <var>` / `return 0` / `return 1`
                    def _is_base_return(s):
                        return isinstance(s, Return) and isinstance(
                            s.value, (Variable, Number)
                        )

                    for s in (stmt.body or []):
                        if _is_base_return(s):
                            has_base = True
                    if stmt.orelse:
                        # If the orelse carries the recursive call, mark it.
                        if any(
                            isinstance(s, Return)
                            and _is_self_recursive_pair(
                                s.value.left, s.value.right, node.name, param_name
                            )
                            if isinstance(s, Return) and isinstance(s.value, BinOp)
                            and s.value.op == TokenType.PLUS
                            else False
                            for s in stmt.orelse
                        ):
                            has_rec = True
                        for s in stmt.orelse:
                            if _is_base_return(s):
                                has_base = True
        elif isinstance(stmt, Return):
            if (
                isinstance(stmt.value, BinOp)
                and stmt.value.op == TokenType.PLUS
                and _is_self_recursive_pair(stmt.value.left, stmt.value.right, node.name, param_name)
            ):
                has_rec = True

    if has_base and has_rec:
        return (param_name, param_type, ret_type)
    return None


def _is_self_recursive_pair(left, right, func_name: str, param_name: str) -> bool:
    """Return True when *left* and *right* are ``func(param ± const)`` calls."""
    if not (isinstance(left, Call) and isinstance(right, Call)):
        return False
    if not (isinstance(left.callee, Variable) and left.callee.name == func_name):
        return False
    if not (isinstance(right.callee, Variable) and right.callee.name == func_name):
        return False
    if len(left.args) != 1 or len(right.args) != 1:
        return False
    for arg in (left.args[0], right.args[0]):
        if not isinstance(arg, BinOp):
            return False
        if arg.op != TokenType.MINUS:
            return False
        if not (isinstance(arg.left, Variable) and arg.left.name == param_name):
            return False
        if not isinstance(arg.right, Number):
            return False
    return True


def _iterative_fib_indices(n_bits: int) -> list[bool]:
    """Return the bit-pattern of *n* (MSB-first, excluding the leading 1) for
    iterative fast-doubling.  The leading 1 is always processed implicitly
    (it seeds ``a=0, b=1`` then applies the doubling step once)."""
    if n_bits <= 0:
        return []
    bin_str = bin(n_bits)[3:]  # strip '0b1'
    return [c == '1' for c in bin_str]


def _fib_precomputed_table(max_n: int = 70) -> list[int]:
    """Return ``table[i] == F(i)`` for ``0 <= i <= max_n``."""
    table = [0] * (max_n + 1)
    if max_n >= 1:
        table[1] = 1
    for i in range(2, max_n + 1):
        table[i] = table[i - 1] + table[i - 2]
    return table


# Pre-computed table for small N closed-form lookups (up to F(70) in 64-bit).
_FIB_SMALL_TABLE = _fib_precomputed_table(70)


# ---------------------------------------------------------------------------
# 3. Loop analysis helpers
# ---------------------------------------------------------------------------

def _loop_written_names(node) -> frozenset[str]:
    """Names of scalar variables the loop body may assign, collected by a
    generic walk over every statement and nested statement body (If/While/
    Switch/Try, dict-based ``for`` bodies).  Refer to ``Assign.target`` /
    ``VarDecl.target`` Variable names.

    The walk deliberately over-approximates (it descends into every AST value,
    including expressions): invalidating a variable that is only *read* inside
    a loop just loses a constant fold, never correctness — the *soundness*
    requirement is that every name whose stored value can change must be
    dropped from the straight-line constant table across the boundary."""
    written: set[str] = set()
    stack: list = [list(node.body)]
    while stack:
        obj = stack.pop()
        if obj is None:
            continue
        t = type(obj)
        if t is Assign:
            if isinstance(obj.target, Variable):
                written.add(obj.target.name)
            stack.append(obj.target)
            stack.append(obj.value)
        elif isinstance(obj, VarDecl):
            written.add(obj.name)
            stack.append(obj.init)
            stack.append(obj.var_type)
        elif isinstance(obj, (list, tuple)):
            stack.extend(obj)
        elif isinstance(obj, dict):
            stack.extend(obj.values())
        elif isinstance(obj, If):
            stack.extend((obj.cond, obj.body, obj.orelse))
        elif isinstance(obj, While):
            stack.extend((obj.cond, obj.body))
        elif hasattr(obj, "_token"):
            slots = getattr(obj, "__slots__", None)
            if slots:
                if isinstance(slots, str):
                    slots = (slots,)
                stack.extend(getattr(obj, s, None) for s in slots)
            else:
                try:
                    stack.extend(vars(obj).values())
                except TypeError:
                    pass
    return frozenset(written)


def _loop_has_alias_risk(body) -> bool:
    """True when a loop body may write or observe scalar state through a
    pointer/field/array path that the name-based unrolling/counter bookkeeping
    cannot see (``*p = ...``, ``a[i] = ...``, ``obj.field = ...`` writes, or
    address-taking).  Such loops must not be unrolled with canonical counter
    stores, because those stores can clobber aliased writes."""
    stack: list = [list(body)]
    while stack:
        obj = stack.pop()
        if obj is None:
            continue
        if isinstance(obj, (Deref, AddrOf, Attr, Index, NewExpr, BorrowExpr, MoveExpr)):
            return True
        if isinstance(obj, Assign):
            stack.append(obj.target)
            stack.append(obj.value)
        elif isinstance(obj, (list, tuple)):
            stack.extend(obj)
        elif isinstance(obj, dict):
            stack.extend(obj.values())
        elif isinstance(obj, If):
            stack.extend((obj.cond, obj.body, obj.orelse))
        elif isinstance(obj, While):
            stack.extend((obj.cond, obj.body))
        elif hasattr(obj, "_token"):
            slots = getattr(obj, "__slots__", None)
            if slots:
                if isinstance(slots, str):
                    slots = (slots,)
                stack.extend(getattr(obj, s, None) for s in slots)
            else:
                try:
                    stack.extend(vars(obj).values())
                except TypeError:
                    pass
    return False


def _is_simple_counted_loop(node) -> tuple[str, int, int, bool] | None:
    """Detect a simple ``while`` counted loop and return
    ``(counter_name, stop, step, inclusive)`` or *None*.

    Recognises patterns like::

        i = <start>          # set just before the loop
        while i < <stop>:
            ...
            i = i + <step>

    with a strict or non-strict comparison (``<``, ``<=``, ``>``, ``>=``) and
    *exactly one* assignment to the counter inside the body (the increment).
    ``inclusive`` is True for ``<=``/``>=`` (the boundary value is visited).
    The initial value is not part of the pattern — the caller supplies it
    from the statement that precedes the loop.
    """
    if not isinstance(node, While):
        return None
    cond = node.cond
    if not isinstance(cond, BinOp):
        return None
    if cond.op not in (TokenType.LESS, TokenType.LESS_EQ, TokenType.GREATER, TokenType.GREATER_EQ):
        return None
    if not isinstance(cond.left, Variable):
        return None
    iv_name = cond.left.name
    if not isinstance(cond.right, Number):
        return None
    stop = _const_int_value(cond.right)
    if stop is None:
        return None

    body = node.body
    iv_assigns = 0
    step_val = None
    for stmt in body:
        if (
            isinstance(stmt, Assign)
            and isinstance(stmt.target, Variable)
            and stmt.target.name == iv_name
        ):
            iv_assigns += 1
            b = stmt.value
            if isinstance(b, BinOp) and isinstance(b.left, Variable) and b.left.name == iv_name:
                n = _const_int_value(b.right)
                if n is None:
                    return None
                if b.op == TokenType.PLUS:
                    step_val = n
                elif b.op == TokenType.MINUS:
                    step_val = -n
                else:
                    return None
            else:
                return None
    if iv_assigns != 1 or step_val is None:
        return None
    # Direction must match the comparison.
    if cond.op in (TokenType.LESS, TokenType.LESS_EQ) and step_val <= 0:
        return None
    if cond.op in (TokenType.GREATER, TokenType.GREATER_EQ) and step_val >= 0:
        return None
    inclusive = cond.op in (TokenType.LESS_EQ, TokenType.GREATER_EQ)
    return (iv_name, stop, step_val, inclusive)


def _count_loop_iterations(start: int, stop: int, step: int, inclusive: bool = False) -> int:
    """Return the number of iterations of a counted loop, or -1 if unknown.

    With *inclusive* set (``<=``/``>=`` conditions) the boundary value is
    visited, so the count is one higher than the strict form when the
    boundary lies exactly on the iteration lattice."""
    if step == 0:
        return -1
    if step > 0:
        if stop < start or (not inclusive and stop == start):
            return 0
        return (stop - start + step) // step if inclusive else (stop - start + step - 1) // step
    else:
        if stop > start or (not inclusive and stop == start):
            return 0
        return (start - stop + (-step)) // (-step) if inclusive else (start - stop + (-step) - 1) // (-step)


# ---------------------------------------------------------------------------
# 4. Algebraic-identity helpers
# ---------------------------------------------------------------------------

def _collect_assigned_names(stmts) -> set[str]:
    """Return the set of variable names assigned anywhere in *stmts*."""
    names: set[str] = set()
    for stmt in stmts:
        if isinstance(stmt, Assign):
            target = stmt.target
            if isinstance(target, Variable):
                names.add(target.name)
            elif isinstance(target, str):
                names.add(target)
        elif isinstance(stmt, VarDecl):
            names.add(stmt.name)
    return names


def _get_used_variables(entity) -> set[str]:
    """Collect the names of variables *read* anywhere inside an AST node.

    Walks the generic ``__slots__``/``vars()`` structure of the node.  An
    ``Assign``'s plain-variable target is treated as a *write*, not a read;
    everything else, including the right-hand side, is walked.  Used by the
    loop-invariant analysis for its read set."""
    names: set[str] = set()
    stack: list = [entity]
    while stack:
        obj = stack.pop()
        if obj is None:
            continue
        if isinstance(obj, Variable):
            names.add(obj.name)
        elif isinstance(obj, Assign):
            stack.append(obj.value)
        elif isinstance(obj, (list, tuple)):
            stack.extend(obj)
        elif isinstance(obj, dict):
            stack.extend(obj.values())
        elif isinstance(obj, If):
            stack.extend((obj.cond, obj.body, obj.orelse))
        elif isinstance(obj, While):
            stack.extend((obj.cond, obj.body))
        elif hasattr(obj, "_token"):
            slots = getattr(obj, "__slots__", None)
            if slots:
                if isinstance(slots, str):
                    slots = (slots,)
                stack.extend(getattr(obj, s, None) for s in slots)
            else:
                try:
                    stack.extend(vars(obj).values())
                except TypeError:
                    pass
    return names


def _find_loop_invariants(loop: While) -> list[Assign]:
    """Identify assign statements inside *loop* that can be hoisted before
    the loop and *removed* from its body (store-once LICM).

    ``Variable = <expr>`` is hoistable when:
    * ``<expr>`` reads no variable the loop writes anywhere (deep, including
      nested branches; ``y = y + 1`` is excluded by its self-read), and
    * the target is never *read* in the loop body (a read preceding the
      hoisted store in the original ordering would observe a different
      value), and
    * the target does not appear in the loop condition (moving the store out
      could change how many times the loop runs), and
    * the RHS performs no function call and no memory dereference (calls may
      have side effects, and pointer/field reads might alias writes the loop
      makes through a different name that our name-based analysis cannot
      see).

    Writes to the target *elsewhere* in the loop are fine: the hoisted store
    simply establishes a value that those later stores overwrite, which
    preserves every read and the post-loop value."""
    mutated = _loop_written_names(loop)
    cond_names = _get_used_variables(loop.cond)
    all_read: set[str] = set()
    for stmt in loop.body:
        all_read |= _get_used_variables(stmt)
    invariants: list[Assign] = []
    for stmt in loop.body:
        if not isinstance(stmt, Assign):
            continue
        target = stmt.target
        if not isinstance(target, Variable):
            continue
        rhs_vars = _get_used_variables(stmt.value)
        if not rhs_vars.isdisjoint(mutated):
            continue
        if target.name in all_read:
            continue
        if target.name in cond_names:
            continue
        if _expr_may_violate_invariance(stmt.value):
            continue
        invariants.append(stmt)
    return invariants


def _expr_may_violate_invariance(node) -> bool:
    """True if *node* mentions a call, dereference, field access, or array
    index anywhere -- anything that could observe or perform effects that
    our name-based Hoist analysis cannot reason about (side-effectful calls
    must NOT be moved out of a loop, and pointer/field reads might alias a
    write the loop performs through a different name)."""
    stack = [node]
    while stack:
        obj = stack.pop()
        if obj is None:
            continue
        if isinstance(obj, (Call, Deref, Attr, Index, NewExpr, InlineAsm)):
            return True
        if isinstance(obj, (list, tuple)):
            stack.extend(obj)
        elif isinstance(obj, dict):
            stack.extend(obj.values())
        elif isinstance(obj, If):
            stack.extend((obj.cond, obj.body, obj.orelse))
        elif isinstance(obj, While):
            stack.extend((obj.cond, obj.body))
        elif hasattr(obj, "_token"):
            slots = getattr(obj, "__slots__", None)
            if slots:
                if isinstance(slots, str):
                    slots = (slots,)
                stack.extend(getattr(obj, s, None) for s in slots)
            else:
                try:
                    stack.extend(vars(obj).values())
                except TypeError:
                    pass
    return False


def _is_const_zero(node) -> bool:
    if isinstance(node, Number):
        try:
            return int(node.value) == 0
        except (ValueError, TypeError):
            return False
    return False


def _is_const_one(node) -> bool:
    if isinstance(node, Number):
        try:
            return int(node.value) == 1
        except (ValueError, TypeError):
            return False
    return False


def _const_int_value(node) -> Optional[int]:
    if isinstance(node, Number):
        try:
            return int(node.value, 0)
        except (ValueError, TypeError):
            return None
    return None


def _const_fp_value(node) -> Optional[float]:
    """Return the floating-point value of a numeric literal *node* that is
    NOT integer-parseable (``2.0``, ``1e3``), else *None*.  Integral literals
    (``2``) are left to ``_const_int_value`` -- a double-typed context is
    decided by the caller from ``inferred_type``."""
    if not isinstance(node, Number):
        return None
    try:
        int(node.value, 0)
        return None
    except (ValueError, TypeError):
        pass
    try:
        return float(node.value)
    except (ValueError, TypeError):
        return None


def _detect_shift_amount_from_mul(node) -> Optional[int]:
    """If *node* is ``x * 2**k`` or ``2**k * x`` return *k*, else *None*."""
    if not isinstance(node, BinOp) or node.op != TokenType.STAR:
        return None
    for a, b in ((node.left, node.right), (node.right, node.left)):
        if isinstance(a, Number):
            v = _const_int_value(a)
            if v is not None and v > 0 and _is_power_of_2(v):
                return _log2_floor(v)
    return None
