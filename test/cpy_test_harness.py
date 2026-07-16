#!/usr/bin/env python3
"""
Cpyte unified test harness: valid code generation, fuzzing, differential testing,
and corpus mutation.

Modes:
  fuzz [N] --seed S      Generate and test random programs
  diff FILE.cpy            Differential test a single file (opt=0 vs opt=3)
  mutate [N]               Load crash corpus, mutate each program, re-test
  all [N] --seed S         Run fuzz then mutate

Examples:
  python cpy_test_harness.py fuzz 5000 --seed 42
  python cpy_test_harness.py diff test/crashes/crash_0000.cpy
  python cpy_test_harness.py mutate 2000
  python cpy_test_harness.py all 3000
"""

import sys, os, random, traceback, io, contextlib, string, re, copy, itertools
import multiprocessing as mp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'source'))

from cpyte.lexar import Lexer, LexerError
from cpyte.astparse import parse_file, ParseError, Token, TokenType, BinOp, UnaryOp, Number, String, Variable, Assign, Print, Return, VarDecl, FuncDef, StructDef, Field, If, While, Call, NewExpr, SizeOf, Index, Attr
from cpyte.semantic_analasis import analyze
from cpyte.bytecoding import LLVM
from cpyte.compiling import run_jit

TIMEOUT_S = 30
CRASH_DIR = os.path.join(os.path.dirname(__file__), 'crashes')
os.makedirs(CRASH_DIR, exist_ok=True)

_crash_counter = 0

# ---------------------------------------------------------------------------
# Edge-case value tables (from fuzzer.py)
# ---------------------------------------------------------------------------
EDGE_INTS = [0, 1, -1, 127, 128, 255, 256, 32767, 32768, 65535,
             2147483647, -2147483648, 4294967295, 9223372036854775807,
             -9223372036854775808, 18446744073709551615]
BIG_EDGE = [999999999999999999999999999999, 340282366920938463463374607431768211455,
            340282366920938463463374607431768211456,
            6277101735386680764176071790128604879565730051895802724352627,
            7775847889649854587847588477,
            100000000000000000000000000000000000000,
            123456789012345678901234567890123456789012345678901234567890]
EDGE_HEX = [0x7fffffff, 0x80000000, 0xffffffff, 0x100000000,
            0x7fffffffffffffff, 0x8000000000000000, 0xffffffffffffffff,
            0x10000000000000000]
BIG_EDGE_HEX = [0x10000000000000000, 0xdeadbeefcafebabedeadbeef,
                0xffffffffffffffffffffffffffffffff,
                0x80000000000000000000000000000000,
                0x7fffffffffffffffffffffffffffffff]
EDGE_FLOATS = [0.0, 1.0, -1.0, 0.5, 3.14159, 2.71828, 1e10, 1e-10,
               0.0000000001, 9999999999999999.0]

TYPES = ['int', 'int64', 'uint64', 'float', 'double', 'char', 'str', 'big']
NUMERIC_TYPES = ['int', 'int64', 'uint64', 'float', 'double', 'big']
FLOAT_TYPES = ['float', 'double']
ALL_SCALAR = TYPES + ['bool']
FIELD_NAMES = ['head', 'tail', 'key', 'value', 'next', 'prev',
               'left', 'right', 'name', 'x', 'y', 'data']

# ---------------------------------------------------------------------------
# Scope and state
# ---------------------------------------------------------------------------
class Scope:
    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent
    def add(self, name, ty):
        self.vars[name] = ty
    def lookup(self, name):
        if name in self.vars:
            return self.vars[name]
        if self.parent:
            return self.parent.lookup(name)
        return None
    def all_vars(self):
        seen = set()
        items = []
        s = self
        while s:
            for k, v in s.vars.items():
                if k not in seen:
                    seen.add(k)
                    items.append((k, v))
            s = s.parent
        return items
    def vars_of_type(self, ty):
        return [(n, t) for n, t in self.all_vars() if t == ty]

class FuzzerState:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.scope = Scope()
        self.counter = 0
        self.loop_depth = 0
        self.structs = {}
        self.funcs = {}
    def fresh(self, prefix='v'):
        self.counter += 1
        return f'{prefix}{self.counter}'

# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------
def base_type(ty):
    while ty.endswith('*') or ty.endswith('[]'):
        ty = ty[:-1]
    return ty

def random_scalar_type(rng, allow_big=True):
    pool = [t for t in TYPES if allow_big or t != 'big']
    return rng.choice(pool)

