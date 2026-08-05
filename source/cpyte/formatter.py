from dataclasses import dataclass, field

from .lexar import Lexer, LexerError, TokenType
from . import astparse


class FormatError(Exception):
    pass


@dataclass
class FormatResult:
    formatted: str
    errors: list = field(default_factory=list)
    comment_count: int = 0
    parsed: object = None


_BINOP_TEXT = {
    TokenType.POW: '**',
    TokenType.STAR: '*',
    TokenType.SLASH: '/',
    TokenType.SLASH_SLASH: '//',
    TokenType.PERCENT: '%',
    TokenType.PLUS: '+',
    TokenType.MINUS: '-',
    TokenType.SHL: '<<',
    TokenType.SHR: '>>',
    TokenType.GREATER: '>',
    TokenType.LESS: '<',
    TokenType.GREATER_EQ: '>=',
    TokenType.LESS_EQ: '<=',
    TokenType.EQ_EQ: '==',
    TokenType.NOT_EQ: '!=',
    TokenType.AMPERSAND: '&',
    TokenType.CARET: '^',
    TokenType.PIPE: '|',
    TokenType.AND: 'and',
    TokenType.OR: 'or',
}

_UNARY_TEXT = {
    TokenType.MINUS: '-',
    TokenType.PLUS: '+',
    TokenType.NOT: 'not ',
    TokenType.TILDE: '~',
    TokenType.MINUS_MINUS: '--',
}

_COMPOUND_TEXT = {
    TokenType.PLUS: '+=',
    TokenType.MINUS: '-=',
    TokenType.STAR: '*=',
    TokenType.SLASH: '/=',
    TokenType.SLASH_SLASH: '//=',
}

_STRING_ESCAPES = {
    '\\': '\\\\',
    '"': '\\"',
    "'": "\\'",
    '\n': '\\n',
    '\t': '\\t',
    '\r': '\\r',
    '\x00': '\\0',
}

_DEF_LIKE = (astparse.FuncDef, astparse.StructDef, astparse.ClassDef, astparse.EnumDef, astparse.TypeAlias, astparse.Import)


def _escape_string(value: str) -> str:
    return '"' + ''.join(_STRING_ESCAPES.get(ch, ch) for ch in value) + '"'


def _scan_comments(source: str):
    comments = []
    for i, line in enumerate(source.split('\n'), start=1):
        idx = line.find('#')
        if idx == -1:
            continue
        text = line[idx:].rstrip()
        if text:
            comments.append((i, idx, text))
    return comments


def _line(node) -> int:
    token = getattr(node, '_token', None)
    if token is not None:
        return getattr(token, 'line', 0)
    return 0


def _node_signature(node):
    if isinstance(node, dict):
        return ('dict', node.get('type'), _line(node))
    if isinstance(node, (list, tuple)):
        return tuple(_node_signature(n) for n in node)
    cname = type(node).__name__
    if isinstance(node, astparse.Number):
        return (cname, node.value)
    if isinstance(node, astparse.String):
        return (cname, node.value)
    if isinstance(node, astparse.Variable):
        return (cname, node.name)
    if isinstance(node, astparse.UnaryOp):
        return (cname, getattr(node.op, 'name', node.op), _node_signature(node.operand))
    if isinstance(node, astparse.BinOp):
        return (cname, getattr(node.op, 'name', node.op), _node_signature(node.left), _node_signature(node.right))
    return (cname, _line(node))


