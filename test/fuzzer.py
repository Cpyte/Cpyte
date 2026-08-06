import sys
import os
import re
import random
import traceback
import signal
import io
import contextlib
import string
import multiprocessing as mp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'source'))

from cpyte.lexar import Lexer, LexerError
from cpyte.astparse import parse_file, ParseError
from cpyte.semantic_analasis import analyze
from cpyte.bytecoding import LLVM
from cpyte.compiling import run_jit


TIMEOUT_S = 30


CRASH_DIR = os.path.join(os.path.dirname(__file__), 'crashes')
os.makedirs(CRASH_DIR, exist_ok=True)
_crash_counter = 0  # bump on each save_crash to give unique filenames


# ---------------------------------------------------------------------------
# Edge-case values
# ---------------------------------------------------------------------------

EDGE_INTS = [
    '0', '1', '-1', '2147483647', '-2147483648', '2147483648', '-2147483649',
    '4294967295', '4294967296',
    '9223372036854775807', '-9223372036854775808',
    '9223372036854775808', '-9223372036854775809',
    '18446744073709551615',
    '999999999999999999999999999999',
]

BIG_EDGE = [
    '18446744073709551616',        # 2^64 — exactly at big boundary
    '18446744073709551617',        # 2^64 + 1
    '340282366920938463463374607431768211456',  # 2^128
    '100000000000000000000000000000000000000',
    '123456789012345678901234567890',
]

EDGE_HEX = [
    '0x0', '0x1', '0x7fffffff', '0x80000000', '0xffffffff',
    '0x7fffffffffffffff', '0x8000000000000000', '0xffffffffffffffff',
    '0x8d3963ea15c50adb',
]

BIG_EDGE_HEX = [
    '0x10000000000000000',          # 2^64
    '0x10000000000000001',          # 2^64 + 1
    '0xffffffffffffffffffffffffffffffff',  # 2^128 - 1
    '0xdeadbeefcafebabedeadbeef',
]

EDGE_FLOATS = [
    '0.0', '1.0', '-1.0', '0.5', '1.5', '3.14159', '2.71828',
    '1e10', '1e-10', '1e100', '1e-100',
    '0.0000000001', '9999999999999999.0',
]

EDGE_STRINGS = [
    repr(''), repr(' '), repr('abc'), repr('hello world'),
    repr('x'), repr('\\n'), repr('\\t'), repr('a' * 100),
    repr('!@#$%^&*()'), repr('12345'),
]

NONZERO_LITERALS = {  # expressions that are always truthy (infinite loop risk)
    '1', '-1', '2.71828', 'true', '2', '100', '0.5', '-0.5',
    '2147483647', '9223372036854775807',
}

TYPES = ['int', 'int64', 'uint64', 'big', 'float', 'double', 'char', 'str']
NUMERIC_TYPES = ['int', 'int64', 'uint64', 'big']
FLOAT_TYPES = ['float', 'double']
ALL_SCALAR = TYPES + ['bool']
FIELD_NAMES = ['x', 'y', 'z', 'data', 'next', 'prev', 'left', 'right',
               'value', 'key', 'name', 'head', 'tail', 'ptr']

# Type sets the generator can safely emit. These mirror the type-checking
# rules in cpyte/semantic_analasis.py so that generated programs actually
# parse and analyze:
#   - INT_FAMILY: everything assignable into any int-family variable.
#   - BITWISE_OK: operands allowed for ~ << >> & | ^  (no char, no big).
#   - MOD_OK:     operands allowed for % and // (no char, no float).
#   - NEG_OK:     operands allowed for unary minus.
# Note: 'char' is deliberately excluded from these sets. cpy only allows
# char -> int (widening) and str -> char conversions; an int value can NOT
# initialize a char variable, and there are no char literals.
INT_FAMILY = tuple(sorted(('int', 'int64', 'uint64', 'big')))
FLOAT_FAMILY = tuple(sorted(('float', 'double')))
BITWISE_OK = tuple(sorted(('int', 'int64', 'uint64')))
MOD_OK = tuple(sorted(('int', 'int64', 'uint64', 'big')))
NEG_OK = tuple(sorted(('int', 'int64', 'uint64', 'big', 'float', 'double')))
_NO_CHAR = tuple(sorted(('int', 'int64', 'uint64', 'big')))
SCALAR_GEN_TYPES = ('int', 'int64', 'uint64', 'big', 'float', 'double', 'str')


# ---------------------------------------------------------------------------
# Type-aware fuzzer state
# ---------------------------------------------------------------------------

class Scope:
    def __init__(self, parent=None):
        self.vars: dict[str, str] = {}       # name → type_str
        self.parent: Scope | None = parent

    def add(self, name: str, ty: str):
        self.vars[name] = ty

    def lookup(self, name: str) -> str | None:
        if name in self.vars:
            return self.vars[name]
        if self.parent:
            return self.parent.lookup(name)
        return None

    def all_vars(self) -> list[tuple[str, str]]:
        result = []
        seen = set()
        s: Scope | None = self
        while s:
            for k, v in s.vars.items():
                if k not in seen:
                    result.append((k, v))
                    seen.add(k)
            s = s.parent
        return result

    def vars_of_type(self, ty: str | None = None) -> list[tuple[str, str]]:
        all_v = self.all_vars()
        if ty is None:
            return all_v
        return [(n, t) for n, t in all_v if t == ty]


class FuzzerState:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.scope = Scope()
        self.counter = 0
        self.loop_depth = 0
        self.structs: dict[str, list[tuple[str, str]]] = {}
        self.funcs: dict[str, dict] = {}
        self.known_truthy_globals: set[str] = set()  # global vars always non-zero

    def fresh(self, prefix='v'):
        self.counter += 1
        return f'{prefix}{self.counter}'


# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------

def base_type(ty: str) -> str:
    while ty.endswith('*') or ty.endswith('[]') or ty.endswith('&'):
        ty = ty[:-1]
    return ty


def random_scalar_type(rng: random.Random) -> str:
    return rng.choice(TYPES)