def random_ptr_type(rng):
    return rng.choice(TYPES) + '*'

def random_type(rng, allow_big=True):
    return rng.choice([random_scalar_type(rng, allow_big), random_ptr_type(rng)])

def type_is_int(ty):
    return ty in ('int', 'int64', 'uint64', 'char', 'bool')

def type_is_float(ty):
    return ty in ('float', 'double')

def type_is_numeric(ty):
    return type_is_int(ty) or type_is_float(ty) or ty == 'big'

def type_is_ptr(ty):
    return ty.endswith('*') or ty.endswith('[]')

def pointee_type(ty):
    if ty.endswith('*'):
        return ty[:-1]
    if ty.endswith('[]'):
        return ty[:-2]
    return None

# ---------------------------------------------------------------------------
# Expression generators
# ---------------------------------------------------------------------------
def gen_literal(state, target_type=None):
    rng = state.rng
    if target_type == 'bool':
        return ('true' if rng.random() < 0.5 else 'false', 'bool')
    if target_type == 'char':
        return (repr(rng.choice(string.ascii_letters + string.digits + ' \t\n')), 'char')
    if target_type == 'big':
        pool = BIG_EDGE + BIG_EDGE_HEX
        v = rng.choice(pool)
        if isinstance(v, int) and v > 0xffffffffffffffff and rng.random() < 0.5:
            return (f'0x{v:x}', 'big')
        return (str(v), 'big')
    if target_type in ('int64', 'uint64', 'int'):
        pool = EDGE_INTS + EDGE_HEX
        v = rng.choice(pool)
        fmt = hex if isinstance(v, int) and rng.random() < 0.3 and v != 0 else str
        return (fmt(v), target_type)
    if target_type in ('float', 'double'):
        return (str(rng.choice(EDGE_FLOATS)), target_type)
    if target_type == 'str':
        length = rng.randint(1, 8)
        s = ''.join(rng.choices(string.ascii_lowercase + string.digits + '\t\n', k=length))
        return (repr(s), 'str')
    if target_type and target_type.endswith('*'):
        return ('null', target_type)
    if target_type is None:
        scalar = rng.choice(TYPES)
        return gen_literal(state, scalar)
    return (str(rng.randint(0, 100)), 'int')

def gen_variable(state, target_type=None):
    vars = state.scope.all_vars()
    if target_type:
        vars = [(n, t) for n, t in vars if t == target_type]
    if not vars:
        return None
    name, ty = state.rng.choice(vars)
    return (name, ty)

def gen_expr(state, depth=0):
    rng = state.rng
    max_depth = 5
    choices = []

    choices.append((15, lambda: gen_literal(state)))
    if state.scope.all_vars():
        choices.append((20, lambda: gen_variable(state) or gen_literal(state)))
        choices.append((10, lambda: gen_variable(state, rng.choice(NUMERIC_TYPES)) or gen_literal(state)))
    if depth < max_depth:
        choices.append((30, lambda: gen_binop(state, depth + 1)))
        choices.append((12, lambda: gen_unary(state, depth + 1)))
    if state.funcs:
        choices.append((15, lambda: gen_call(state, depth + 1)))
    choices.append((5, lambda: gen_new(state)))
    choices.append((3, lambda: gen_sizeof(state)))

    weights = [c[0] for c in choices]
    fns = [c[1] for c in choices]
    fn = rng.choices(fns, weights=weights)[0]
    return fn()

def gen_binop(state, depth):
    rng = state.rng
    left, lt = gen_expr(state, depth)
    right, rt = gen_expr(state, depth)

    left_ptr = type_is_ptr(lt)
    right_ptr = type_is_ptr(rt)
    both_ptr = left_ptr and right_ptr

    left_int = type_is_int(lt) or lt == 'big' if not left_ptr else False
    right_int = type_is_int(rt) or rt == 'big' if not right_ptr else False
    both_int = left_int and right_int
    left_num = left_int or type_is_float(lt)
    right_num = right_int or type_is_float(rt)
    both_num = left_num and right_num

    ops = []
    if both_num and not left_ptr and not right_ptr:
        ops += ['+', '-', '*']
    if both_num and lt != 'big' and rt != 'big' and not left_ptr and not right_ptr:
        ops += ['/', '//', '%']
    if both_int and not left_ptr and not right_ptr:
        ops += ['&', '|', '^']
    if both_int and lt != 'big' and rt != 'big' and not left_ptr and not right_ptr:
        ops += ['<<', '>>']
    if both_ptr or (left_num and right_num):
        ops += ['==', '!=', '<', '>', '<=', '>=']
    ops += ['and', 'or']

    if not ops:
        return (f'({left} == {right})', 'bool')
    op = rng.choice(ops)
    return (f'({left} {op} {right})', 'bool' if op in ('==','!=','<','>','<=','>=','and','or') else lt)

