import os

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
    __slots__ = ('kind', 'type', 'node')

    def __init__(self, kind: str, type_: str | None = None, node=None):
        self.kind = kind
        self.type = type_
        self.node = node


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
            if '.' in node.value or 'e' in node.value or 'E' in node.value:
                node.inferred_type = 'float'
                return 'float'
            # Check for hexadecimal literals (0x prefix)
            if node.value.startswith('0x') or node.value.startswith('0X'):
                # For large hex values, infer int64 or uint64 based on size
                try:
                    val = int(node.value, 16)
                    if val > 2**31 - 1 or val < -2**31:
                        # Prefer int64 for values that fit in signed range
                        if val <= 2**63 - 1:
                            node.inferred_type = 'int64'
                            return 'int64'
                        node.inferred_type = 'uint64'
                        return 'uint64'
                except ValueError:
                    pass
                node.inferred_type = 'int64'
                return 'int64'
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
                    self.error(
                        f'integer literal `{node.value}` exceeds maximum value',
                        node,
                        note=f'value too large for any integer type (max 2^64-1)'
                    )
                    node.inferred_type = 'int64'
                    return 'int64'
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
            return sym.type

        if isinstance(node, BinOp):
            left_t = self._infer_type(node.left)

            if node.op.name in ('AND', 'OR'):
                if node.op.name == 'AND' and self._is_compile_time_false(node.left):
                    return 'bool'
                if node.op.name == 'OR' and self._is_compile_time_true(node.left):
                    return 'bool'
                right_t = self._infer_type(node.right)
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
                    )
                    if not ok:
                        self.error(
                            f'incompatible types in comparison: `{left_t}` vs `{right_t}`',
                            node,
                            note=f'both sides of `{node.op.name}` must be the same type'
                        )
                return 'bool'

            if node.op.name in ('SHL', 'SHR', 'AMPERSAND', 'PIPE', 'CARET', 'PERCENT', 'SLASH_SLASH'):
                valid_int_types = ('int', 'int64', 'uint64')
                if (left_t is not None and left_t not in valid_int_types) or (right_t is not None and right_t not in valid_int_types):
                    self.error(
                        f'bitwise operator `{node.op.name}` requires integer operands',
                        node,
                        note=f'got `{left_t}` and `{right_t}`'
                    )
                if node.op.name in ('PERCENT', 'SLASH_SLASH') and self._is_literal_zero(node.right):
                    self.error(
                        f'division by zero in `{node.op.name}`',
                        node,
                        note=f'cannot divide or mod by zero'
                    )
                # Type promotion for mixed integer types
                if left_t in ('int64', 'uint64') or right_t in ('int64', 'uint64'):
                    return 'int64'  # Simplified: promote to int64 for mixed operations
                return 'int'

            if node.op.name in ('PLUS', 'MINUS', 'STAR', 'SLASH', 'POW'):
                if left_t == 'str' and right_t == 'str' and node.op.name == 'PLUS':
                    return 'str'
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
                    valid_int_types = ('int', 'int64', 'uint64')
                    if left_t in valid_int_types and right_t in valid_int_types:
                        # Promote to larger type
                        if left_t in ('int64', 'uint64') or right_t in ('int64', 'uint64'):
                            return 'int64'
                        return 'int'  # Both are int
                    else:
                        self.error(
                            f'mismatched types `{left_t}` and `{right_t}` in arithmetic expression',
                            node,
                            note=f'cannot apply `{node.op.name}` to different types'
                        )
                if left_t == 'float' or right_t == 'float':
                    return 'float'
                # Return the larger integer type
                if left_t in ('int64', 'uint64') or right_t in ('int64', 'uint64'):
                    return 'int64'
                return 'int'

            return left_t

        if isinstance(node, UnaryOp):
            operand_t = self._infer_type(node.operand)
            if node.op.name == 'MINUS':
                valid_types = ('int', 'float', 'int64', 'uint64')
                if operand_t is not None and operand_t not in valid_types:
                    self.error(
                        f'cannot apply unary minus to `{operand_t}`',
                        node,
                        note='unary minus expects numeric type'
                    )
            return operand_t

        if isinstance(node, Call):
            sym = self._resolve_callee(node.callee)
            if sym is not None:
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
            if obj_t and obj_t.endswith('[]'):
                return obj_t[:-2]
            if obj_t == 'str':
                return 'char'
            return None

        if isinstance(node, Attr):
            obj_t = self._infer_type(node.obj)
            if obj_t:
                struct_sym = self.current_scope.lookup(obj_t)
                if struct_sym and struct_sym.kind == 'struct' and struct_sym.node:
                    for field in struct_sym.node.fields:
                        if field.name == node.name:
                            return field.type_expr
            return None

        if isinstance(node, Deref):
            operand_t = self._infer_type(node.operand)
            if operand_t and operand_t.endswith('*'):
                return operand_t[:-1]
            if operand_t:
                self.error(f'cannot dereference non-pointer type `{operand_t}`', node)
            return operand_t

        if isinstance(node, AddrOf):
            operand_t = self._infer_type(node.operand)
            if operand_t:
                return operand_t + '*'
            return None

        if isinstance(node, NewExpr):
            if node.size is not None:
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

    def _resolve_module_path(self, module: str) -> str | None:
        candidates = [module]
        if self._filedir:
            candidates.append(os.path.normpath(os.path.join(self._filedir, module)))
        if self._workspace_root:
            candidates.append(os.path.normpath(os.path.join(self._workspace_root, module)))
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return None

    def _visit_import(self, node: Import):
        module = node.module
        resolved = self._resolve_module_path(module)

        is_file_import = (module.endswith('.c') or module.endswith('.cc')
                          or module.endswith('.h') or module.endswith('.cpy')
                          or '/' in module)

        if not is_file_import:
            result = resolve_library(module)
            if result is None:
                self.error(f'unknown library `{module}`', node)
            else:
                symbols, kind = result
                s = self.globals
                for fname, (ret_type, params, vararg) in symbols.items():
                    existing = s.lookup_local(fname)
                    if not existing:
                        s.define(fname, Symbol('function', ret_type, node))
                node.symbols = list(symbols.items())
            return

        if resolved is None:
            self.error(f'file not found: `{module}`', node)
            return

        if module.endswith('.c') or module.endswith('.cc'):
            result = parse_c_source(resolved)
            node.src_file = resolved
        elif module.endswith('.h'):
            result = parse_header_file(resolved)
        elif module.endswith('.cpy'):
            result = self._import_cpy(resolved, node)
        elif '/' in module:
            ext = module.rsplit('.', 1)[-1] if '.' in module else ''
            if ext in ('c', 'cc'):
                result = parse_c_source(resolved)
                node.src_file = resolved
            elif ext == 'cpy':
                result = self._import_cpy(resolved, node)
            else:
                result = parse_header_file(resolved)

        if result is None:
            self.error(f'unknown library `{module}`', node)
            return

        symbols, kind = result
        s = self.globals
        for fname, (ret_type, params, vararg) in symbols.items():
            existing = s.lookup_local(fname)
            if not existing:
                s.define(fname, Symbol('function', ret_type, node))
        node.symbols = list(symbols.items())

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
                pass

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
        for stmt in node.body:
            self._visit(stmt, scope)
        if node.orelse:
            for stmt in node.orelse:
                self._visit(stmt, scope)

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
        loop_scope.define(node['var'], Symbol('variable', None, node))
        iterable = node.get('iter')
        if iterable is not None:
            self._infer_type(iterable)
        self._loop_depth += 1
        for stmt in node.get('body', []):
            self._visit(stmt, loop_scope)
        self._loop_depth -= 1


def analyze(source: str, nodes: list, strict: bool = False) -> str | None:
    analyzer = SemanticAnalyzer(source, strict=strict)
    if analyzer.analyze(nodes):
        return None
    return analyzer.reporter.display()