class _Formatter:
    def __init__(self, parsed, comments, tab_size=4, enable_extensions=True, source=''):
        self._parsed = parsed
        self._comments = comments
        self._ci = 0
        self._tab = ' ' * tab_size
        self._enable_extensions = enable_extensions
        self._source = source
        self._out = []
        self.errors = []

    def _indent(self, level: int) -> str:
        return self._tab * level

    def _error(self, msg: str):
        self.errors.append(msg)

    def _flush_before(self, line: int, level: int):
        while self._ci < len(self._comments) and self._comments[self._ci][0] < line:
            _, _, text = self._comments[self._ci]
            self._out.append(self._indent(level) + text)
            self._ci += 1

    def _flush_rest(self, level: int):
        while self._ci < len(self._comments):
            _, _, text = self._comments[self._ci]
            self._out.append(self._indent(level) + text)
            self._ci += 1

    def _inline_after(self, line: int):
        while self._ci < len(self._comments) and self._comments[self._ci][0] == line:
            _, _, text = self._comments[self._ci]
            self._out[-1] = self._out[-1] + '  ' + text
            self._ci += 1

    def _header(self, node, text: str, level: int):
        self._flush_before(_line(node), level)
        self._out.append(self._indent(level) + text)
        self._inline_after(_line(node))

    def _expr(self, node, parent_op=None, is_right=False):
        if isinstance(node, astparse.BinOp):
            body = self._binop_text(node)
            if parent_op is not None and _need_parens(node, parent_op, is_right):
                return '(' + body + ')'
            return body
        if isinstance(node, astparse.UnaryOp):
            return self._unary_text(node)
        return self._atom_text(node)

    def _binop_text(self, node):
        left = self._expr(node.left, node.op, False)
        right = self._expr(node.right, node.op, True)
        op_text = _BINOP_TEXT[node.op]
        return f'{left} {op_text} {right}'

    def _unary_text(self, node):
        prefix = _UNARY_TEXT[node.op]
        operand = node.operand
        if isinstance(operand, astparse.BinOp):
            operand_text = '(' + self._expr(operand) + ')'
        else:
            operand_text = self._expr(operand)
        if node.op in (TokenType.MINUS, TokenType.PLUS, TokenType.MINUS_MINUS) and isinstance(operand, astparse.UnaryOp) and operand.op in (TokenType.MINUS, TokenType.PLUS, TokenType.MINUS_MINUS):
            return prefix + ' ' + operand_text
        return prefix + operand_text

    def _postfix_base(self, node):
        if isinstance(node, (astparse.BinOp, astparse.UnaryOp, astparse.Deref, astparse.AddrOf)):
            return '(' + self._expr(node) + ')'
        return self._expr(node)

    def _atom_text(self, node):
        if isinstance(node, astparse.Number):
            return node.value
        if isinstance(node, astparse.String):
            return _escape_string(node.value)
        if isinstance(node, astparse.Variable):
            return node.name
        if isinstance(node, astparse.Call):
            callee = self._postfix_base(node.callee)
            args = ', '.join(self._expr(a) for a in node.args)
            return f'{callee}({args})'
        if isinstance(node, astparse.Index):
            obj = self._postfix_base(node.obj)
            return f'{obj}[{self._expr(node.index)}]'
        if isinstance(node, astparse.Attr):
            obj = self._postfix_base(node.obj)
            return f'{obj}.{node.name}'
        if isinstance(node, astparse.Deref):
            return '*' + self._expr(node.operand)
        if isinstance(node, astparse.AddrOf):
            return '&' + self._expr(node.operand)
        if isinstance(node, astparse.SizeOf):
            return f'sizeof({node.type_expr})'
        if isinstance(node, astparse.NewExpr):
            text = f'new {node.type_expr}'
            if node.size is not None:
                text += f'[{self._expr(node.size)}]'
            return text
        if isinstance(node, astparse.Input):
            return 'input()'
        if isinstance(node, astparse.InputStr):
            return 'input_str()'
        if isinstance(node, astparse.Signed67):
            return '67()'
        self._error(f'unsupported expression node {type(node).__name__}')
        return '?'

    def _emit_body(self, body, level: int):
        for stmt in body:
            self._emit_statement(stmt, level)

    def _emit_statement(self, node, level: int):
        if isinstance(node, dict):
            if node.get('type') == 'for':
                self._emit_for(node, level)
            else:
                self._error(f'unsupported node dict {node.get("type")}')
            return
        cname = type(node).__name__
        handler = _STATEMENT_HANDLERS.get(cname)
        if handler is None:
            self._error(f'unsupported statement type {cname}')
            return
        handler(self, node, level)

    def _emit_simple(self, node, text: str, level: int):
        self._flush_before(_line(node), level)
        self._out.append(self._indent(level) + text)
        self._inline_after(_line(node))

    def _emit_vardecl(self, node, level: int):
        text = node.name
        if node.var_type:
            if getattr(node, 'is_const', False):
                text = f'{node.var_type} ({text})'
            else:
                text = f'{node.var_type} {text}'
        if node.init is not None:
            text += f' = {self._expr(node.init)}'
        self._emit_simple(node, text, level)

    def _emit_assign(self, node, level: int):
        target = self._expr(node.target)
        value = node.value
        compound = None
        if isinstance(value, astparse.BinOp):
            left = value.left
            if left is node.target or (isinstance(left, astparse.Variable) and isinstance(node.target, astparse.Variable) and left._token is node.target._token):
                compound = _COMPOUND_TEXT.get(value.op)
        if compound:
            text = f'{target} {compound} {self._expr(value.right)}'
        else:
            text = f'{target} = {self._expr(value)}'
        self._emit_simple(node, text, level)

    def _emit_return(self, node, level: int):
        if node.value is not None:
            text = f'return {self._expr(node.value)}'
        else:
            text = 'return'
        self._emit_simple(node, text, level)

    def _emit_print(self, node, level: int):
        self._emit_simple(node, f'print({self._expr(node.value)})', level)

    def _emit_break(self, node, level: int):
        self._emit_simple(node, 'break', level)

    def _emit_continue(self, node, level: int):
        self._emit_simple(node, 'continue', level)

    def _emit_exprstmt(self, node, level: int):
        self._emit_simple(node, self._expr(node.expr), level)

    def _emit_import(self, node, level: int):
        module = node.module
        if module.startswith('@'):
            text = f'import @{module[1:].replace("/", ".")}'
        elif _IDENT_RE.match(module):
            text = f'import {module}'
        else:
            text = f'import {_escape_string(module)}'
        if getattr(node, 'sdk_path', None):
            text += f' sdk({_escape_string(node.sdk_path)})'
        self._emit_simple(node, text, level)

    def _emit_if(self, node, level: int):
        self._header(node, f'if {self._expr(node.cond)}:', level)
        self._emit_body(node.body, level + 1)
        self._emit_tail(node.orelse, level)

    def _emit_tail(self, orelse, level: int):
        if orelse is None:
            return
        if len(orelse) == 1 and isinstance(orelse[0], astparse.If) and getattr(orelse[0]._token, 'value', None) == 'elif':
            self._emit_elif(orelse[0], level)
        elif orelse:
            self._flush_before(_line(orelse[0]), level)
            self._out.append(self._indent(level) + 'else:')
            self._emit_body(orelse, level + 1)

    def _emit_elif(self, node, level: int):
        self._header(node, f'elif {self._expr(node.cond)}:', level)
        self._emit_body(node.body, level + 1)
        self._emit_tail(node.orelse, level)

    def _emit_while(self, node, level: int):
        self._header(node, f'while {self._expr(node.cond)}:', level)
        self._emit_body(node.body, level + 1)

    def _emit_for(self, node, level: int):
        tok = node.get('_token')
        self._flush_before(_line(node), level)
        self._out.append(self._indent(level) + f'for {node["var"]} in {self._expr(node["iter"])}:')
        if tok is not None:
            self._inline_after(tok.line)
        self._emit_body(node['body'], level + 1)

    def _emit_funcdef(self, node, level: int):
        vis = f'{node.visibility} ' if node.visibility else ''
        generic = f'<{", ".join(node.generic_params)}>' if node.generic_params else ''
        params = ', '.join(f'{n}: {t}' for n, t in node.params.items())
        ret = f' -> {node.rettype}' if node.rettype else ''
        self._header(node, f'{vis}def {node.name}{generic}({params}){ret}:', level)
        self._emit_body(node.body, level + 1)

    def _emit_field(self, node, level: int):
        self._emit_simple(node, f'{node.type_expr} {node.name}', level)

    def _emit_structdef(self, node, level: int):
        generic = f'<{", ".join(node.generic_params)}>' if node.generic_params else ''
        self._header(node, f'struct {node.name}{generic}:', level)
        for f in node.fields:
            self._emit_field(f, level + 1)

    def _emit_classdef(self, node, level: int):
        generic = f'<{", ".join(node.generic_params)}>' if node.generic_params else ''
        base = f'({node.base})' if node.base else ''
        self._header(node, f'class {node.name}{generic}{base}:', level)
        for f in node.fields:
            self._emit_field(f, level + 1)
        for m in node.methods:
            self._emit_funcdef(m, level + 1)

    def _emit_enumdef(self, node, level: int):
        self._header(node, f'enum {node.name}:', level)
        for m in node.members:
            self._flush_before(_line(m), level + 1)
            if m['value'] is not None:
                text = f'{m["name"]} = {self._expr(m["value"])}'
            else:
                text = m['name']
            self._out.append(self._indent(level + 1) + text)
            self._inline_after(_line(m))

    def _emit_typealias(self, node, level: int):
        self._emit_simple(node, f'type {node.name} = {node.target_type}', level)

    def _emit_switch(self, node, level: int):
        self._header(node, f'switch {self._expr(node.value)}:', level)
        for val, body in node.cases:
            if val is None:
                label = 'default:'
                lab_line = _line(body[0]) if body else 0
            else:
                label = f'case {self._expr(val)}:'
                lab_line = _line(val)
            self._flush_before(lab_line, level + 1)
            self._out.append(self._indent(level + 1) + label)
            self._inline_after(lab_line)
            self._emit_body(body, level + 2)

    def _emit_try(self, node, level: int):
        self._header(node, 'try:', level)
        self._emit_body(node.body, level + 1)
        for h in node.handlers:
            if h.type_name:
                label = f'except {h.type_name}:'
            else:
                label = 'except:'
            self._header(h, label, level)
            self._emit_body(h.body, level + 1)

    def _emit_raise(self, node, level: int):
        self._emit_simple(node, f'raise {node.exc_type}({self._expr(node.message)})', level)

    def _emit_inlineasm(self, node, level: int):
        prefix = 'asm volatile' if node.volatile else 'asm'
        groups = []
        for group in (node.outputs, node.inputs, node.clobbers):
            if not group:
                groups.append(None)
                continue
            parts = []
            for item in group:
                if isinstance(item, tuple):
                    constraint, expr = item
                    parts.append(f'{_escape_string(constraint)}({self._expr(expr)})')
                else:
                    parts.append(_escape_string(item))
            groups.append(', '.join(parts))
        text = f'{prefix}({_escape_string(node.template)}'
        last = -1
        for i in range(2, -1, -1):
            if groups[i] is not None:
                last = i
                break
        if last >= 0:
            for i in range(last + 1):
                seg = groups[i] if groups[i] is not None else ''
                text += f' : {seg}' if seg else ' :'
        self._emit_simple(node, text + ')', level)