def gen_unary(state, depth):
    rng = state.rng
    operand, ot = gen_expr(state, depth)
    ops = []
    is_ptr = type_is_ptr(ot)
    is_float = type_is_float(ot)
    is_int = type_is_int(ot) and not is_ptr
    is_big = ot == 'big'

    if (is_int or is_float or is_big) and not is_ptr:
        ops.append('-')
    if is_int and not is_big:
        ops.append('~')
    ops.append('not')
    if (is_int or is_float) and not is_ptr and not is_big:
        ops.append('+')
    if (is_int or is_float) and not is_ptr and not is_big:
        ops.append('--')
    if not ops:
        return (operand, ot)
    op = rng.choice(ops)
    return (f'({op} {operand})', ot)

def gen_call(state, depth):
    rng = state.rng
    name = rng.choice(list(state.funcs.keys()))
    func = state.funcs[name]
    args = []
    for pname, ptype in func['params']:
        arg, _ = gen_expr(state, depth)
        args.append(arg)
    return (f'{name}({", ".join(args)})', func['ret'] or 'int')

def gen_new(state):
    rng = state.rng
    ty = rng.choice(TYPES)
    if rng.random() < 0.3:
        size, _ = gen_expr(state, 1)
        return (f'new {ty}[{size}]', f'{ty}*')
    return (f'new {ty}', f'{ty}*')

def gen_sizeof(state):
    rng = state.rng
    ty = rng.choice(TYPES + ['int64', 'uint64', 'float', 'double', 'char'])
    return (f'sizeof({ty})', 'int')

# ---------------------------------------------------------------------------
# Statement generators
# ---------------------------------------------------------------------------
def gen_var_decl(state):
    rng = state.rng
    ty = random_type(rng, allow_big=True)
    name = state.fresh('v')
    state.scope.add(name, ty)
    if rng.random() < 0.6:
        val, vt = gen_expr(state)
        return (f'{ty} {name} = {val}', ty)
    return (f'{ty} {name}', ty)

def gen_assign(state):
    rng = state.rng
    vars = state.scope.all_vars()
    if not vars:
        return None
    name, ty = rng.choice(vars)
    val, vt = gen_expr(state)
    return f'{name} = {val}'

def gen_compound_assign(state):
    rng = state.rng
    vars = state.scope.all_vars()
    if not vars:
        return None
    name, ty = rng.choice(vars)
    if not type_is_numeric(ty):
        return None
    val, vt = gen_expr(state)
    ops = ['+=', '-=', '*=', '/=', '//='] if ty != 'big' else ['+=', '-=', '*=']
    op = rng.choice(ops)
    return f'{name} {op} {val}'

def gen_if(state, depth=0):
    rng = state.rng
    cond, ct = gen_expr(state, depth + 1)
    body = gen_body(state, depth + 1)
    parts = [f'if {cond}:']
    parts.extend(f'    {s}' for s in body)
    if rng.random() < 0.3 and depth < 3:
        else_body = gen_body(state, depth + 1)
        parts.append('else:')
        parts.extend(f'    {s}' for s in else_body)
    return '\n'.join(parts)

def gen_while(state, depth=0):
    rng = state.rng
    state.loop_depth += 1
    cond, ct = gen_expr(state, depth + 1)
    body = gen_body(state, depth + 1)
    state.loop_depth -= 1
    parts = [f'while {cond}:']
    parts.extend(f'    {s}' for s in body)
    return '\n'.join(parts)

def gen_for(state, depth=0):
    rng = state.rng
    state.loop_depth += 1
    length = rng.randint(1, 6)
    chars = ''.join(rng.choices(string.ascii_lowercase + '\t\n ', k=length))
    body = gen_body(state, depth + 1)
    state.loop_depth -= 1
    parts = [f"for {state.fresh('i')} in {repr(chars)}:"]
    parts.extend(f'    {s}' for s in body)
    return '\n'.join(parts)

def gen_print(state):
    val, vt = gen_expr(state)
    return f'print({val})'

