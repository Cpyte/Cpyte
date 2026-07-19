import os
import re
import subprocess

def _get_sdk_paths():
    paths = []
    try:
        sdk = subprocess.run(['xcrun', '--show-sdk-path'], capture_output=True, text=True, timeout=5)
        if sdk.returncode == 0 and sdk.stdout.strip():
            paths.append(sdk.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    base = '/Library/Developer/CommandLineTools/SDKs'
    if os.path.isdir(base):
        for entry in sorted(os.listdir(base), reverse=True):
            full = os.path.join(base, entry)
            if os.path.isdir(full) and entry.startswith('MacOSX'):
                if full not in paths:
                    paths.append(full)
    return paths

from .lexar import Lexer, LexerError
from .astparse import (
    _loc, Number, String, Variable, Call, Index, Attr,
    UnaryOp, BinOp, Assign, Return, If, FuncDef, Print, ExprStmt,
    VarDecl, Break, Continue, Switch, Import, While,
    NewExpr, Deref, AddrOf, SizeOf, StructDef, Field, Input,
    InputStr, Signed67,
    parse_file, ParseError,
)
from .clib import resolve_library, parse_header_file, parse_c_source


class Diagnostic:
    __slots__ = ('message', 'token', 'note', 'level')

    def __init__(self, message: str, token=None, note: str | None = None, level: str = 'error'):
        self.message = message
        self.token = token
        self.note = note
        self.level = level


class Reporter:
    def __init__(self, source: str):
        self.source = source
        self.lines = source.split('\n')
        self.diagnostics: list[Diagnostic] = []

    def error(self, message: str, token=None, note: str | None = None):
        self.diagnostics.append(Diagnostic(message, token, note, level='error'))

    def strict_error(self, message: str, token=None, note: str | None = None):
        self.diagnostics.append(Diagnostic(message, token, note, level='strict-error'))

    def strict_warning(self, message: str, token=None, note: str | None = None):
        self.diagnostics.append(Diagnostic(message, token, note, level='strict-warning'))

    def has_errors(self) -> bool:
        return any(d.level in ('error', 'strict-error') for d in self.diagnostics)

    def display(self) -> str:
        if not self.diagnostics:
            return ''

        parts = []
        for diag in self.diagnostics:
            parts.append(self._format(diag))

        err_count = sum(1 for d in self.diagnostics if d.level in ('error', 'strict-error'))
        warn_count = sum(1 for d in self.diagnostics if d.level == 'strict-warning')
        summary_parts = []
        if err_count:
            plural = 's' if err_count > 1 else ''
            summary_parts.append(f'{err_count} semantic error{plural}')
        if warn_count:
            plural = 's' if warn_count > 1 else ''
            summary_parts.append(f'{warn_count} strict warning{plural}')
        if summary_parts:
            parts.append('found: ' + ', '.join(summary_parts) + '.')
        return '\n'.join(parts)

    def _format(self, diag: Diagnostic) -> str:
        token = diag.token
        if token is None:
            return f'{diag.level}: {diag.message}'

        line = token.line
        col = token.column

        source_line = self.lines[line - 1] if 0 < line <= len(self.lines) else ''

        line_str = str(line)
        pad = ' ' * (len(line_str) + 1)

        parts = [
            f'{diag.level}[{line}:{col}]: {diag.message}',
            f'{pad}|',
            f' {line_str} | {source_line}',
            f'{pad}| {" " * (col - 1)}^',
        ]

        if diag.note:
            parts.append(f'{pad}| {diag.note}')

        return '\n'.join(parts)


class Symbol:
    __slots__ = ('kind', 'type', 'node', 'const_value')

    def __init__(self, kind: str, type_: str | None = None, node=None):
        self.kind = kind
        self.type = type_
        self.node = node
        self.const_value = None


class Scope:
    def __init__(self, parent: 'Scope | None' = None):
        self.parent = parent
        self.symbols: dict[str, Symbol] = {}

    def define(self, name: str, symbol: Symbol):
        self.symbols[name] = symbol

    def lookup(self, name: str) -> Symbol | None:
        if name in self.symbols:
            return self.symbols[name]
        if self.parent is not None:
            return self.parent.lookup(name)
        return None

    def lookup_local(self, name: str) -> Symbol | None:
        return self.symbols.get(name)


class SemanticAnalyzer:
    def __init__(self, source: str, filepath: str | None = None,
                 workspace_root: str | None = None, strict: bool = False):
        self.reporter = Reporter(source)
        self.globals = Scope()
        self.current_func: FuncDef | None = None
        self.locals: Scope | None = None
        self.filepath = filepath
        self._filedir = os.path.dirname(filepath) if filepath else None
        self._workspace_root = workspace_root
        self._loop_depth = 0
        self.strict = strict

    def _tok(self, node):
        if isinstance(node, dict):
            return node.get('_token')
        return getattr(node, '_token', None)

    def error(self, message: str, node=None, note: str | None = None):
        self.reporter.error(message, self._tok(node), note)

    def _strict_error(self, message: str, node=None, note: str | None = None):
        if self.strict:
            self.reporter.strict_error(message, self._tok(node), note)

    def _strict_warning(self, message: str, node=None, note: str | None = None):
        if self.strict:
            self.reporter.strict_warning(message, self._tok(node), note)

    def _is_literal_zero(self, node) -> bool:
        if not isinstance(node, Number):
            return False
        if node.inferred_type == 'float':
            return False
        try:
            return int(node.value, 0) == 0
        except (ValueError, TypeError):
            return False

    def _is_compile_time_false(self, node) -> bool:
        if isinstance(node, Number):
            try:
                v = int(node.value, 0)
                return v == 0
            except (ValueError, TypeError):
                try:
                    return float(node.value) == 0.0
                except (ValueError, TypeError):
                    return False
        return False

    def _is_compile_time_true(self, node) -> bool:
        return isinstance(node, Number) and not self._is_compile_time_false(node)

    def analyze(self, nodes: list) -> bool:
        for node in nodes:
            self._visit(node)
        if self.reporter.has_errors():
            return False
        return True

    def _infer_type(self, node):
        if isinstance(node, Number):
            # Check for hexadecimal literals (0x prefix) BEFORE float 'e' check,
            # since hex values legitimately contain the letter 'e' as a digit (0-9a-f)
            if node.value.startswith('0x') or node.value.startswith('0X'):
                try:
                    val = int(node.value, 16)
                    if val > 2**31 - 1 or val < -2**31:
                        if val <= 2**63 - 1:
                            node.inferred_type = 'int64'
                            return 'int64'
                        if val <= 2**64 - 1:
                            node.inferred_type = 'uint64'
                            return 'uint64'
                        node.inferred_type = 'big'
                        return 'big'
                    node.inferred_type = 'int'
                    return 'int'
                except ValueError:
                    pass
                node.inferred_type = 'int64'
                return 'int64'
            if '.' in node.value or 'e' in node.value or 'E' in node.value:
                node.inferred_type = 'float'
                return 'float'
            # Check for large decimal values that might need 64-bit
            try:
                val = int(node.value)
                if val > 2**31 - 1 or val < -2**31:
                    if val <= 2**63 - 1 and val >= -2**63:
                        node.inferred_type = 'int64'
                        return 'int64'
                    if val <= 2**64 - 1:
                        node.inferred_type = 'uint64'
                        return 'uint64'
                    node.inferred_type = 'big'
                    return 'big'
            except ValueError:
                pass
            # For small integers, return 'int' but allow implicit conversion to int64
            node.inferred_type = 'int'
            return 'int'

        if isinstance(node, String):
            return 'str'

        if isinstance(node, Variable):
            sym = self.current_scope.lookup(node.name)
            if sym is None:
                self.error(f'use of undeclared identifier `{node.name}`', node,
                           note=f'no definition found in this scope')
                return None
            if sym.const_value is not None:
                node.const_value = sym.const_value
            return sym.type

        if isinstance(node, BinOp):
            left_t = self._infer_type(node.left)

            if node.op.name in ('AND', 'OR'):
                if node.op.name == 'AND' and self._is_compile_time_false(node.left):
                    self._infer_type(node.right)
                    node.inferred_type = 'bool'
                    return 'bool'
                if node.op.name == 'OR' and self._is_compile_time_true(node.left):
                    self._infer_type(node.right)
                    node.inferred_type = 'bool'
                    return 'bool'
                right_t = self._infer_type(node.right)
                node.inferred_type = 'bool'
                return 'bool'

            right_t = self._infer_type(node.right)

            if node.op.name in ('EQ_EQ', 'NOT_EQ', 'LESS', 'GREATER', 'LESS_EQ', 'GREATER_EQ'):
                if left_t is not None and right_t is not None and left_t != right_t:
                    ok = (left_t == 'int' and right_t.endswith('*'))
                    ok = ok or (left_t.endswith('*') and right_t == 'int')
                    ok = ok or (left_t == 'str' and (right_t.endswith('*') or right_t == 'char'))
                    ok = ok or (right_t == 'str' and (left_t.endswith('*') or left_t == 'char'))
                    ok = ok or (left_t, right_t) in (
                        ('float', 'double'), ('double', 'float'),
                        ('int', 'int64'), ('int', 'uint64'),
                        ('int64', 'uint64'), ('uint64', 'int64'),
                        ('str', 'char'), ('char', 'str'),
                        ('char', 'int'), ('int', 'char'),
                        ('char', 'int64'), ('int64', 'char'),
                    )
                    ok = ok or left_t == 'big' and right_t in ('int', 'int64', 'uint64', 'big')
                    ok = ok or right_t == 'big' and left_t in ('int', 'int64', 'uint64', 'big')
                    if not ok:
                        self.error(
                            f'incompatible types in comparison: `{left_t}` vs `{right_t}`',
                            node,
                            note=f'both sides of `{node.op.name}` must be the same type'
                        )
                if left_t == 'big' or right_t == 'big':
                    node.inferred_type = 'bool'
                    return 'bool'
                node.inferred_type = 'bool'
                return 'bool'

            if node.op.name in ('SHL', 'SHR', 'AMPERSAND', 'PIPE', 'CARET', 'PERCENT', 'SLASH_SLASH'):
                if node.op.name in ('SHL', 'SHR', 'AMPERSAND', 'PIPE', 'CARET'):
                    # Bitwise operations not supported for big
                    valid_int_types = ('int', 'int64', 'uint64')
                    if left_t == 'big' or right_t == 'big':
                        self.error(
                            f'bitwise operator `{node.op.name}` not supported for `big` operands',
                            node,
                            note=f'got `{left_t}` and `{right_t}`'
                        )
                    elif (left_t is not None and left_t not in valid_int_types) or (right_t is not None and right_t not in valid_int_types):
                        self.error(
                            f'bitwise operator `{node.op.name}` requires integer operands',
                            node,
                            note=f'got `{left_t}` and `{right_t}`'
                        )
                    if left_t in ('int64', 'uint64') or right_t in ('int64', 'uint64'):
                        node.inferred_type = 'int64'
                        return 'int64'
                    node.inferred_type = 'int'
                    return 'int'
                # PERCENT, SLASH_SLASH
                valid_int_types = ('int', 'int64', 'uint64', 'big')
                if (left_t is not None and left_t not in valid_int_types) or (right_t is not None and right_t not in valid_int_types):
                    self.error(
                        f'operator `{node.op.name}` requires integer operands',
                        node,
                        note=f'got `{left_t}` and `{right_t}`'
                    )
                if self._is_literal_zero(node.right):
                    self.error(
                        f'division by zero in `{node.op.name}`',
                        node,
                        note=f'cannot divide or mod by zero'
                    )
                # Type promotion for mixed integer types
                if left_t == 'big' or right_t == 'big':
                    node.inferred_type = 'big'
                    return 'big'
                if left_t in ('int64', 'uint64') or right_t in ('int64', 'uint64'):
                    node.inferred_type = 'int64'
                    return 'int64'  # Simplified: promote to int64 for mixed operations
                node.inferred_type = 'int'
                return 'int'

            if node.op.name in ('PLUS', 'MINUS', 'STAR', 'SLASH', 'POW'):
                if left_t == 'str' and right_t == 'str' and node.op.name == 'PLUS':
                    node.inferred_type = 'str'
                    return 'str'
                if left_t == 'str' or right_t == 'str':
                    self.error(
                        f'operator `{node.op.name}` not supported for string operands',
                        node,
                        note=f'strings only support `+` (concatenation)'
                    )
                    result = left_t if left_t is not None else right_t
                    node.inferred_type = result
                    return result
                if node.op.name == 'SLASH':
                    int_types = ('int', 'int64', 'uint64')
                    if left_t in int_types and right_t in int_types and self._is_literal_zero(node.right):
                        self.error(
                            f'division by zero',
                            node,
                            note=f'cannot divide integer by zero'
                        )
                if left_t is not None and right_t is not None and left_t != right_t:
                    # Allow mixed integer types with promotion
                    valid_int_types = ('int', 'int64', 'uint64', 'big')
                    if left_t in valid_int_types and right_t in valid_int_types:
                        # Promote to larger type
                        if left_t == 'big' or right_t == 'big':
                            node.inferred_type = 'big'
                            return 'big'
                        if left_t in ('int64', 'uint64') or right_t in ('int64', 'uint64'):
                            node.inferred_type = 'int64'
                            return 'int64'
                        node.inferred_type = 'int'
                        return 'int'  # Both are int
                    else:
                        self.error(
                            f'mismatched types `{left_t}` and `{right_t}` in arithmetic expression',
                            node,
                            note=f'cannot apply `{node.op.name}` to different types'
                        )
                if left_t == 'float' or right_t == 'float':
                    node.inferred_type = 'float'
                    return 'float'
                # Return the larger integer type
                if left_t == 'big' or right_t == 'big':
                    node.inferred_type = 'big'
                    return 'big'
                if left_t in ('int64', 'uint64') or right_t in ('int64', 'uint64'):
                    node.inferred_type = 'int64'
                    return 'int64'
                node.inferred_type = 'int'
                return 'int'

            node.inferred_type = left_t
            return left_t

        if isinstance(node, UnaryOp):
            operand_t = self._infer_type(node.operand)
            if node.op.name == 'NOT':
                node.inferred_type = 'int'
                return 'int'
            if node.op.name == 'MINUS':
                valid_types = ('int', 'float', 'int64', 'uint64', 'big')
                if operand_t is not None and operand_t not in valid_types:
                    self.error(
                        f'cannot apply unary minus to `{operand_t}`',
                        node,
                        note='unary minus expects numeric type'
                    )
            if node.op.name == 'TILDE':
                valid_types = ('int', 'int64', 'uint64')
                if operand_t is not None and operand_t not in valid_types:
                    self.error(
                        f'bitwise NOT (`~`) not supported for `{operand_t}`',
                        node,
                        note='bitwise NOT expects int, int64, or uint64 operand'
                    )
            if node.op.name == 'MINUS_MINUS':
                if operand_t == 'big':
                    self.error(
                        'decrement (`--`) not supported for `big`',
                        node,
                        note='big integers do not support decrement'
                    )
            node.inferred_type = operand_t
            return operand_t

        if isinstance(node, Call):
            sym = self._resolve_callee(node.callee)
            if sym is not None:
                for arg in node.args:
                    self._infer_type(arg)
                self._check_call_args(node, sym)
                return sym.type
            return None

        if isinstance(node, Input):
            return 'int'

        if isinstance(node, InputStr):
            return 'str'

        if isinstance(node, Signed67):
            return 'str'

        if isinstance(node, Index):
            obj_t = self._infer_type(node.obj)
            self._infer_type(node.index)
            if obj_t and obj_t.endswith('[]'):
                return obj_t[:-2]
            if obj_t == 'str':
                return 'char'
            if obj_t is not None:
                self.error(
                    f'cannot index value of type `{obj_t}`',
                    node,
                    note='indexing requires a string or array type'
                )
            return None

        if isinstance(node, Attr):
            obj_t = self._infer_type(node.obj)
            if obj_t:
                struct_sym = self.current_scope.lookup(obj_t)
                if struct_sym and struct_sym.kind == 'struct' and struct_sym.node:
                    for field in struct_sym.node.fields:
                        if field.name == node.name:
                            return field.type_expr
                    self.error(
                        f'type `{obj_t}` has no field `{node.name}`',
                        node
                    )
                    return None
                self.error(
                    f'cannot access field `{node.name}` on non-struct type `{obj_t}`',
                    node
                )
                return None
            return None

        if isinstance(node, Deref):
            operand_t = self._infer_type(node.operand)
            if operand_t is not None and operand_t.endswith('*'):
                return operand_t[:-1]
            if operand_t is not None:
                self.error(f'cannot dereference non-pointer type `{operand_t}`', node)
            elif operand_t is None and hasattr(node.operand, '_token'):
                self.error('cannot dereference value of unknown type', node)
            return operand_t

        if isinstance(node, AddrOf):
            operand_t = self._infer_type(node.operand)
            if operand_t:
                return operand_t + '*'
            return None

        if isinstance(node, NewExpr):
            if node.size is not None:
                self._infer_type(node.size)
                return node.type_expr + '[]'
            return node.type_expr + '*'

        if isinstance(node, SizeOf):
            return 'int'

        if isinstance(node, ExprStmt):
            return self._infer_type(node.expr)

        return None

    @property
    def current_scope(self) -> Scope:
        return self.locals if self.locals is not None else self.globals

    def _resolve_callee(self, callee):
        if isinstance(callee, Variable):
            sym = self.current_scope.lookup(callee.name)
            if sym is None:
                self.error(f'use of undeclared identifier `{callee.name}`', callee,
                           note='call target must be a function defined in scope')
                return None
            if sym.kind not in ('function', 'builtin_func'):
                self.error(f'`{callee.name}` is not callable', callee,
                           note=f'declared as `{sym.kind}`, not a function')
                return None
            return sym
        return None

    def _check_call_args(self, call: Call, sym: Symbol):
        expected_count = 0
        if sym.node and isinstance(sym.node, FuncDef):
            expected_count = len(sym.node.params)
        elif sym.node and isinstance(sym.node, Import):
            for fname, (_, params, vararg) in sym.node.symbols:
                if fname == call.callee.name:
                    expected_count = len(params)
                    if vararg:
                        return
                    break
        actual_count = len(call.args)
        if expected_count != actual_count:
            name = call.callee.name if isinstance(call.callee, Variable) else '?'
            self.error(
                f'wrong number of arguments to `{name}`',
                call,
                note=f'expects {expected_count}, got {actual_count}'
            )

    def _visit(self, node, scope: Scope | None = None):
        if isinstance(node, FuncDef):
            self._visit_funcdef(node, scope)
        elif isinstance(node, If):
            self._visit_if(node, scope)
        elif isinstance(node, Return):
            self._visit_return(node)
        elif isinstance(node, Assign):
            self._visit_assign(node, scope)
        elif isinstance(node, VarDecl):
            self._visit_vardecl(node, scope)
        elif isinstance(node, Print):
            self._visit_print(node)
        elif isinstance(node, Break):
            self._visit_break(node)
        elif isinstance(node, Continue):
            self._visit_continue(node)
        elif isinstance(node, While):
            self._visit_while(node, scope)
        elif isinstance(node, Switch):
            self._visit_switch(node, scope)
        elif isinstance(node, ExprStmt):
            self._infer_type(node.expr)
        elif isinstance(node, Import):
            self._visit_import(node)
        elif isinstance(node, StructDef):
            self._visit_struct(node, scope)
        elif isinstance(node, Deref):
            self._infer_type(node)
        elif isinstance(node, AddrOf):
            self._infer_type(node)
        elif isinstance(node, NewExpr):
            self._infer_type(node)
        elif isinstance(node, SizeOf):
            pass
        elif isinstance(node, Input):
            pass
        elif isinstance(node, InputStr):
            pass
        elif isinstance(node, Signed67):
            pass
        elif isinstance(node, dict):
            self._visit_dict(node, scope)
        elif isinstance(node, (list, tuple)):
            for n in node:
                self._visit(n, scope)

    def _resolve_module_path(self, module: str, sdk_path: str | None = None) -> str | None:
        candidates = [module]
        if self._filedir:
            candidates.append(os.path.normpath(os.path.join(self._filedir, module)))
        if self._workspace_root:
            candidates.append(os.path.normpath(os.path.join(self._workspace_root, module)))
        if module.endswith('.h') and '/' in module:
            parts = module.split('/')
            framework_name = parts[0]
            header_path = '/'.join(parts[1:])
            sdk_paths = [sdk_path] if sdk_path else []
            sdk_paths.extend(_get_sdk_paths())
            for sdk in sdk_paths:
                for framework_dir in (
                    os.path.join(sdk, 'System/Library/Frameworks', f'{framework_name}.framework', 'Headers'),
                    os.path.join(sdk, 'System/Library/Frameworks', f'{framework_name}.framework', 'Versions/A/Headers'),
                ):
                    candidate = os.path.join(framework_dir, header_path)
                    if candidate not in candidates:
                        candidates.append(candidate)
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return None

    def _visit_import(self, node: Import):
        module = node.module

        if node.is_package:
            self._visit_package_import(node, module)
            return

        resolved = self._resolve_module_path(module, node.sdk_path)

        is_file_import = (module.endswith('.c') or module.endswith('.cc')
                          or module.endswith('.h') or module.endswith('.cpy')
                          or '/' in module)

        if not is_file_import:
            result = resolve_library(module)
            if result is None:
                self.error(f'unknown library `{module}`', node)
            else:
                symbols, kind = result
                self._register_import_symbols(symbols, node)
            return

        if resolved is None:
            self.error(f'file not found: `{module}`', node)
            return

        search_paths = [node.sdk_path] if node.sdk_path else []
        search_paths.extend(_get_sdk_paths())

        if module.endswith('.c') or module.endswith('.cc'):
            result = parse_c_source(resolved)
            if result:
                self._register_import_symbols(result[0], node)
            node.src_file = resolved
        elif module.endswith('.h'):
            result = parse_header_file(resolved, search_paths)
            symbols, _kind, constants, framework, var_names = result
            node.constants = constants
            if framework:
                node.frameworks.append(framework)
            self._register_import_symbols(symbols, node, var_names)
            self._register_import_constants(constants, node)
            return
        elif module.endswith('.cpy'):
            result = self._import_cpy(resolved, node)
            if result:
                self._register_import_symbols(result[0], node)
        elif '/' in module:
            ext = module.rsplit('.', 1)[-1] if '.' in module else ''
            if ext in ('c', 'cc'):
                result = parse_c_source(resolved)
                if result:
                    self._register_import_symbols(result[0], node)
                node.src_file = resolved
            elif ext == 'cpy':
                result = self._import_cpy(resolved, node)
                if result:
                    self._register_import_symbols(result[0], node)
            else:
                result = parse_header_file(resolved, search_paths)
                symbols, _kind, constants, framework, var_names = result
                node.constants = constants
                if framework:
                    node.frameworks.append(framework)
                self._register_import_symbols(symbols, node, var_names)
                return
        else:
            result = None

        if result is None:
            self.error(f'unknown library `{module}`', node)

    def _register_import_symbols(self, symbols, node, var_names=None):
        s = self.globals
        var_names = var_names or set()
        node.var_names = var_names
        for fname, (ret_type, params, vararg) in symbols.items():
            existing = s.lookup_local(fname)
            if not existing:
                kind = 'variable' if fname in var_names else 'function'
                s.define(fname, Symbol(kind, ret_type, node))
        node.symbols = list(symbols.items())

    def _register_import_constants(self, constants, node):
        s = self.globals
        for name, val in constants.items():
            existing = s.lookup_local(name)
            if not existing:
                sym = Symbol('variable', 'int', node)
                sym.const_value = val
                s.define(name, sym)

    def _find_package_entry(self, search_dir: str, pkg_name: str) -> str | None:
        for dir_candidate in (search_dir, os.path.join(search_dir, 'src')):
            if not os.path.isdir(dir_candidate):
                continue
            for entry_name in (f'{pkg_name}.cpy', 'package.cpy'):
                entry_path = os.path.join(dir_candidate, entry_name)
                if os.path.isfile(entry_path):
                    return entry_path
        return None

    def _import_prebuilt(self, ll_dir: str, node: Import):
        ll_files = []
        symbols = {}
        for root, _dirs, files in os.walk(ll_dir):
            for f in sorted(files):
                if not f.endswith('.ll'):
                    continue
                path = os.path.join(root, f)
                with open(path) as fh:
                    content = fh.read()
                for m in re.finditer(r'^\s*(?:define|declare)\s+.*?@(\w+)\s*\(([^)]*)\)', content, re.MULTILINE):
                    func_name = m.group(1)
                    params_str = m.group(2).strip()
                    param_count = len([p for p in params_str.split(',') if p.strip()]) if params_str else 0
                    if func_name not in symbols:
                        symbols[func_name] = ('int', [(f'p{i}', 'int') for i in range(param_count)], False)
                ll_files.append(path)
        if symbols:
            node.prebuilt_ll_files = ll_files
            self._register_import_symbols(symbols, node)
            return symbols, 'prebuilt'
        return None

    def _visit_package_import(self, node: Import, module: str):
        cpm_root = None
        if self._workspace_root:
            cpm_root = os.path.join(self._workspace_root, '.cpm', 'modules')
        elif self._filedir:
            cpm_root = os.path.join(self._filedir, '..', '.cpm', 'modules')
        if cpm_root and os.path.isdir(cpm_root):
            pkg_dir = os.path.join(cpm_root, module)
            if os.path.isdir(pkg_dir):
                versions = sorted([d for d in os.listdir(pkg_dir) if os.path.isdir(os.path.join(pkg_dir, d))], reverse=True)
                if versions:
                    pkg_name = module.rsplit('/', 1)[-1]
                    version_dir = os.path.join(pkg_dir, versions[0])
                    cpy_file = self._find_package_entry(version_dir, pkg_name)
                    if cpy_file:
                        result = self._import_cpy(cpy_file, node)
                        if result:
                            self._register_import_symbols(result[0], node)
                        return
                    result = self._import_prebuilt(version_dir, node)
                    if result:
                        return
        self.error(f"package `{module}` not installed — run 'cpm install'", node)

    def _import_cpy(self, module: str, node: Import | None = None):
        try:
            with open(module) as f:
                source = f.read()
        except FileNotFoundError:
            self.error(f'file not found: `{module}`')
            return None

        lex = Lexer(source)
        try:
            tokens = lex.get_tokens()
        except LexerError as e:
            self.error(f'lex error in imported `{module}`: {e}')
            return None

        try:
            imported_ast, _ = parse_file(tokens)
        except ParseError as e:
            self.error(f'parse error in imported `{module}`: {e}')
            return None

        # Run semantic analysis on imported file
        sub = SemanticAnalyzer(source, filepath=module, workspace_root=self._workspace_root)
        if not sub.analyze(imported_ast):
            self.error(f'imported file `{module}` has semantic errors')
            return None

        # Extract public functions and structs
        symbols = {}
        sub_ast = []
        for ast_node in imported_ast:
            if isinstance(ast_node, FuncDef) and ast_node.visibility == 'public':
                params = [(name, ptype) for name, ptype in ast_node.params.items()]
                ret_type = ast_node.rettype or 'int'
                symbols[ast_node.name] = (ret_type, params, False)
                sub_ast.append(ast_node)
            elif isinstance(ast_node, StructDef):
                sub_ast.append(ast_node)
                existing = self.globals.lookup_local(ast_node.name)
                if not existing:
                    self.globals.define(ast_node.name, Symbol('struct', None, ast_node))
            elif isinstance(ast_node, Import):
                if getattr(ast_node, 'is_package', False):
                    for fname, (ret_type, params, vararg) in ast_node.symbols:
                        if fname not in symbols:
                            symbols[fname] = (ret_type, params, vararg)

        if node is not None:
            node.sub_ast = sub_ast
        return symbols, 'cpy'

    def _visit_dict(self, node: dict, scope: Scope | None = None):
        t = node.get('type')
        if t == 'class':
            self._visit_class(node, scope)
        elif t == 'while':
            self._visit_while(node, scope)
        elif t == 'for':
            self._visit_for(node, scope)

    def _visit_funcdef(self, node: FuncDef, scope: Scope | None = None):
        s = scope or self.globals
        existing = s.lookup_local(node.name)
        if existing:
            self.error(f'redefinition of `{node.name}`', node,
                       note='a function with this name already exists in this scope')
            return

        sym = Symbol('function', node.rettype or 'void', node)
        s.define(node.name, sym)

        old_func = self.current_func
        self.current_func = node
        old_locals = self.locals
        self.locals = Scope(s)

        for param_name, param_type in node.params.items():
            self.locals.define(param_name, Symbol('variable', param_type or None, node))

        for stmt in node.body:
            self._visit(stmt, self.locals)

        self.current_func = old_func
        self.locals = old_locals

    def _visit_if(self, node: If, scope: Scope | None = None):
        self._infer_type(node.cond)
        old_locals = self.locals
        body_scope = Scope(scope or self.current_scope)
        self.locals = body_scope
        for stmt in node.body:
            self._visit(stmt, body_scope)
        if node.orelse:
            else_scope = Scope(scope or self.current_scope)
            self.locals = else_scope
            for stmt in node.orelse:
                self._visit(stmt, else_scope)
        self.locals = old_locals

    def _visit_return(self, node: Return):
        if node.value is not None:
            val_type = self._infer_type(node.value)
            if self.current_func:
                expected = self.current_func.rettype
                if expected and val_type and expected != val_type:
                    self.error(
                        f'return type `{val_type}` does not match declared return type `{expected}`',
                        node,
                        note=f'in function `{self.current_func.name}`'
                    )
        else:
            if self.current_func and self.current_func.rettype and self.current_func.rettype != 'void':
                self.error(
                    f'missing return value in function returning `{self.current_func.rettype}`',
                    node,
                    note=f'function `{self.current_func.name}` expects a return value of type `{self.current_func.rettype}`'
                )

    def _visit_assign(self, node: Assign, scope: Scope | None = None):
        val_type = self._infer_type(node.value)
        if isinstance(node.target, (Variable, str)):
            name = node.target.name if isinstance(node.target, Variable) else node.target
            s = scope or self.current_scope
            existing = s.lookup_local(name)
            if existing is None:
                existing = s.lookup(name)
            if existing is None:
                s.define(name, Symbol('variable', val_type, node))
            elif val_type is not None and existing.type is not None and val_type != existing.type:
                # Allow implicit conversion from int to int64/uint64
                # Allow implicit conversion between int64 and uint64
                # Allow int literal 0 as null for any pointer type
                # Allow float/double interchange (same LLVM type)
                # Allow str/char* interchange (same LLVM type)
                narrowing = {
                    ('int64', 'int'), ('uint64', 'int'),
                    ('double', 'float'),
                }
                valid_conversions = [
                    ('int', 'int64'), ('int', 'uint64'),
                    ('int64', 'int'), ('uint64', 'int'),
                    ('int64', 'uint64'), ('uint64', 'int64'),
                    ('float', 'double'), ('double', 'float'),
                    ('str', 'char'), ('char', 'str'),
                    ('int', 'big'), ('int64', 'big'), ('uint64', 'big'),
                    ('big', 'big'),
                ]
                ok = (val_type, existing.type) in valid_conversions
                ok = ok or (val_type == 'int' and existing.type.endswith('*'))
                ok = ok or (val_type == 'str' and (existing.type.endswith('*') or existing.type == 'char'))
                ok = ok or (existing.type == 'str' and (val_type.endswith('*') or val_type == 'char'))
                if ok and (val_type, existing.type) in narrowing:
                    self._strict_error(
                        f'narrowing conversion from `{val_type}` to `{existing.type}` in assignment',
                        node
                    )
                if ok and (val_type == 'int' and existing.type.endswith('*')):
                    self._strict_warning(
                        f'implicit int-to-pointer conversion in assignment to `{existing.type}`',
                        node,
                        note='use 0 literal for null pointer'
                    )
                if not ok:
                    self.error(
                        f'cannot assign `{val_type}` to variable `{name}` of type `{existing.type}`',
                        node
                    )
        elif isinstance(node.target, Attr):
            obj_t = self._infer_type(node.target.obj)
            if obj_t:
                struct_sym = scope.lookup(obj_t) if scope else self.current_scope.lookup(obj_t)
                if struct_sym and struct_sym.kind == 'struct' and struct_sym.node:
                    for field in struct_sym.node.fields:
                        if field.name == node.target.name and val_type is not None and field.type_expr != val_type:
                            ok = (val_type == 'int' and field.type_expr.endswith('*'))
                            ok = ok or (val_type == 'str' and (field.type_expr.endswith('*') or field.type_expr == 'char'))
                            ok = ok or (field.type_expr == 'str' and (val_type.endswith('*') or val_type == 'char'))
                            ok = ok or (val_type, field.type_expr) in (
                                ('int', 'int64'), ('int', 'uint64'),
                                ('int64', 'uint64'), ('uint64', 'int64'),
                                ('float', 'double'), ('double', 'float'),
                                ('str', 'char'), ('char', 'str'),
                            )
                            if not ok:
                                self.error(
                                    f'cannot assign `{val_type}` to field `{node.target.name}` of type `{field.type_expr}`',
                                    node
                                )
                            break
        else:
            self._infer_type(node.target)

    def _visit_vardecl(self, node: VarDecl, scope: Scope | None = None):
        val_type = node.var_type
        s = scope or self.current_scope
        existing = s.lookup_local(node.name)
        if existing:
            self.error(f'redeclaration of `{node.name}`', node,
                       note=f'variable already exists in this scope')
            return
        if node.init is not None:
            init_type = self._infer_type(node.init)
            if init_type is not None and val_type is not None and init_type != val_type:
                # Allow implicit conversion from int to int64/uint64
                # Allow implicit conversion between int64 and uint64
                # Allow int literal 0 as null for any pointer type
                # Allow float/double interchange
                # Allow str/char* interchange
                # Allow char to int (widening)
                narrowing = {
                    ('int64', 'int'), ('uint64', 'int'),
                    ('double', 'float'),
                }
                valid_conversions = [
                    ('int', 'int64'), ('int', 'uint64'),
                    ('int64', 'int'), ('uint64', 'int'),
                    ('int64', 'uint64'), ('uint64', 'int64'),
                    ('float', 'double'), ('double', 'float'),
                    ('str', 'char'), ('char', 'str'),
                    ('char', 'int'),
                    ('int', 'big'), ('int64', 'big'), ('uint64', 'big'),
                    ('big', 'big'),
                ]
                ok = (init_type, val_type) in valid_conversions
                ok = ok or (init_type == 'int' and val_type.endswith('*'))
                ok = ok or (init_type == 'str' and (val_type.endswith('*') or val_type == 'char'))
                ok = ok or (val_type == 'str' and (init_type.endswith('*') or init_type == 'char'))
                if ok and (init_type, val_type) in narrowing:
                    self._strict_error(
                        f'narrowing conversion from `{init_type}` to `{val_type}` in variable declaration',
                        node
                    )
                if ok and (init_type == 'int' and val_type.endswith('*')):
                    self._strict_warning(
                        f'implicit int-to-pointer conversion in declaration of `{val_type}`',
                        node,
                        note='use 0 literal for null pointer'
                    )
                if not ok:
                    self.error(
                        f'cannot initialize `{val_type}` variable with value of type `{init_type}`',
                        node
                    )
        s.define(node.name, Symbol('variable', val_type, node))

    def _visit_break(self, node: Break):
        if self._loop_depth == 0:
            self.error('break outside loop', node)

    def _visit_continue(self, node: Continue):
        if self._loop_depth == 0:
            self.error('continue outside loop', node)

    def _visit_switch(self, node: Switch, scope: Scope | None = None):
        self._infer_type(node.value)
        for val, body in node.cases:
            if val is not None:
                self._infer_type(val)
            for stmt in body:
                self._visit(stmt, scope)

    def _visit_print(self, node: Print):
        self._infer_type(node.value)

    def _visit_class(self, node: dict, scope: Scope | None = None):
        s = scope or self.globals
        class_sym = Symbol('class', None, node)
        s.define(node['name'], class_sym)
        class_scope = Scope(s)
        for stmt in node.get('body', []):
            self._visit(stmt, class_scope)

    def _visit_struct(self, node: StructDef, scope: Scope | None = None):
        s = scope or self.globals
        existing = s.lookup_local(node.name)
        if existing:
            self.error(f'redefinition of struct `{node.name}`', node)
            return
        s.define(node.name, Symbol('struct', None, node))
        struct_scope = Scope(s)
        for param in node.generic_params:
            struct_scope.define(param, Symbol('type_param', None, node))
        for field in node.fields:
            struct_scope.define(field.name, Symbol('field', field.type_expr, node))

    def _visit_while(self, node, scope: Scope | None = None):
        if isinstance(node, While):
            self._infer_type(node.cond)
            self._loop_depth += 1
            for stmt in node.body:
                self._visit(stmt, scope)
            self._loop_depth -= 1
        elif isinstance(node, dict):
            cond = node.get('cond')
            if cond is not None:
                self._infer_type(cond)
            self._loop_depth += 1
            for stmt in node.get('body', []):
                self._visit(stmt, scope)
            self._loop_depth -= 1

    def _visit_for(self, node: dict, scope: Scope | None = None):
        s = scope or self.current_scope
        loop_scope = Scope(s)
        loop_scope.define(node['var'], Symbol('variable', 'char', node))
        iterable = node.get('iter')
        if iterable is not None:
            self._infer_type(iterable)
        old_locals = self.locals
        self.locals = loop_scope
        self._loop_depth += 1
        for stmt in node.get('body', []):
            self._visit(stmt, loop_scope)
        self._loop_depth -= 1
        self.locals = old_locals


def analyze(source: str, nodes: list, strict: bool = False, workspace_root: str | None = None) -> str | None:
    analyzer = SemanticAnalyzer(source, strict=strict, workspace_root=workspace_root)
    if analyzer.analyze(nodes):
        return None
    return analyzer.reporter.display()