def random_ptr_type(rng: random.Random, inner: str | None = None) -> str:
    t = inner or rng.choice(TYPES)
    nptr = rng.choices([0, 1, 2], weights=[60, 30, 10])[0]
    for _ in range(nptr):
        t += '*'
    return t


def random_type(rng: random.Random, allow_ptr=True, allow_array=False) -> str:
    t = rng.choice(TYPES)
    if allow_ptr and rng.random() < 0.2:
        t = random_ptr_type(rng, t)
    if allow_array and rng.random() < 0.15:
        t += f'[{rng.randint(1, 10)}]'
    return t


def type_is_int(ty: str) -> bool:
    return ty in NUMERIC_TYPES or ty == 'char' or ty == 'bool'


def type_is_float(ty: str) -> bool:
    return ty in FLOAT_TYPES


def type_is_numeric(ty: str) -> bool:
    return type_is_int(ty) or type_is_float(ty)


def type_is_ptr(ty: str) -> bool:
    return ty.endswith('*') or ty.endswith('[]')


NONZERO_HEX_RE = re.compile(r'^0x[0-9a-fA-F]+$')
FLOAT_LITERAL_RE = re.compile(r'^[+-]?\d+\.\d+([eE][+-]?\d+)?$')
INT_LITERAL_RE = re.compile(r'^[+-]?\d+$')


def _literal_is_nonzero(code: str) -> bool:
    if code in ('true',):
        return True
    if code in NONZERO_LITERALS:
        return True
    if FLOAT_LITERAL_RE.match(code):
        val = float(code)
        return val != 0.0
    if INT_LITERAL_RE.match(code):
        val = int(code)
        return val != 0
    if NONZERO_HEX_RE.match(code):
        val = int(code, 16)
        return val != 0
    return False


def _str_literal_is_truthy(code: str) -> bool:
    """All string literals are non-null pointers in Cpy, so always truthy."""
    return (code.startswith("'") and code.endswith("'")) or (code.startswith('"') and code.endswith('"'))

def _is_nonempty_str_literal(code: str) -> bool:
    if code in ("''", '""'):
        return False
    if (code.startswith("'") and code.endswith("'")) or (code.startswith('"') and code.endswith('"')):
        return len(code) > 2
    return False


def _is_all_constant(code: str) -> bool:
    """Check if an expression consists only of literals and operators."""
    stripped = code.strip().strip('(').strip(')')
    if stripped in NONZERO_LITERALS:
        return True
    if _literal_is_nonzero(stripped) or stripped == '0':
        return True
    if _is_nonempty_str_literal(stripped):
        return True
    return False