def gen_return(state):
    return 'return 0'

MAX_DEPTH = 6

def gen_body(state, depth=0):
    if depth >= MAX_DEPTH:
        return [gen_return(state)]
    rng = state.rng
    n = rng.randint(0, 5)
    stmts = []
    for _ in range(n):
        choices = [
            (20, lambda: gen_var_decl(state)),
            (20, lambda: gen_assign(state)),
            (8, lambda: gen_compound_assign(state)),
            (8, lambda: gen_if(state, depth)),
            (8, lambda: gen_while(state, depth)),
            (6, lambda: gen_for(state, depth)),
            (15, lambda: gen_print(state)),
            (10, lambda: gen_expr_stmt(state)),
        ]
        weights = [c[0] for c in choices]
        fns = [c[1] for c in choices]
        fn = rng.choices(fns, weights=weights)[0]
        result = fn()
        if result:
            if isinstance(result, tuple):
                stmts.append(result[0])
            else:
                stmts.append(result)
    if depth == 0:
        stmts.append('return 0')
    return stmts

def gen_expr_stmt(state):
    val, vt = gen_expr(state)
    return val

# ---------------------------------------------------------------------------
# Program generator
# ---------------------------------------------------------------------------
def gen_program(state):
    rng = state.rng
    state.scope = Scope()
    state.counter = 0
    state.loop_depth = 0
    state.structs = {}
    state.funcs = {}

    lines = []
    imports = ''

    n_structs = rng.randint(0, 2)
    for _ in range(n_structs):
        sname = f'S{rng.randint(1, 9)}'
        n_fields = rng.randint(1, 4)
        fields = []
        for _ in range(n_fields):
            ft = random_type(rng, allow_big=True)
            fname = rng.choice(FIELD_NAMES)
            fields.append(f'    {ft} {fname}')
        lines.append(f'struct {sname}:')
        lines.extend(fields)
        state.structs[sname] = fields

    n_globals = rng.randint(0, 3)
    for _ in range(n_globals):
        gt = random_type(rng, allow_big=True)
        gname = state.fresh('g')
        state.scope.add(gname, gt)
        if rng.random() < 0.4:
            val, vt = gen_literal(state, gt)
            lines.append(f'{gt} {gname} = {val}')
        else:
            lines.append(f'{gt} {gname}')

    lines.append('')
    lines.append('def main() -> int:')
    state.scope = Scope(state.scope)
    body = gen_body(state, depth=0)
    lines.extend(f'    {s}' for s in body)

    return '\n'.join(lines)

# ---------------------------------------------------------------------------
# JIT runner
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Test pipeline
# ---------------------------------------------------------------------------
def run_program(source, label='', save=True):
    global _crash_counter
    try:
        tokens = Lexer(source).get_tokens()
        parsed, _ = parse_file(tokens)
    except (LexerError, ParseError, Exception):
        return 'reject'

    try:
        err = analyze(source, parsed, strict=False)
    except Exception as e:
        _save_crash(source, f'analyzer crash: {e}')
        return 'crash'
    if err:
        return 'reject'

    try:
        c = LLVM()
        prog, src_files = c.emit_program(parsed)
    except Exception as e:
        _save_crash(source, f'codegen crash: {e}')
        return 'crash'

    out0, ret0, err0 = _run_jit_capture(prog, src_files, opt_level=0)
    out3, ret3, err3 = _run_jit_capture(prog, src_files, opt_level=3)

    if err0 or err3:
        reason0 = err0 or 'ok'
        reason3 = err3 or 'ok'
        _save_crash(source, f'diff fail: unopt={reason0} opt={reason3}')
        return 'bug'
    if out0 != out3:
        _save_crash(source, f'output mismatch: unopt={out0!r} opt={out3!r}')
        return 'bug'
    return 'pass'

def _save_crash(source, error):
    global _crash_counter
    crash_id = _crash_counter
    _crash_counter += 1
    path = os.path.join(CRASH_DIR, f'crash_{crash_id:04d}.cpy')
    with open(path, 'w') as f:
        f.write(source)
    print(f'\n  *** SAVED crash_{crash_id:04d}.cpy ({error})', flush=True)

# ---------------------------------------------------------------------------
# Corpus mutation engine
# ---------------------------------------------------------------------------
_MUTATION_OPS_PLUS = ['+', '-']
_MUTATION_OPS_MUL = ['*', '/', '//', '%']
_MUTATION_OPS_BIT = ['&', '|', '^']
_MUTATION_OPS_SHIFT = ['<<', '>>']
_MUTATION_OPS_CMP = ['==', '!=', '<', '>', '<=', '>=']
_MUTATION_OPS_LOGICAL = ['and', 'or']

