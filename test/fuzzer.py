import sys
import os
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

TYPES = ['int', 'int64', 'uint64', 'big', 'float', 'double', 'char', 'str']
NUMERIC_TYPES = ['int', 'int64', 'uint64', 'big']
FLOAT_TYPES = ['float', 'double']
ALL_SCALAR = TYPES + ['bool']
FIELD_NAMES = ['x', 'y', 'z', 'data', 'next', 'prev', 'left', 'right',
               'value', 'key', 'name', 'head', 'tail', 'ptr']


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


def pointee_type(ty: str) -> str:
    if ty.endswith('[]'):
        return ty[:-2] + '*'
    if ty.endswith('*'):
        return ty[:-1]
    return ty


# ---------------------------------------------------------------------------
# Expression generators — return (code: str, type_str: str)
# ---------------------------------------------------------------------------

def gen_literal(state: FuzzerState, target_type: str | None = None) -> tuple[str, str]:
    rng = state.rng
    ty = target_type or rng.choice(TYPES)

    if ty == 'int':
        kind = rng.choices(['edge', 'hex', 'big'], weights=[40, 20, 40])[0]
        if kind == 'edge':  return rng.choice(EDGE_INTS), 'int'
        if kind == 'hex':   return rng.choice(EDGE_HEX), 'int'
        return str(rng.randint(-10**9, 10**9)), 'int'

    if ty == 'big':
        kind = rng.choices(['dec', 'hex'], weights=[60, 40])[0]
        if kind == 'dec':
            return rng.choice(BIG_EDGE), 'big'
        return rng.choice(BIG_EDGE_HEX), 'big'

    if ty == 'int64':
        return rng.choice(EDGE_INTS), 'int64'

    if ty == 'uint64':
        return rng.choice(EDGE_INTS), 'uint64'

    if ty in ('float', 'double'):
        return rng.choice(EDGE_FLOATS), ty

    if ty == 'char':
        return repr(rng.choice(string.printable.strip())), 'char'

    if ty == 'str':
        if rng.random() < 0.2:   return repr(''), 'str'
        if rng.random() < 0.2:   return repr(' ' * rng.randint(1, 10)), 'str'
        return repr(''.join(rng.choices('abcdefgh \t\n', k=rng.randint(0, 15)))), 'str'

    if ty == 'bool':
        return rng.choice(['true', 'false']), 'bool'

    if type_is_ptr(ty):
        return 'null', ty

    return '0', 'int'


def gen_variable(state: FuzzerState, target_type: str | None = None) -> tuple[str, str] | None:
    candidates = state.scope.vars_of_type(target_type)
    if not candidates:
        return None
    name, ty = state.rng.choice(candidates)
    return name, ty


def gen_expr(state: FuzzerState, target_type: str | None = None, depth: int = 0) -> tuple[str, str]:
    rng = state.rng

    if depth > 5 or (depth > 0 and rng.random() < 0.15):
        # terminal: literal or variable
        if rng.random() < 0.5:
            var = gen_variable(state, target_type)
            if var:
                return var
        return gen_literal(state, target_type)

    candidates = []
    weights = []

    # Literal (always possible)
    candidates.append(('literal', lambda: gen_literal(state, target_type)))
    weights.append(15)

    # Variable (if any variable of target_type exists, or any variable)
    if state.scope.all_vars():
        if target_type and state.scope.vars_of_type(target_type):
            candidates.append(('var', lambda: gen_variable(state, target_type)))
            weights.append(30)
        elif target_type is None:
            candidates.append(('var', lambda: gen_variable(state, None)))
            weights.append(20)

    # Binary op
    candidates.append(('binop', lambda: gen_binop(state, target_type, depth)))
    weights.append(30)

    # Unary op
    candidates.append(('unary', lambda: gen_unary(state, target_type, depth)))
    weights.append(12)

    # Call (builtin cast or registered function)
    candidates.append(('call', lambda: gen_call(state, target_type, depth)))
    weights.append(15)

    # new expr — produces pointer types
    if target_type is None or type_is_ptr(target_type):
        candidates.append(('new', lambda: gen_new(state, target_type, depth)))
        weights.append(5)

    # sizeof — produces int
    if target_type is None or target_type == 'int':
        candidates.append(('sizeof', lambda: gen_sizeof(state)))
        weights.append(3)

    # Cast — only if target_type differs from inner expression's type
    # (cast functions like int(), float() are NOT registered in the runtime)

    kind, fn = rng.choices(candidates, weights=weights)[0]
    return fn()