import re as _re
_IDENT_RE = _re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _need_parens(child, parent_op, is_right: bool) -> bool:
    if not isinstance(child, astparse.BinOp):
        return False
    parent_p = astparse._PREC.get(parent_op, 0)
    child_p = astparse._PREC.get(child.op, 0)
    if is_right:
        if parent_op == TokenType.POW:
            return child_p < parent_p
        return child_p <= parent_p
    if child.op == TokenType.POW:
        return child_p <= parent_p
    return child_p < parent_p


_STATEMENT_HANDLERS = {
    'VarDecl': _Formatter._emit_vardecl,
    'Assign': _Formatter._emit_assign,
    'Return': _Formatter._emit_return,
    'Print': _Formatter._emit_print,
    'Break': _Formatter._emit_break,
    'Continue': _Formatter._emit_continue,
    'ExprStmt': _Formatter._emit_exprstmt,
    'Import': _Formatter._emit_import,
    'If': _Formatter._emit_if,
    'While': _Formatter._emit_while,
    'FuncDef': _Formatter._emit_funcdef,
    'StructDef': _Formatter._emit_structdef,
    'ClassDef': _Formatter._emit_classdef,
    'EnumDef': _Formatter._emit_enumdef,
    'TypeAlias': _Formatter._emit_typealias,
    'Switch': _Formatter._emit_switch,
    'Try': _Formatter._emit_try,
    'Raise': _Formatter._emit_raise,
    'InlineAsm': _Formatter._emit_inlineasm,
}