def _mutate_token_source(source):
    """Apply simple source-level mutations."""
    rng = random.Random()
    lines = source.split('\n')
    mutated = []
    for line in lines:
        # Skip empty/comments
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            mutated.append(line)
            continue

        # Only mutate expression lines inside function body (indented)
        is_expr = stripped.startswith(('print(', '(', 'ret'))
        if not is_expr:
            mutated.append(line)
            continue

        # 25% chance to mutate this line
        if rng.random() > 0.25:
            mutated.append(line)
            continue

        # --- Operator swaps ---
        for old_ops, new_ops in [
            (_MUTATION_OPS_PLUS, _MUTATION_OPS_MUL + _MUTATION_OPS_BIT),
            (_MUTATION_OPS_MUL, _MUTATION_OPS_PLUS),
            (_MUTATION_OPS_BIT, _MUTATION_OPS_PLUS + _MUTATION_OPS_SHIFT),
            (_MUTATION_OPS_SHIFT, _MUTATION_OPS_BIT),
            (_MUTATION_OPS_CMP, _MUTATION_OPS_CMP),
            (_MUTATION_OPS_LOGICAL, _MUTATION_OPS_LOGICAL),
        ]:
            for old in old_ops:
                if old in line and rng.random() < 0.3:
                    new = rng.choice(new_ops)
                    line = line.replace(old, new, 1)
                    break

        # --- Numeric literal perturbation ---
        def perturb_match(m):
            val = m.group(0)
            try:
                if val.startswith('0x'):
                    n = int(val, 16)
                    delta = rng.choice([1, -1, 10, -10, 256, -256])
                    return hex(max(0, n + delta))
                if val.isdigit() or (val.startswith('-') and val[1:].isdigit()):
                    n = int(val)
                    delta = rng.choice([1, -1, 10, -10, 1000, -1000])
                    return str(n + delta)
            except (ValueError, OverflowError):
                pass
            return val

        if rng.random() < 0.2:
            line = re.sub(r'\b\d+\b', perturb_match, line)
            line = re.sub(r'\b0x[0-9a-fA-F]+\b', perturb_match, line)

        # --- Toggle 'not' insertion ---
        if '==' in line and rng.random() < 0.15:
            line = line.replace('==', '!=', 1)
        elif '!=' in line and rng.random() < 0.15:
            line = line.replace('!=', '==', 1)

        mutated.append(line)

    return '\n'.join(mutated)  # return mutated source