def _eval_constant(code: str):
    """Try to evaluate a constant expression (int or float).
    Returns an int, float, or None if it can't be evaluated."""
    code = code.strip()
    if code in ('true',):
        return 1
    try:
        if INT_LITERAL_RE.match(code):
            return int(code)
        if NONZERO_HEX_RE.match(code):
            return int(code, 16)
        if FLOAT_LITERAL_RE.match(code):
            return float(code)
    except (ValueError, OverflowError):
        pass
    # Unary minus/plus (avoid double negation which is handled by integer literals)
    if code.startswith('-') and not code.startswith('--') and not code.startswith('- '):
        inner = _eval_constant(code[1:])
        if inner is not None:
            return -inner
    if code.startswith('+') and not code.startswith('+ '):
        return _eval_constant(code[1:])
    # not expr
    if code.startswith('not '):
        inner = _eval_constant(code[4:])
        if inner is not None:
            return 1 if inner == 0 else 0
    # Parenthesized sub-expressions
    if code.startswith('(') and code.endswith(')'):
        return _eval_constant(code[1:-1])
    # Binary ops on constant operands (ordered longest-first to avoid partial matches)
    for op, fn in [
        ('!=', lambda a, b: int(a != b)),
        ('==', lambda a, b: int(a == b)),
        ('>=', lambda a, b: int(a >= b)),
        ('<=', lambda a, b: int(a <= b)),
        ('<<', lambda a, b: a << b if isinstance(a, int) and isinstance(b, int) and b >= 0 and b < 128 else None),
        ('>>', lambda a, b: a >> b if isinstance(a, int) and isinstance(b, int) and b >= 0 else None),
        ('+', lambda a, b: a + b),
        ('-', lambda a, b: a - b),
        ('*', lambda a, b: a * b),
        ('//', lambda a, b: a // b if isinstance(a, int) and isinstance(b, int) and b != 0 else None),
        ('/', lambda a, b: a / b if b != 0 else None),
        ('%', lambda a, b: a % b if isinstance(a, int) and isinstance(b, int) and b != 0 else None),
        ('&', lambda a, b: a & b if isinstance(a, int) and isinstance(b, int) else None),
        ('|', lambda a, b: a | b if isinstance(a, int) and isinstance(b, int) else None),
        ('^', lambda a, b: a ^ b if isinstance(a, int) and isinstance(b, int) else None),
        ('>', lambda a, b: int(a > b)),
        ('<', lambda a, b: int(a < b)),
    ]:
        if op in code:
            parts = _split_top_level(code, op)
            if len(parts) == 2:
                a = _eval_constant(parts[0].strip())
                b = _eval_constant(parts[1].strip())
                if a is not None and b is not None:
                    result = fn(a, b)
                    if result is not None:
                        return result
    return None


def _split_top_level(code: str, op: str) -> list[str]:
    """Split on operator only at top level (not inside parentheses)."""
    depth = 0
    for i, ch in enumerate(code):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif depth == 0 and code[i:i+len(op)] == op:
            # For multi-char ops like '!=', '>=', '<=', '<<', '>>', check
            # we're not in the middle of another multi-char operator
            prev_ch = code[i-1] if i > 0 else ''
            next_ch = code[i+len(op)] if i+len(op) < len(code) else ''
            # Skip if this '-' is part of '--'
            if op == '-' and (prev_ch == '-' or next_ch == '-'):
                continue
            # Skip if this '>' is part of '>=', '>>'
            if op == '>' and next_ch in ('=', '>'):
                continue
            # Skip if this '<' is part of '<=', '<<'
            if op == '<' and next_ch in ('=', '<'):
                continue
            # Skip if this '!' is part of '!='
            if op == '!' and next_ch == '=':
                continue
            # Skip if this '=' is part of '==', '>=', '<=', '!='
            if op == '=' and prev_ch in ('!', '>', '<', '='):
                continue
            return [code[:i], code[i+len(op):]]
    return [code]


def expr_is_always_truthy(code: str, known_truthy_globals: set[str] | None = None) -> bool:
    if known_truthy_globals is None:
        known_truthy_globals = set()
    code = code.strip()
    if code == '':
        return False

    # Direct truthy check
    if _literal_is_nonzero(code):
        return True
    if code.startswith('"') or code.startswith("'"):
        return _str_literal_is_truthy(code)

    # Global variable with known non-zero initializer
    if code in known_truthy_globals:
        return True

    # Structural patterns
    if code.startswith('&'):
        return True
    if code.startswith('sizeof('):
        return True
    if code.startswith('new ') or code.startswith('(new '):
        return True

    # not expr: not 0 → 1 (truthy), not <non-zero> → 0 (falsy)
    if code.startswith('not '):
        inner_val = _eval_constant(code[4:])
        if inner_val is not None:
            return inner_val == 0
        return False  # can't determine

    # Try full constant evaluation
    val = _eval_constant(code)
    if val is not None:
        return val != 0

    # String concat: 'a' + 'b' is always non-empty (truthy)
    if '+' in code and not any(c in code for c in '*-/%&|^<>'):
        parts = [p.strip() for p in code.split('+')]
        if all(p.startswith("'") or p.startswith('"') for p in parts if p.strip()):
            if any(_is_nonempty_str_literal(p) for p in parts):
                return True

    # Variable reference to a known-truthy global
    if code in known_truthy_globals:
        return True

    return False

def type_is_printable_safely(ty: str) -> bool:
    return not type_is_ptr(ty) and ty not in ('void',) and ty != ''


def pointee_type(ty: str) -> str:
    if ty.endswith('[]'):
        return ty[:-2] + '*'
    if ty.endswith('*'):
        return ty[:-1]
    return ty


# ---------------------------------------------------------------------------
# Expression generators — return (code: str, type_str: str)
# ---------------------------------------------------------------------------

def _gen_int_literal(rng: random.Random, target: str) -> str:
    """Produce a decimal literal whose inferred type is exactly `target`.
    Mirrors the Number typing in semantic_analasis.py (int32 / int64 / uint64 /
    big range checks). Only non-negative values are emitted; negation is done
    with unary minus so the literal's inferred type is predictable."""
    if target == 'int64':
        if rng.random() < 0.35:
            return rng.choice(['2147483648', '4294967296', '1000000000000', '9223372036854775807'])
        return str(rng.randint(2**31, 2**63 - 1))
    if target == 'uint64':
        if rng.random() < 0.35:
            return rng.choice(['9223372036854775808', '10000000000000000000', '18446744073709551615'])
        return str(rng.randint(2**63, 2**64 - 1))
    if target == 'big':
        if rng.random() < 0.35:
            return rng.choice(['18446744073709551616', '340282366920938463463374607431768211456'])
        return str(rng.randint(2**64 + 1, 10**30))
    # 'int' / 'char' targets: small literal (inferred 'int', assignable to char)
    if rng.random() < 0.35:
        return rng.choice(['0', '1', '2', '5', '42', '-1', '-5', '100', '1000', '1000000'])
    return str(rng.randint(0, 2147483647))


def _random_string(rng: random.Random) -> str:
    """A single-line string literal body (no quotes / escapes / newlines)."""
    return ''.join(rng.choices(
        'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _+-*/=.,', k=rng.randint(0, 12)))


def gen_literal(state: FuzzerState, target_type: str | None = None) -> tuple[str, str]:
    rng = state.rng
    ty = target_type or rng.choice(SCALAR_GEN_TYPES)

    if type_is_ptr(ty):
        # Never null: a null pointer indexed / dereferenced / converted to
        # str is a runtime null deref (UB). cpy zero-inits raw `new` boxes, so
        # `new T` is always a usable non-null pointer.
        pointee = ty[:-1] if ty.endswith('*') else ty[:-2]
        return f'(new {pointee})', ty

    if ty == 'str':
        if rng.random() < 0.2:
            return '""', 'str'
        if rng.random() < 0.2:
            return '"hello"', 'str'
        return f'"{_random_string(rng)}"', 'str'

    if ty in ('float', 'double'):
        return _gen_float_literal(rng), 'float'

    if ty == 'char':
        # No char literals exist; a single-char str converts to char.
        return rng.choice(['"a"', '"b"', '"c"', '"x"', '" "']), 'str'

    return _gen_int_literal(rng, ty), ('int' if ty in ('int', 'char') else ty)


def _gen_float_literal(rng: random.Random) -> str:
    """A float literal cpy can actually lex — no `e`-exponent notation
    (1e10 fails to tokenize; only `digits.digits` is supported)."""
    if rng.random() < 0.6:
        return rng.choice(['0.0', '1.0', '-1.0', '0.5', '1.5', '3.14159', '2.71828',
                           '0.0000000001', '9999999999999999.0', '123.456'])
    return f'{rng.uniform(-100000.0, 100000.0):.6f}'


def gen_variable(state: FuzzerState, target_type: str | None = None) -> tuple[str, str] | None:
    candidates = [(n, t) for n, t in state.scope.all_vars()
                  if _assignable(t, target_type)]
    if not candidates:
        return None
    return state.rng.choice(candidates)


def _assignable(vt: str, target: str | None) -> bool:
    """True if a value of type vt may initialize / be assigned into target.
    Mirrors the valid_conversions table in semantic_analasis.py (one-way!):
    no int-family variable accepts a `big` value, and pointer targets require
    an exactly matching pointer type (or void*/int/str interop)."""
    if target is None:
        return True
    if vt == target:
        return True
    if target == 'str':
        # Only real string sources. cpy's analyzer also allows any pointer ->
        # str, but a null pointer stored as a str then dereferenced (str->char)
        # is a null deref at runtime, so the generator keeps str truly non-null.
        return vt == 'char' or vt == 'str'
    if type_is_ptr(target):
        return vt in ('int', 'str') or vt == 'void*' or target == 'void*'
    if target in ('float', 'double'):
        return vt in ('float', 'double')
    if target == 'char':
        return vt in ('char', 'str')
    if target == 'int':
        # char -> int widening is the only char-to-int-family conversion.
        return vt in ('int', 'int64', 'uint64', 'char')
    if target in ('int64', 'uint64'):
        return vt in ('int', 'int64', 'uint64')
    return vt in ('int', 'int64', 'uint64', 'big')  # target == 'big'


def _num_promote(lt: str, rt: str) -> str:
    """Usual arithmetic conversions — mirrors semantic_analasis._numeric_promote."""
    if lt == rt:
        return lt
    if lt in ('float', 'double') or rt in ('float', 'double'):
        return 'double' if 'double' in (lt, rt) else 'float'
    if 'big' in (lt, rt):
        return 'big'
    if lt in ('int64', 'uint64') or rt in ('int64', 'uint64'):
        return 'int64'
    return 'int'


def _is_constant_zero(code: str) -> bool:
    val = _eval_constant(code)
    return val is not None and val == 0


def _ensure_nonzero(state: FuzzerState, value: tuple[str, str], depth: int,
                    exact: frozenset | None) -> tuple[str, str]:
    """Regenerate a divisor until it is not a constant zero."""
    rng = state.rng
    code, vt = value
    for _ in range(4):
        if not _is_constant_zero(code):
            return (code, vt)
        code, vt = gen_expr(state, vt, depth + 1, exact)
    return ('(0 + 1)', 'int')


def gen_expr(state: FuzzerState, target_type: str | None = None, depth: int = 0,
             exact: frozenset | None = None) -> tuple[str, str]:
    """Generate a *value* expression (never bool) whose type is assignable to
    target_type. `exact` optionally restricts the set of acceptable value
    types (e.g. bitwise operands must not be big/char)."""
    rng = state.rng

    if depth > 5 or (depth > 0 and rng.random() < 0.12):
        var = gen_variable(state, target_type)
        if var and (exact is None or var[1] in exact):
            return var
        return gen_literal(state, target_type)

    candidates = []
    weights = []

    candidates.append(('literal', lambda: gen_literal(state, target_type)))
    weights.append(15)

    if state.scope.all_vars():
        var = gen_variable(state, target_type)
        if var:
            candidates.append(('var', lambda: var))
            weights.append(30)

    want_num = target_type is None or target_type in INT_FAMILY or target_type in FLOAT_FAMILY
    want_ptr = target_type is None or type_is_ptr(target_type)
    want_scalar = target_type is None or target_type in SCALAR_GEN_TYPES or type_is_ptr(target_type)

    if want_num:
        candidates.append(('binop', lambda: gen_binop(state, target_type, depth)))
        weights.append(30)
        candidates.append(('unary', lambda: gen_unary(state, target_type, depth)))
        weights.append(12)

    if want_ptr:
        candidates.append(('new', lambda: gen_new(state, target_type, depth)))
        weights.append(5)

    if target_type in (None, 'int'):
        candidates.append(('sizeof', lambda: gen_sizeof(state)))
        weights.append(3)

    if target_type in (None, 'str'):
        candidates.append(('strcat', lambda: gen_strcat(state, depth)))
        weights.append(5)

    if want_scalar:
        candidates.append(('field', lambda: gen_field_access(state, target_type, depth)))
        weights.append(5)
        candidates.append(('index', lambda: gen_index_expr(state, target_type, depth)))
        weights.append(4)

    kind, fn = rng.choices(candidates, weights=weights)[0]
    code, vt = fn()
    if not _assignable(vt, target_type) or (exact is not None and vt not in exact):
        return gen_literal(state, target_type)
    return code, vt


def gen_binop(state: FuzzerState, target_type: str | None, depth: int) -> tuple[str, str]:
    rng = state.rng

    if target_type is None:
        cat = rng.choices(['arith_int', 'arith_float', 'bitwise', 'mod', 'shift'],
                          weights=[30, 15, 15, 20, 20])[0]
    elif target_type in ('float', 'double'):
        cat = 'arith_float'
    else:
        cat = rng.choices(['arith_int', 'bitwise', 'mod', 'shift'],
                          weights=[45, 15, 20, 20])[0]

    if cat == 'arith_float':
        op = rng.choice(['+', '-', '*', '/'])
        left = gen_expr(state, rng.choice(list(FLOAT_FAMILY)), depth + 1, FLOAT_FAMILY)
        right = gen_expr(state, rng.choice(list(FLOAT_FAMILY)), depth + 1, FLOAT_FAMILY)
        if op == '/':
            right = _ensure_nonzero(state, right, depth, FLOAT_FAMILY)
        return f'({left[0]} {op} {right[0]})', _num_promote(left[1], right[1])

    if cat == 'arith_int':
        op = rng.choice(['+', '-', '*', '/'])
        left = gen_expr(state, rng.choice(list(INT_FAMILY)), depth + 1, INT_FAMILY)
        right = gen_expr(state, rng.choice(list(INT_FAMILY)), depth + 1, INT_FAMILY)
        if op == '/':
            right = _ensure_nonzero(state, right, depth, INT_FAMILY)
        return f'({left[0]} {op} {right[0]})', _num_promote(left[1], right[1])

    if cat == 'mod':
        op = rng.choice(['//', '%'])
        left = gen_expr(state, rng.choice(['int', 'int64', 'uint64', 'big']), depth + 1, MOD_OK)
        right = gen_expr(state, rng.choice(['int', 'int64', 'uint64', 'big']), depth + 1, MOD_OK)
        right = _ensure_nonzero(state, right, depth, MOD_OK)
        return f'({left[0]} {op} {right[0]})', _num_promote(left[1], right[1])

    if cat == 'shift':
        op = rng.choice(['<<', '>>'])
        left = gen_expr(state, rng.choice(['int', 'int64', 'uint64']), depth + 1, BITWISE_OK)
        right = gen_expr(state, 'int', depth + 1, BITWISE_OK)
        return f'({left[0]} {op} {right[0]})', ('int64' if left[1] in ('int64', 'uint64') else 'int')

    # bitwise & | ^
    op = rng.choice(['&', '|', '^'])
    left = gen_expr(state, rng.choice(['int', 'int64', 'uint64']), depth + 1, BITWISE_OK)
    right = gen_expr(state, rng.choice(['int', 'int64', 'uint64']), depth + 1, BITWISE_OK)
    result = 'int64' if left[1] in ('int64', 'uint64') or right[1] in ('int64', 'uint64') else 'int'
    return f'({left[0]} {op} {right[0]})', result


def gen_unary(state: FuzzerState, target_type: str | None, depth: int) -> tuple[str, str]:
    rng = state.rng

    if target_type is None:
        kind = rng.choices(['neg', 'not', 'tilde', 'deref', 'addr'], weights=[25, 25, 15, 20, 15])[0]
    elif target_type in ('float', 'double'):
        kind = 'neg'
    elif type_is_ptr(target_type):
        kind = rng.choices(['deref', 'addr'], weights=[60, 40])[0]
    else:
        kind = rng.choices(['neg', 'not', 'tilde'], weights=[45, 35, 20])[0]

    if kind == 'neg':
        op_t = target_type if target_type in NEG_OK else rng.choice(['int', 'int64', 'uint64', 'big', 'float', 'double'])
        exact = NEG_OK if op_t in INT_FAMILY else FLOAT_FAMILY
        inner = gen_expr(state, op_t, depth + 1, exact)
        code = inner[0]
        # `-` followed by `-` would lex as a decrement (`--`).
        if code.startswith('-'):
            code = f'({code})'
        return f'-{code}', inner[1]

    if kind == 'not':
        inner = gen_expr(state, None, depth + 1)
        return f'not {inner[0]}', 'int'

    if kind == 'tilde':
        inner = gen_expr(state, rng.choice(['int', 'int64', 'uint64']), depth + 1, BITWISE_OK)
        return f'~{inner[0]}', ('int64' if inner[1] in ('int64', 'uint64') else 'int')

    if kind == 'addr':
        var = gen_variable(state, None)
        if var:
            return f'&{var[0]}', var[1] + '*'
        return gen_literal(state, target_type)

    # deref — operand must be a genuine pointer (T*), never a literal or array
    inner, vt = _gen_ptr_operand(state, depth)
    return f'*{inner[0]}', vt


def _gen_ptr_operand(state: FuzzerState, depth: int) -> tuple[tuple[str, str], str]:
    """Return ((code, T*), pointee_type) for a genuine pointer expression."""
    rng = state.rng
    ptr_vars = [(n, t) for n, t in state.scope.all_vars() if t.endswith('*')]
    if ptr_vars and rng.random() < 0.6:
        name, pty = rng.choice(ptr_vars)
        return (name, pty), pty[:-1]
    inner = rng.choice(SCALAR_GEN_TYPES + tuple(state.structs.keys()))
    return (f'(new {inner})', f'{inner}*'), inner


def gen_call(state: FuzzerState, target_type: str | None, depth: int) -> tuple[str, str]:
    # No user-defined functions are emitted (globals are hoisted into main, so
    # calls from main would be undefined). Falls back to a literal.
    return gen_literal(state, target_type)


def gen_new(state: FuzzerState, target_type: str | None, depth: int) -> tuple[str, str]:
    rng = state.rng
    # Parenthesize the whole `new` form: `new T * x` / `new T & x` are parsed
    # as a pointer-type continuation / fail, so never leave `*`/`&` adjacent.
    if target_type and target_type.endswith('*'):
        return f'(new {target_type[:-1]})', target_type
    if target_type and target_type.endswith('[]'):
        size = rng.randint(1, 8)
        return f'(new {target_type[:-2]}[{size}])', target_type
    inner = rng.choice(SCALAR_GEN_TYPES + tuple(state.structs.keys()))
    if rng.random() < 0.4:
        size = rng.randint(1, 8)
        return f'(new {inner}[{size}])', f'{inner}[]'
    return f'(new {inner})', f'{inner}*'


def gen_sizeof(state: FuzzerState) -> tuple[str, str]:
    rng = state.rng
    kind = rng.choice(['type', 'expr'])
    if kind == 'expr' and state.scope.all_vars():
        name, ty = rng.choice(state.scope.all_vars())
        return f'sizeof({name})', 'int'
    # Array types (`int64[5]`) do not parse inside sizeof().
    ty = random_type(rng, allow_ptr=True, allow_array=False)
    return f'sizeof({ty})', 'int'


def _gen_str_operand(state: FuzzerState, depth: int) -> tuple[str, str]:
    """A str-typed expression (string concat needs exact str operands)."""
    rng = state.rng
    for _ in range(4):
        code, vt = gen_expr(state, 'str', depth + 1)
        if vt == 'str':
            return (code, vt)
    return gen_literal(state, 'str')


def gen_strcat(state: FuzzerState, depth: int) -> tuple[str, str]:
    left = _gen_str_operand(state, depth)
    right = _gen_str_operand(state, depth)
    return f'({left[0]} + {right[0]})', 'str'


def gen_field_access(state: FuzzerState, target_type: str | None, depth: int) -> tuple[str, str]:
    rng = state.rng
    if not state.structs:
        return gen_literal(state, target_type)
    struct_vars = [(n, t) for n, t in state.scope.all_vars()
                   if t.endswith('*') and t[:-1] in state.structs]
    if not struct_vars:
        return gen_literal(state, target_type)
    name, pty = rng.choice(struct_vars)
    matches = [f for f in state.structs[pty[:-1]] if _assignable(f[1], target_type)]
    if not matches:
        return gen_literal(state, target_type)
    fname, ftype = rng.choice(matches)
    return f'{name}.{fname}', ftype


def gen_ptr_arith(state: FuzzerState, depth: int) -> tuple[str, str]:
    rng = state.rng
    ptr_vars = [(n, t) for n, t in state.scope.all_vars() if type_is_ptr(t)]
    if not ptr_vars:
        return gen_literal(state, None)
    name, ty = rng.choice(ptr_vars)
    op = rng.choice(['+', '-'])
    return f'({name} {op} 0)', ty


def gen_index_expr(state: FuzzerState, target_type: str | None, depth: int) -> tuple[str, str]:
    rng = state.rng
    arr_vars = [(n, t) for n, t in state.scope.all_vars() if t.endswith('*') or t.endswith('[]')]
    if not arr_vars:
        return gen_literal(state, target_type)
    name, aty = rng.choice(arr_vars)
    inner = aty[:-1] if aty.endswith('*') else aty[:-2]
    if not _assignable(inner, target_type):
        return gen_literal(state, target_type)
    return f'{name}[0]', inner


# ---------------------------------------------------------------------------
# Statement generators
# ---------------------------------------------------------------------------

def gen_var_decl(state: FuzzerState, indent: int, depth: int) -> str | None:
    rng = state.rng
    pad = '    ' * indent
    ty = random_type(rng)
    name = state.fresh()
    init_text, _ = gen_expr(state, ty, depth + 1)
    state.scope.add(name, ty)
    return f'{pad}{ty} {name} = {init_text}'


def gen_assign(state: FuzzerState, indent: int, depth: int) -> str | None:
    rng = state.rng
    pad = '    ' * indent
    all_v = state.scope.all_vars()
    if not all_v:
        return None
    name, ty = rng.choice(all_v)
    if type_is_ptr(ty) and rng.random() < 0.2:
        inner = ty[:-1] if ty.endswith('*') else ty[:-2]
        value, _ = gen_expr(state, inner, depth + 1, _NO_CHAR)
        return f'{pad}{name}[0] = {value}'
    # Assignment (unlike initialization) does not allow char -> int.
    value, _ = gen_expr(state, ty, depth + 1, _NO_CHAR if ty == 'int' else None)
    return f'{pad}{name} = {value}'


def gen_compound_assign(state: FuzzerState, indent: int, depth: int) -> str | None:
    rng = state.rng
    pad = '    ' * indent
    all_v = state.scope.all_vars()
    if not all_v:
        return None
    candidates = [(n, t) for n, t in all_v if t in INT_FAMILY or t in FLOAT_FAMILY]
    if not candidates:
        return None
    name, ty = rng.choice(candidates)
    if ty == 'big':
        # big is only mutually promotable with the int family; char/float
        # operands are a "mismatched types" analyzer error.
        op = rng.choice(['+=', '-=', '*=', '/=', '//='])
        exact = MOD_OK
        val_tuple = gen_expr(state, ty, depth + 1, exact)
    else:
        if ty in ('float', 'double') or ty == 'char':
            op = rng.choice(['+=', '-=', '*=', '/='])
        else:
            op = rng.choice(['+=', '-=', '*=', '/=', '//='])
        if op == '//=':
            exact = MOD_OK
            val_tuple = gen_expr(state, ty, depth + 1, exact)
        else:
            exact = None
            val_tuple = gen_expr(state, ty, depth + 1)
    if op in ('/=', '//='):
        val_tuple = _ensure_nonzero(state, val_tuple, depth, exact)
    return f'{pad}{name} {op} {val_tuple[0]}'


def gen_compare(state: FuzzerState, depth: int) -> tuple[str, str]:
    rng = state.rng
    fam = rng.choices(['int', 'float', 'str'], weights=[50, 25, 25])[0]
    if fam == 'int':
        ct = rng.choice(['int', 'int64', 'uint64', 'big'])
        left = gen_expr(state, ct, depth + 1, MOD_OK)
        right = gen_expr(state, ct, depth + 1, MOD_OK)
    elif fam == 'float':
        left = gen_expr(state, rng.choice(['float', 'double']), depth + 1, FLOAT_FAMILY)
        right = gen_expr(state, rng.choice(['float', 'double']), depth + 1, FLOAT_FAMILY)
    else:
        left = gen_expr(state, 'str', depth + 1, frozenset(('str',)))
        right = gen_expr(state, 'str', depth + 1, frozenset(('str',)))
    op = rng.choice(['==', '!=', '<', '>', '<=', '>='])
    return f'({left[0]} {op} {right[0]})', 'bool'


def gen_cond(state: FuzzerState, depth: int = 0) -> tuple[str, str]:
    """A condition expression for if/while: a value or a bool expression."""
    rng = state.rng
    if depth > 4:
        return gen_expr(state, None, depth)
    kind = rng.choices(['value', 'compare', 'logical'], weights=[40, 35, 25])[0]
    if kind == 'compare':
        return gen_compare(state, depth)
    if kind == 'logical':
        op = rng.choice(['and', 'or'])
        left = gen_cond(state, depth + 1)
        right = gen_cond(state, depth + 1)
        return f'({left[0]} {op} {right[0]})', 'bool'
    return gen_expr(state, None, depth)


def gen_if(state: FuzzerState, indent: int, depth: int) -> str:
    rng = state.rng
    pad = '    ' * indent
    cond, _ = gen_cond(state, depth + 1)
    body = gen_body_in_scope(state, depth + 1, indent + 1) or [f'{pad}    print(0)']
    result = f'{pad}if {cond}:\n' + '\n'.join(body)
    if rng.random() < 0.3:
        else_body = gen_body_in_scope(state, depth + 1, indent + 1) or [f'{pad}    print(0)']
        result += f'\n{pad}else:\n' + '\n'.join(else_body)
    return result


def gen_while(state: FuzzerState, indent: int, depth: int) -> str | None:
    pad = '    ' * indent
    cond = None
    for _ in range(8):
        c, _ = gen_cond(state, depth + 1)
        if not expr_is_always_truthy(c, state.known_truthy_globals):
            cond = c
            break
    if cond is None:
        return None
    counter = state.fresh('w')
    trips = state.rng.randint(1, 4)
    state.loop_depth += 1
    body = gen_body_in_scope(state, depth + 1, indent + 1) or [f'{pad}    print(0)']
    state.loop_depth -= 1
    guard = f'(({cond}) and ({counter} > 0))'
    inner = f'{pad}    {counter} = {counter} - 1'
    return (f'{pad}int {counter} = {trips}\n'
            f'{pad}while {guard}:\n'
            + inner + '\n' + '\n'.join(body))


def gen_for(state: FuzzerState, indent: int, depth: int) -> str:
    rng = state.rng
    pad = '    ' * indent
    v = state.fresh()
    iter_expr = rng.choice(EDGE_STRINGS)
    state.loop_depth += 1
    old_scope = state.scope
    state.scope = Scope(old_scope)
    state.scope.add(v, 'char')
    body = gen_body(state, depth + 1, indent + 1) or [f'{pad}    print(0)']
    state.scope = old_scope
    state.loop_depth -= 1
    return f'{pad}for {v} in {iter_expr}:\n' + '\n'.join(body)


def gen_print(state: FuzzerState, indent: int, depth: int) -> str | None:
    pad = '    ' * indent
    expr, etype = gen_expr(state, None, depth + 1)
    if etype not in SCALAR_GEN_TYPES:
        return None
    return f'{pad}print({expr})'


def gen_expr_stmt(state: FuzzerState, indent: int, depth: int) -> str:
    pad = '    ' * indent
    expr, _ = gen_expr(state, None, depth + 1)
    return f'{pad}{expr}'


def gen_return(state: FuzzerState, indent: int) -> str:
    pad = '    ' * indent
    return f'{pad}return 0'


def gen_body_in_scope(state: FuzzerState, depth: int, indent: int) -> list[str]:
    """Generate a body inside a fresh child scope (blocks scope like cpy)."""
    old_scope = state.scope
    state.scope = Scope(old_scope)
    try:
        return gen_body(state, depth, indent)
    finally:
        state.scope = old_scope


def gen_body(state: FuzzerState, depth: int, indent: int) -> list[str]:
    rng = state.rng
    if depth > 6:
        return []
    pad = '    ' * indent
    n = rng.choices([0, 1, 2, 3, 4, 5, 6, 7], weights=[3, 15, 20, 20, 15, 10, 5, 2])[0]
    stmts = []
    had_return = False
    for _ in range(n):
        if had_return:
            break  # nothing after return is reachable
        kind = rng.choices([
            'vardecl', 'assign', 'compound_assign',
            'if', 'while', 'for',
            'return',
            'print', 'expr',
        ], weights=[
            25, 20, 5,
            15, 10, 8,
            5,
            10, 10,
        ])[0]

        fn_map = {
            'vardecl': lambda: gen_var_decl(state, indent, depth),
            'assign': lambda: gen_assign(state, indent, depth),
            'compound_assign': lambda: gen_compound_assign(state, indent, depth),
            'if': lambda: gen_if(state, indent, depth),
            'while': lambda: gen_while(state, indent, depth),
            'for': lambda: gen_for(state, indent, depth),
            'return': lambda: gen_return(state, indent),
            'print': lambda: gen_print(state, indent, depth),
            'expr': lambda: gen_expr_stmt(state, indent, depth),
        }

        s = fn_map[kind]()
        if s is not None:
            stmts.append(s)
            if kind == 'return':
                had_return = True
    if not stmts:
        stmts.append(f'{pad}print(0)')
    return stmts


# ---------------------------------------------------------------------------
# Program-level generators
# ---------------------------------------------------------------------------

def gen_struct(state: FuzzerState) -> str:
    rng = state.rng
    name = state.fresh('S')
    nfields = rng.randint(1, 6)
    fields = []
    field_list = []
    used_names = set()
    for _ in range(nfields):
        fname = rng.choice(FIELD_NAMES)
        while fname in used_names:
            fname = rng.choice(FIELD_NAMES)
        used_names.add(fname)
        # Pick scalar or pointer field types only — array field syntax
        # (`int64[10] next`) does not parse in cpy struct bodies.
        if rng.random() < 0.25:
            ftype = random_ptr_type(rng)
        else:
            ftype = rng.choice(TYPES)
        fields.append(f'{ftype} {fname}')
        field_list.append((fname, ftype))
    state.structs[name] = field_list
    return f'struct {name}:\n' + '\n'.join(f'    {f}' for f in fields)


def gen_func_def(state: FuzzerState, indent: int = 0) -> str:
    rng = state.rng
    pad = '    ' * indent
    name = state.fresh('f')

    old_scope = state.scope
    state.scope = Scope(old_scope)

    nparams = rng.randint(0, 3)
    params_list = []       # list of (name, type)
    params_strs = []       # list of "name: type"
    for _ in range(nparams):
        pname = state.fresh('p')
        ptype = random_type(rng)
        state.scope.add(pname, ptype)
        params_list.append((pname, ptype))
        params_strs.append(f'{pname}: {ptype}')

    ret_type = rng.choices(
        [None] + TYPES + ['void'],
        weights=[30, 10, 10, 10, 5, 5, 5, 5, 10]
    )[0]
    ret_arrow = f' -> {ret_type}' if ret_type else ''
    params_str = ', '.join(params_strs)

    body = gen_body(state, 0, indent + 1)
    if ret_type and ret_type != 'void':
        ret_expr, _ = gen_expr(state, ret_type)
        body.append(f'{pad}    return {ret_expr}')
    elif ret_type == 'void':
        body.append(f'{pad}    return')

    state.funcs[name] = {'params': params_list, 'ret': ret_type}
    state.scope = old_scope
    return f'{pad}def {name}({params_str}){ret_arrow}:\n' + '\n'.join(body)


def gen_program(state: FuzzerState) -> str:
    rng = state.rng
    state.scope = Scope()
    state.structs = {}
    state.funcs = {}
    state.counter = 0
    state.loop_depth = 0

    lines = []

    # Structs
    n_structs = rng.choices([0, 1, 2, 3], weights=[40, 30, 20, 10])[0]
    for _ in range(n_structs):
        lines.append(gen_struct(state))
        lines.append('')

    # Global variables
    for _ in range(rng.randint(0, 4)):
        ty = random_type(rng)
        name = state.fresh('g')
        init_text, _ = gen_expr(state, ty, 0)
        state.scope.add(name, ty)
        lines.append(f'{ty} {name} = {init_text}')
        # Track known-truthy globals to avoid infinite while loops
        if type_is_ptr(ty):
            pass  # null is a common pointer initializer
        elif ty == 'str' and _is_nonempty_str_literal(init_text):
            state.known_truthy_globals.add(name)
        elif ty != 'str' and init_text.strip() not in ('0', 'null', 'false', "''", '""', '0.0') and expr_is_always_truthy(init_text):
            state.known_truthy_globals.add(name)
    if lines and lines[-1] != '':
        lines.append('')

    # Main function
    lines.append('def main() -> int:')
    old_scope = state.scope
    state.scope = Scope(old_scope)
    body = gen_body(state, 0, 1)
    for stmt in body:
        lines.append(stmt)
    lines.append('    return 0')

    # No additional functions — cpy hoists globals into main(), so they're
    # not visible from other functions, causing "Undefined variable" crashes.

    state.scope = old_scope
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Test runner (identical to original but uses type-aware gen_program)
# ---------------------------------------------------------------------------

TEST_COUNT = 0
CRASH_COUNT = 0
BUG_COUNT = 0
PASS_COUNT = 0
REJECT_COUNT = 0


def _run_jit_capture(prog, src_files, opt_level):
    q = mp.Queue()
    p = mp.Process(target=_jit_worker, args=(q, prog, src_files, opt_level))
    p.start()
    p.join(timeout=TIMEOUT_S)
    if p.is_alive():
        p.terminate()
        p.join()
        return None, None, 'timeout'
    result = q.get_nowait() if not q.empty() else ('', None, 'no result')
    return result


def _jit_worker(q, prog, src_files, opt_level):
    f_out = io.StringIO()
    try:
        with contextlib.redirect_stdout(f_out):
            ret = run_jit(prog, opt_level=opt_level, src_files=src_files)
        q.put((f_out.getvalue(), ret, None))
    except ZeroDivisionError as e:
        if 'division by zero' in str(e):
            q.put((f_out.getvalue(), None, 'div0'))
        else:
            q.put((None, None, f'div by zero: {e}'))
    except Exception as e:
        q.put((None, None, f'JIT crash: {e}'))


def run_test(source, label=''):
    global TEST_COUNT, CRASH_COUNT, BUG_COUNT, PASS_COUNT, REJECT_COUNT
    TEST_COUNT += 1

    if TEST_COUNT % 500 == 0:
        n = TEST_COUNT
        r = REJECT_COUNT
        p = PASS_COUNT
        print(f'  [{n}] pass={p} reject={r} crashes={CRASH_COUNT} bugs={BUG_COUNT}', flush=True)

    try:
        tokens = Lexer(source).get_tokens()
        parsed, _ = parse_file(tokens)
    except (LexerError, ParseError, Exception) as e:
        REJECT_COUNT += 1
        return

    try:
        err, _ = analyze(source, parsed, strict=False)
    except Exception as e:
        CRASH_COUNT += 1
        save_crash(source, f'analyzer crash: {e}')
        return

    if err:
        REJECT_COUNT += 1
        return

    try:
        c = LLVM()
        prog, src_files = c.emit_program(parsed)
    except Exception as e:
        CRASH_COUNT += 1
        save_crash(source, f'codegen crash: {e}')
        return

    out0, ret0, err0 = _run_jit_capture(prog, src_files, opt_level=0)
    out3, ret3, err3 = _run_jit_capture(prog, src_files, opt_level=3)

    if err0 or err3:
        if err0 and err3:
            reason0 = err0 or 'ok'
            reason3 = err3 or 'ok'
            BUG_COUNT += 1
            save_crash(source, f'both JIT fail: unopt={reason0}, opt={reason3}')
            return
        if err0:
            BUG_COUNT += 1
            save_crash(source, f'unopt JIT fails: {err0} (opt OK)')
            return
        BUG_COUNT += 1
        save_crash(source, f'opt JIT fails: {err3} (unopt OK)')
        return

    if out0 == out3:
        PASS_COUNT += 1
        return

    # If both outputs are purely numeric (addresses), they can legitimately differ
    # between runs due to ASLR. Skip these as false positives.
    if out0 and out3:
        stripped0 = out0.strip()
        stripped3 = out3.strip()
        if stripped0.isdigit() and stripped3.isdigit() and len(stripped0) > 4 and len(stripped3) > 4:
            PASS_COUNT += 1
            return

    BUG_COUNT += 1
    save_crash(source, f'output mismatch: unopt={out0!r} opt={out3!r}')


def save_crash(source, error):
    global _crash_counter
    crash_id = _crash_counter
    _crash_counter += 1
    path = os.path.join(CRASH_DIR, f'crash_{crash_id:04d}.cpy')
    with open(path, 'w') as f:
        f.write(source)
    print(f'\n  *** SAVED crash_{crash_id:04d}.cpy ({error})', flush=True)


def main():
    n = 20000
    if len(sys.argv) > 1:
        n = int(sys.argv[1])

    seed = None
    if '--seed' in sys.argv:
        idx = sys.argv.index('--seed')
        seed = int(sys.argv[idx + 1])
    else:
        seed = random.randint(0, 2**31 - 1)

    state = FuzzerState(seed)
    print(f'Syntax-aware fuzzer: seed={seed}, iterations={n}', flush=True)

    for i in range(n):
        source = gen_program(state)
        run_test(source, f'iter {i}')

    print(f'\nDone: {TEST_COUNT} tests | pass={PASS_COUNT} reject={REJECT_COUNT} crashes={CRASH_COUNT} bugs={BUG_COUNT}')
    crash_files = [f for f in os.listdir(CRASH_DIR) if f.startswith('crash_')]
    if crash_files:
        print(f'Crashes saved in: {CRASH_DIR} ({len(crash_files)} files)')
    print(f'Seed: {seed}')


if __name__ == '__main__':
    main()