def gen_binop(state: FuzzerState, target_type: str | None, depth: int) -> tuple[str, str]:
    rng = state.rng

    # Choose operation category first
    cat = rng.choices(
        ['arith_int', 'arith_float', 'bitwise', 'compare', 'logical', 'shift'],
        weights=[30, 20, 15, 15, 10, 10]
    )[0]

    if cat == 'arith_int':
        op = rng.choice(['+', '-', '*', '/', '//', '%'])
        ty = target_type if target_type in NUMERIC_TYPES + ['char'] else rng.choice(NUMERIC_TYPES + ['char'])
        left = gen_expr(state, ty, depth + 1)
        right = gen_expr(state, ty, depth + 1)
        return f'({left[0]} {op} {right[0]})', ty

    if cat == 'arith_float':
        op = rng.choice(['+', '-', '*', '/'])
        ty = target_type if target_type in FLOAT_TYPES else rng.choice(FLOAT_TYPES)
        left = gen_expr(state, ty, depth + 1)
        right = gen_expr(state, ty, depth + 1)
        return f'({left[0]} {op} {right[0]})', ty

    if cat == 'bitwise':
        op = rng.choice(['&', '|', '^'])
        ty = target_type if target_type in NUMERIC_TYPES else rng.choice(NUMERIC_TYPES)
        left = gen_expr(state, ty, depth + 1)
        right = gen_expr(state, ty, depth + 1)
        return f'({left[0]} {op} {right[0]})', ty

    if cat == 'shift':
        op = rng.choice(['<<', '>>'])
        ty = target_type if target_type in NUMERIC_TYPES else rng.choice(NUMERIC_TYPES)
        left = gen_expr(state, ty, depth + 1)
        right = gen_expr(state, 'int', depth + 1)
        return f'({left[0]} {op} {right[0]})', ty

    if cat == 'compare':
        op = rng.choice(['==', '!=', '<', '>', '<=', '>='])
        # Compare needs matching types; result is int
        ty = target_type if target_type in TYPES else rng.choice(TYPES)
        left = gen_expr(state, ty, depth + 1)
        right = gen_expr(state, ty, depth + 1)
        return f'({left[0]} {op} {right[0]})', 'int'

    if cat == 'logical':
        op = rng.choice(['and', 'or'])
        # Logical ops work on truthy values, result is int
        left = gen_expr(state, None, depth + 1)
        right = gen_expr(state, None, depth + 1)
        return f'({left[0]} {op} {right[0]})', 'int'

    return gen_literal(state, target_type)


def gen_unary(state: FuzzerState, target_type: str | None, depth: int) -> tuple[str, str]:
    rng = state.rng
    kind = rng.choices(['neg', 'not', 'tilde', 'plus', 'deref', 'addr', 'pow'],
                       weights=[25, 25, 10, 5, 10, 10, 5])[0]

    if kind == 'neg':
        ty = target_type if target_type and type_is_numeric(target_type) else rng.choice(NUMERIC_TYPES + FLOAT_TYPES + ['char'])
        inner = gen_expr(state, ty, depth + 1)
        return f'-{inner[0]}', inner[1]

    if kind == 'not':
        inner = gen_expr(state, None, depth + 1)
        return f'not {inner[0]}', 'int'

    if kind == 'tilde':
        ty = target_type if target_type in NUMERIC_TYPES else rng.choice(NUMERIC_TYPES)
        inner = gen_expr(state, ty, depth + 1)
        return f'~{inner[0]}', ty

    if kind == 'plus':
        inner = gen_expr(state, target_type, depth + 1)
        return f'+{inner[0]}', inner[1]

    if kind == 'deref':
        # Need a pointer expression
        ptr_ty = (pointee_type(target_type) + '*') if target_type else random_ptr_type(rng)
        inner = gen_expr(state, ptr_ty, depth + 1)
        return f'*{inner[0]}', pointee_type(ptr_ty)

    if kind == 'addr':
        var = gen_variable(state)
        if var:
            return f'&{var[0]}', var[1] + '*'
        return gen_literal(state, target_type)

    if kind == 'pow':
        inner = gen_expr(state, None, depth + 1)
        return f'**{inner[0]}', pointee_type(inner[1]) if type_is_ptr(inner[1]) else inner[1]

    return gen_literal(state, target_type)