def _mutate_ast_node(node, rng):
    """Recursively mutate an AST node, returning a modified copy."""
    if node is None:
        return None

    if isinstance(node, (list, tuple)):
        return [_mutate_ast_node(n, rng) for n in node]

    if isinstance(node, Number):
        if rng.random() < 0.3:
            val = rng.choice(EDGE_INTS + EDGE_HEX + BIG_EDGE)
            if isinstance(val, int) and val > 0xffffffffffffffff and rng.random() < 0.5:
                return Number(f'0x{val:x}', node._token)
            return Number(str(val), node._token)
        return node

    if isinstance(node, String):
        if rng.random() < 0.3:
            length = rng.randint(1, 6)
            s = ''.join(rng.choices(string.ascii_lowercase + '\t\n ', k=length))
            return String(repr(s), node._token)
        return node

    if isinstance(node, BinOp):
        left = _mutate_ast_node(node.left, rng)
        right = _mutate_ast_node(node.right, rng)
        if rng.random() < 0.25:
            pool = rng.choice([
                [TokenType.PLUS, TokenType.MINUS],
                [TokenType.MUL, TokenType.DIV, TokenType.SLASH_SLASH, TokenType.PERCENT],
                [TokenType.BIT_AND, TokenType.BIT_OR, TokenType.BIT_XOR],
                [TokenType.SHL, TokenType.SHR],
                [TokenType.EQ_EQ, TokenType.NOT_EQ, TokenType.LESS, TokenType.GREATER,
                 TokenType.LESS_EQ, TokenType.GREATER_EQ],
                [TokenType.AND, TokenType.OR],
            ])
            if node.op in [o for p in [
                [TokenType.PLUS, TokenType.MINUS],
                [TokenType.MUL, TokenType.DIV, TokenType.SLASH_SLASH, TokenType.PERCENT],
                [TokenType.BIT_AND, TokenType.BIT_OR, TokenType.BIT_XOR],
                [TokenType.SHL, TokenType.SHR],
                [TokenType.EQ_EQ, TokenType.NOT_EQ, TokenType.LESS, TokenType.GREATER,
                 TokenType.LESS_EQ, TokenType.GREATER_EQ],
                [TokenType.AND, TokenType.OR],
            ] for o in p]:
                return BinOp(left, rng.choice(pool), right, node._token)
        return BinOp(left, node.op, right, node._token)

    if isinstance(node, UnaryOp):
        operand = _mutate_ast_node(node.operand, rng)
        if rng.random() < 0.2:
            if node.op == TokenType.NOT:
                return operand
            return UnaryOp(TokenType.NOT, operand, node._token)
        return UnaryOp(node.op, operand, node._token)

    if isinstance(node, Call):
        args = [_mutate_ast_node(a, rng) for a in node.args]
        return Call(node.callee, args, node._token)

    if isinstance(node, Variable):
        return node

    if isinstance(node, Assign):
        return Assign(node.target, _mutate_ast_node(node.value, rng), node._token)

    if isinstance(node, Print):
        return Print(_mutate_ast_node(node.value, rng), node._token)

    if isinstance(node, Return):
        if node.value is not None:
            return Return(_mutate_ast_node(node.value, rng), node._token)
        return node

    if isinstance(node, VarDecl):
        if node.init is not None:
            return VarDecl(node.var_type, node.name, _mutate_ast_node(node.init, rng), node._token)
        return node

    if isinstance(node, If):
        body = [_mutate_ast_node(s, rng) for s in node.body]
        orelse = [_mutate_ast_node(s, rng) for s in (node.orelse or [])]
        if hasattr(node, 'orelse'):
            return If(node.cond, body, node._token, orelse)
        return If(node.cond, body, node._token)

    if isinstance(node, While):
        body = [_mutate_ast_node(s, rng) for s in node.body]
        return While(node.cond, body, node._token)

    if isinstance(node, FuncDef):
        body = [_mutate_ast_node(s, rng) for s in node.body]
        return FuncDef(node.name, node.return_type, node.params, body, node._token)

    if isinstance(node, StructDef):
        return node

    if isinstance(node, Field):
        return node

    if isinstance(node, NewExpr):
        if node.size is not None:
            return NewExpr(node.type_expr, _mutate_ast_node(node.size, rng), node._token)
        return node

    if isinstance(node, SizeOf):
        return node

    if isinstance(node, Index):
        return Index(_mutate_ast_node(node.value, rng), _mutate_ast_node(node.index, rng), node._token)

    if isinstance(node, Attr):
        return node

    if isinstance(node, Call):
        args = [_mutate_ast_node(a, rng) for a in node.args]
        return Call(node.callee, args, node._token)

def mutate_source_via_ast(source, rng=None):
    """Parse source, mutate the AST, re-generate source."""
    if rng is None:
        rng = random.Random()
    try:
        tokens = Lexer(source).get_tokens()
        parsed, _ = parse_file(tokens)
        parsed = _mutate_ast_node(parsed, rng)
        result = _ast_to_source(parsed)
        if result.strip():
            return result
    except Exception:
        pass
    return _mutate_token_source(source)