def format_source(source: str, tab_size: int = 4, enable_extensions: bool = True) -> FormatResult:
    normalized = source.replace('\r\n', '\n')
    comments = _scan_comments(normalized)

    try:
        lex = Lexer(normalized, tab_size=tab_size, enable_extensions=enable_extensions)
        tokens = lex.get_tokens()
        parsed, _ = astparse.parse_file(tokens, enable_extensions=enable_extensions)
    except (LexerError, astparse.ParseError) as e:
        return FormatResult(formatted=normalized, errors=[f'parse error: {e}'], comment_count=len(comments))

    fmt = _Formatter(parsed, comments, tab_size=tab_size, enable_extensions=enable_extensions, source=normalized)

    prev = None
    for i, node in enumerate(parsed):
        if i > 0 and (isinstance(node, _DEF_LIKE) or isinstance(prev, _DEF_LIKE)):
            fmt._out.append('')
        fmt._emit_statement(node, 0)
        prev = node
    fmt._flush_rest(0)

    text = '\n'.join(fmt._out)
    if text:
        text += '\n'

    result = FormatResult(formatted=text, errors=fmt.errors, comment_count=len(comments), parsed=parsed)
    if not fmt.errors:
        re_comment_count = len(_scan_comments(text))
        if re_comment_count != len(comments):
            result.errors.append(f'comment preservation failed: {len(comments)} -> {re_comment_count}')
    return result


def format_file(path: str, write: bool = False, check: bool = False, tab_size: int = 4, enable_extensions: bool = True):
    with open(path, encoding='utf-8') as f:
        source = f.read()
    result = format_source(source, tab_size=tab_size, enable_extensions=enable_extensions)
    if check:
        if result.errors:
            return result, 1
        return result, 0 if result.formatted == source else 1
    if write:
        if result.errors:
            return result, 1
        with open(path, 'w', encoding='utf-8') as f:
            f.write(result.formatted)
        return result, 0
    return result, 0