def gen_call(state: FuzzerState, target_type: str | None, depth: int) -> tuple[str, str]:
    rng = state.rng
    candidates = []
    weights = []

    # input/input_str — registered runtime functions (disabled: no stdin in fuzzer)
    # if target_type is None or target_type == 'int':
    #     candidates.append(('input', lambda: ('input()', 'int')))
    #     weights.append(30)
    # if target_type is None or target_type == 'str':
    #     candidates.append(('input_str', lambda: ('input_str()', 'str')))
    #     weights.append(15)

    # User-defined functions
    if state.funcs:
        func_name = rng.choice(list(state.funcs.keys()))
        func_info = state.funcs[func_name]
        params = func_info.get('params', [])   # list of (name, type)
        ret = func_info.get('ret') or 'void'
        if target_type is None or ret == target_type or (ret == 'void' and target_type is None):
            def gen_user_call(p=params, r=ret):
                args = []
                for pname, ptype in p:
                    arg, _ = gen_expr(state, ptype, depth + 1)
                    args.append(arg)
                return f'{func_name}({", ".join(args)})', r
            candidates.append(('user_func', gen_user_call))
            weights.append(55)

    if not candidates:
        return gen_literal(state, target_type)
    kind, fn = rng.choices(candidates, weights=weights)[0]
    return fn()


def gen_new(state: FuzzerState, target_type: str | None, depth: int) -> tuple[str, str]:
    rng = state.rng
    inner_ty = rng.choice(TYPES)
    if target_type and type_is_ptr(target_type):
        inner_ty = pointee_type(target_type)
    result_ty = inner_ty + '*'
    if rng.random() < 0.4:
        size = gen_expr(state, 'int', depth + 1)
        return f'new {inner_ty}[{size[0]}]', result_ty
    return f'new {inner_ty}', result_ty


def gen_sizeof(state: FuzzerState) -> tuple[str, str]:
    rng = state.rng
    ty = random_type(rng, allow_ptr=True, allow_array=True)
    return f'sizeof({ty})', 'int'


# ---------------------------------------------------------------------------
# Statement generators
# ---------------------------------------------------------------------------

def gen_var_decl(state: FuzzerState, indent: int, depth: int) -> str | None:
    rng = state.rng
    pad = '    ' * indent
    ty = random_type(rng)
    name = state.fresh()
    has_init = rng.random() < 0.7
    if has_init:
        init_text, _ = gen_expr(state, ty, depth + 1)
        state.scope.add(name, ty)
        return f'{pad}{ty} {name} = {init_text}'
    state.scope.add(name, ty)
    return f'{pad}{ty} {name}'


def gen_assign(state: FuzzerState, indent: int, depth: int) -> str | None:
    rng = state.rng
    pad = '    ' * indent
    all_v = state.scope.all_vars()
    if not all_v:
        return None
    name, ty = rng.choice(all_v)
    value, _ = gen_expr(state, ty, depth + 1)
    # Occasionally add index
    if rng.random() < 0.1 and type_is_ptr(ty):
        idx, _ = gen_expr(state, 'int', depth + 1)
        return f'{pad}{name}[{idx}] = {value}'
    return f'{pad}{name} = {value}'