def _ast_to_source(node, indent=0):
    """Crude AST-to-source serialization for mutation output."""
    prefix = '    ' * indent
    if node is None:
        return ''
    if isinstance(node, (list, tuple)):
        return '\n\n'.join(_ast_to_source(n, indent) for n in node)
    if isinstance(node, Number):
        return node.value
    if isinstance(node, String):
        return repr(node.value) if hasattr(node, 'value') else node.val
    if isinstance(node, Variable):
        return node.name
    if isinstance(node, BinOp):
        op_map = {
            TokenType.PLUS: '+', TokenType.MINUS: '-', TokenType.MUL: '*',
            TokenType.DIV: '/', TokenType.SLASH_SLASH: '//', TokenType.PERCENT: '%',
            TokenType.BIT_AND: '&', TokenType.BIT_OR: '|', TokenType.BIT_XOR: '^',
            TokenType.SHL: '<<', TokenType.SHR: '>>',
            TokenType.EQ_EQ: '==', TokenType.NOT_EQ: '!=',
            TokenType.LESS: '<', TokenType.GREATER: '>',
            TokenType.LESS_EQ: '<=', TokenType.GREATER_EQ: '>=',
            TokenType.AND: 'and', TokenType.OR: 'or',
        }
        op_s = op_map.get(node.op, '?')
        left_s = _ast_to_source(node.left)
        right_s = _ast_to_source(node.right)
        return f'({left_s} {op_s} {right_s})'
    if isinstance(node, UnaryOp):
        op_map = {
            TokenType.PLUS: '+', TokenType.MINUS: '-', TokenType.TILDE: '~',
            TokenType.NOT: 'not', TokenType.MINUS_MINUS: '--', TokenType.POWER: '**',
        }
        op_s = op_map.get(node.op, '?')
        operand_s = _ast_to_source(node.operand)
        return f'{op_s} {operand_s}'
    if isinstance(node, Print):
        return f'print({_ast_to_source(node.value)})'
    if isinstance(node, Return):
        if node.value is not None:
            return f'return {_ast_to_source(node.value)}'
        return 'return'
    if isinstance(node, Assign):
        return f'{_ast_to_source(node.target)} = {_ast_to_source(node.value)}'
    if isinstance(node, VarDecl):
        result = f'{node.var_type} {node.name}'
        if node.init is not None:
            result += f' = {_ast_to_source(node.init)}'
        return result
    if isinstance(node, FuncDef):
        params = ', '.join(f'{t} {n}' for t, n in (node.params or []))
        body = '\n'.join(f'    {_ast_to_source(s, indent+1)}' for s in (node.body or []))
        return f'def {node.name}({params}) -> {node.return_type}:\n{body}'
    if isinstance(node, StructDef):
        fields = '\n'.join(f'    {_ast_to_source(f, indent+1)}' for f in (node.fields or []))
        return f'struct {node.name}:\n{fields}'
    if isinstance(node, Field):
        return f'{node.type_expr} {node.name}'
    if isinstance(node, If):
        body = '\n'.join(f'    {_ast_to_source(s, indent+1)}' for s in node.body)
        else_body = ''
        if hasattr(node, 'orelse') and node.orelse:
            else_body = '\nelse:\n' + '\n'.join(f'    {_ast_to_source(s, indent+1)}' for s in node.orelse)
        return f'if {_ast_to_source(node.cond)}:\n{body}{else_body}'
    if isinstance(node, While):
        body = '\n'.join(f'    {_ast_to_source(s, indent+1)}' for s in node.body)
        return f'while {_ast_to_source(node.cond)}:\n{body}'
    if isinstance(node, Call):
        args = ', '.join(_ast_to_source(a) for a in node.args)
        return f'{node.callee.name}({args})'
    if isinstance(node, NewExpr):
        if node.size:
            return f'new {node.type_expr}[{_ast_to_source(node.size)}]'
        return f'new {node.type_expr}'
    if isinstance(node, SizeOf):
        return f'sizeof({node.type_expr})'
    if isinstance(node, Index):
        return f'{_ast_to_source(node.value)}[{_ast_to_source(node.index)}]'
    if isinstance(node, Attr):
        return f'{_ast_to_source(node.value)}.{node.member}'
    return ''

# ---------------------------------------------------------------------------
# Corpus mutation runner
# ---------------------------------------------------------------------------
def run_corpus_mutation(n_iter=1000):
    crash_files = sorted([
        os.path.join(CRASH_DIR, f)
        for f in os.listdir(CRASH_DIR)
        if f.endswith('.cpy')
    ])
    if not crash_files:
        print('No crash corpus found. Run fuzz first.')
        return

    print(f'Loaded {len(crash_files)} crash files for mutation.')
    rng = random.Random()
    stats = {'pass': 0, 'reject': 0, 'crash': 0, 'bug': 0}

    for i in range(n_iter):
        src_path = rng.choice(crash_files)
        try:
            with open(src_path) as f:
                source = f.read()
        except Exception:
            continue

        # Apply AST-level mutation
        if rng.random() < 0.6:
            mutated = mutate_source_via_ast(source, rng)
        else:
            mutated = source
            # Shuffle/swap variable names for diversity
            var_names = re.findall(r'\bg\d+\b|\bv\d+\b', mutated)
            if len(var_names) >= 2:
                a, b = rng.sample(var_names, 2)
                mutated = mutated.replace(a, '__TMP__').replace(b, a).replace('__TMP__', b)

        result = run_program(mutated, label=f'mutate {i}', save=True)
        stats[result] = stats.get(result, 0) + 1

        if (i + 1) % 200 == 0:
            print(f'  mutate [{i+1}/{n_iter}] pass={stats["pass"]} '
                  f'reject={stats["reject"]} bug={stats["bug"]}', flush=True)

    print(f'\nMutation done: {stats}')

# ---------------------------------------------------------------------------
# Fuzz runner
# ---------------------------------------------------------------------------
def run_fuzz(n=5000, seed=None):
    if seed is None:
        seed = random.randint(0, 2**31 - 1)
    rng = random.Random(seed)
    print(f'Fuzzing: seed={seed}, iterations={n}')

    stats = {'pass': 0, 'reject': 0, 'crash': 0, 'bug': 0}
    state = FuzzerState(seed)

    for i in range(n):
        source = gen_program(state)
        result = run_program(source, label=f'iter {i}', save=True)
        stats[result] = stats.get(result, 0) + 1
        state = FuzzerState(rng.randint(0, 2**31 - 1))

        if (i + 1) % 500 == 0:
            print(f'  [{i+1}/{n}] pass={stats["pass"]} reject={stats["reject"]} '
                  f'crashes={stats["crash"]} bugs={stats["bug"]}', flush=True)

    crash_count = len([f for f in os.listdir(CRASH_DIR) if f.endswith('.cpy')])
    print(f'\nDone: {n} tests | pass={stats["pass"]} reject={stats["reject"]} '
          f'crashes={stats["crash"]} bugs={stats["bug"]}')
    print(f'Crashes saved in: {CRASH_DIR} ({crash_count} files)')
    print(f'Seed: {seed}')

# ---------------------------------------------------------------------------
# Differential test for a single file
# ---------------------------------------------------------------------------
def run_differential(path):
    if not os.path.exists(path):
        print(f'File not found: {path}')
        return
    with open(path) as f:
        source = f.read()

    print(f'Differential testing: {path}')
    print(f'--- source ({len(source)} bytes) ---')
    print(source.strip())
    print('---')

    try:
        tokens = Lexer(source).get_tokens()
        parsed, _ = parse_file(tokens)
    except (LexerError, ParseError) as e:
        print(f'PARSE ERROR: {e}')
        return

    try:
        err = analyze(source, parsed, strict=False)
    except Exception as e:
        print(f'ANALYZER CRASH: {e}')
        traceback.print_exc()
        return
    if err:
        print(f'SEMANTIC ERRORS ({len(err)}):')
        for e in err:
            print(f'  {e}')
        return

    try:
        c = LLVM()
        prog, src_files = c.emit_program(parsed)
    except Exception as e:
        print(f'CODEGEN CRASH: {e}')
        traceback.print_exc()
        return
    print('Codegen OK.')

    out0, ret0, err0 = _run_jit_capture(prog, src_files, opt_level=0)
    out3, ret3, err3 = _run_jit_capture(prog, src_files, opt_level=3)

    print(f'  opt=0: out={out0!r} ret={ret0} err={err0}')
    print(f'  opt=3: out={out3!r} ret={ret3} err={err3}')

    if err0 or err3:
        print('  => DIFFERENTIAL FAILURE')
    elif out0 != out3:
        print('  => OUTPUT MISMATCH')
    else:
        print('  => OK (outputs match)')

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        return

    mode = sys.argv[1]
    args = sys.argv[2:]

    if mode == 'fuzz':
        n = int(args[0]) if args and args[0].isdigit() else 5000
        seed = None
        if '--seed' in args:
            idx = args.index('--seed')
            seed = int(args[idx + 1])
        run_fuzz(n, seed)

    elif mode == 'diff':
        if not args:
            print('Usage: cpy_test_harness.py diff FILE.cpy')
            return
        run_differential(args[0])

    elif mode == 'mutate':
        n = int(args[0]) if args and args[0].isdigit() else 1000
        run_corpus_mutation(n)

    elif mode == 'all':
        n = int(args[0]) if args and args[0].isdigit() else 3000
        seed = None
        if '--seed' in args:
            idx = args.index('--seed')
            seed = int(args[idx + 1])
        print('=== Fuzz ===')
        run_fuzz(max(n // 2, 100), seed)
        print('\n=== Mutate ===')
        run_corpus_mutation(max(n // 2, 100))

    else:
        print(f'Unknown mode: {mode}')
        print(__doc__)

if __name__ == '__main__':
    main()