def gen_compound_assign(state: FuzzerState, indent: int, depth: int) -> str | None:
    rng = state.rng
    pad = '    ' * indent
    all_v = state.scope.all_vars()
    if not all_v:
        return None
    # Only numeric types support compound assignment
    numeric_vars = [(n, t) for n, t in all_v if type_is_numeric(t)]
    if not numeric_vars:
        return None
    name, ty = rng.choice(numeric_vars)
    op = rng.choice(['+=', '-=', '*=', '/=', '//='])
    value, _ = gen_expr(state, ty, depth + 1)
    return f'{pad}{name} {op} {value}'


def gen_if(state: FuzzerState, indent: int, depth: int) -> str:
    rng = state.rng
    pad = '    ' * indent
    cond, _ = gen_expr(state, None, depth + 1)
    body = gen_body(state, depth + 1, indent + 1)
    result = f'{pad}if {cond}:\n' + '\n'.join(body)
    if rng.random() < 0.3:
        else_body = gen_body(state, depth + 1, indent + 1)
        result += f'\n{pad}else:\n' + '\n'.join(else_body)
    return result


def gen_while(state: FuzzerState, indent: int, depth: int) -> str:
    pad = '    ' * indent
    cond, _ = gen_expr(state, None, depth + 1)
    state.loop_depth += 1
    body = gen_body(state, depth + 1, indent + 1)
    state.loop_depth -= 1
    return f'{pad}while {cond}:\n' + '\n'.join(body)


def gen_for(state: FuzzerState, indent: int, depth: int) -> str:
    rng = state.rng
    pad = '    ' * indent
    v = state.fresh()
    state.scope.add(v, 'char')
    iter_expr = gen_literal(state, 'str')[0]
    state.loop_depth += 1
    body = gen_body(state, depth + 1, indent + 1)
    state.loop_depth -= 1
    return f'{pad}for {v} in {iter_expr}:\n' + '\n'.join(body)


def gen_print(state: FuzzerState, indent: int, depth: int) -> str:
    pad = '    ' * indent
    expr, _ = gen_expr(state, None, depth + 1)
    return f'{pad}print({expr})'


def gen_expr_stmt(state: FuzzerState, indent: int, depth: int) -> str:
    pad = '    ' * indent
    expr, _ = gen_expr(state, None, depth + 1)
    return f'{pad}{expr}'


def gen_return(state: FuzzerState, indent: int) -> str:
    pad = '    ' * indent
    return f'{pad}return 0'




def gen_body(state: FuzzerState, depth: int, indent: int) -> list[str]:
    rng = state.rng
    if depth > 6:
        return []
    pad = '    ' * indent
    n = rng.choices([0, 1, 2, 3, 4, 5, 6, 7], weights=[3, 15, 20, 20, 15, 10, 5, 2])[0]
    stmts = []
    for _ in range(n):
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
        if s:
            stmts.append(s)
    if not stmts:
        stmts.append(f'{pad}pass')
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
    for _ in range(nfields):
        fname = rng.choice(FIELD_NAMES)
        # Pick either pointer OR array, not both
        if rng.random() < 0.2:
            ftype = rng.choice(TYPES) + '[' + str(rng.randint(1, 10)) + ']'
        elif rng.random() < 0.2:
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

    # Imports (rarely)
    if rng.random() < 0.05:
        lines.append('import "stdio.h"')

    # Structs
    n_structs = rng.choices([0, 1, 2, 3], weights=[40, 30, 20, 10])[0]
    for _ in range(n_structs):
        lines.append(gen_struct(state))
        lines.append('')

    # Global variables
    for _ in range(rng.randint(0, 4)):
        ty = random_type(rng)
        name = state.fresh('g')
        if rng.random() < 0.5:
            # Don't add name to scope yet — init can't reference itself
            init_text, _ = gen_expr(state, ty, 0)
            state.scope.add(name, ty)
            lines.append(f'{ty} {name} = {init_text}')
        else:
            state.scope.add(name, ty)
            lines.append(f'{ty} {name}')
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
        err = analyze(source, parsed, strict=False)
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
