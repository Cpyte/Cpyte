from .lexar import Token, TokenType


def parse_f(tree: list[Token]):
    node = {}

    node["type"] = tree[0].value
    node["name"] = tree[1].value

    i = 3
    params = {}

    while i < len(tree) and tree[i].type != TokenType.RPAREN:
        if tree[i].type != TokenType.IDENTIFIER:
            i += 1
            continue
        param_name = tree[i].value
        i += 1
        while i < len(tree) and tree[i].type != TokenType.IDENTIFIER:
            i += 1
        if i < len(tree) and tree[i].type == TokenType.IDENTIFIER:
            param_type = tree[i].value
            i += 1
            params[param_name] = param_type

    node["params"] = params
    node["body"] = tree[i + 1:] if i < len(tree) else []

    return node

def _loc(node):
    token = getattr(node, '_token', None)
    if token:
        return f'L{token.line}:{token.column}'
    return '<unknown>'


class Number:
    __slots__ = ('value', '_token', 'inferred_type')
    def __init__(self, value: str, token=None):
        self.value = value
        self._token = token
        self.inferred_type = None
    def __repr__(self):
        return f'Number({self.value})'

class String:
    __slots__ = ('value', '_token')
    def __init__(self, value: str, token=None):
        self.value = value
        self._token = token
    def __repr__(self):
        return f'String({self.value})'

class Variable:
    __slots__ = ('name', '_token')
    def __init__(self, name: str, token=None):
        self.name = name
        self._token = token
    def __repr__(self):
        return f'Variable({self.name})'

class Call:
    __slots__ = ('callee', 'args', '_token')
    def __init__(self, callee, args: list, token=None):
        self.callee = callee
        self.args = args
        self._token = token
    def __repr__(self):
        return f'Call({self.callee}, {self.args})'

class Index:
    __slots__ = ('obj', 'index', '_token')
    def __init__(self, obj, index, token=None):
        self.obj = obj
        self.index = index
        self._token = token
    def __repr__(self):
        return f'Index({self.obj}, {self.index})'

class Attr:
    __slots__ = ('obj', 'name', '_token')
    def __init__(self, obj, name: str, token=None):
        self.obj = obj
        self.name = name
        self._token = token
    def __repr__(self):
        return f'Attr({self.obj}, {self.name})'

class UnaryOp:
    __slots__ = ('op', 'operand', '_token', 'inferred_type')
    def __init__(self, op: TokenType, operand, token=None):
        self.op = op
        self.operand = operand
        self._token = token
        self.inferred_type = None
    def __repr__(self):
        return f'UnaryOp({self.op.name}, {self.operand})'

class BinOp:
    __slots__ = ('left', 'op', 'right', '_token', 'inferred_type')
    def __init__(self, left, op: TokenType, right, token=None):
        self.left = left
        self.op = op
        self.right = right
        self._token = token
        self.inferred_type = None
    def __repr__(self):
        return f'BinOp({self.left}, {self.op.name}, {self.right})'


_PREC = {
    TokenType.POW: 80,
    TokenType.STAR: 70, TokenType.SLASH: 70, TokenType.SLASH_SLASH: 70, TokenType.PERCENT: 70,
    TokenType.PLUS: 60, TokenType.MINUS: 60,
    TokenType.SHL: 50, TokenType.SHR: 50,
    TokenType.GREATER: 40, TokenType.LESS: 40,
    TokenType.GREATER_EQ: 40, TokenType.LESS_EQ: 40,
    TokenType.EQ_EQ: 40, TokenType.NOT_EQ: 40,
    TokenType.AMPERSAND: 30,
    TokenType.CARET: 20,
    TokenType.PIPE: 10,
    TokenType.AND: 5,
    TokenType.OR: 3,
}

_BINARY_OPS = set(_PREC.keys())


class ParseError(Exception):
    def __init__(self, msg: str, token: Token | None = None):
        self.token = token
        loc = f' at L{token.line}:{token.column}' if token else ''
        super().__init__(f'{msg}{loc}')


def _prec(tok: Token) -> int:
    return _PREC.get(tok.type, 0)


def parse_expression(tokens: list[Token], pos: int = 0):
    return _parse_binary(tokens, pos, 0)


def _parse_binary(tokens: list[Token], pos: int, min_prec: int):
    left, pos = _parse_unary(tokens, pos)

    while pos < len(tokens) and tokens[pos].type in _BINARY_OPS and _prec(tokens[pos]) >= min_prec:
        op = tokens[pos]
        pos += 1
        next_prec = _prec(op) if op.type == TokenType.POW else _prec(op) + 1
        right, pos = _parse_binary(tokens, pos, next_prec)
        left = BinOp(left, op.type, right, token=op)

    return left, pos


def _parse_unary(tokens: list[Token], pos: int):
    if pos >= len(tokens):
        raise ParseError('Unexpected end of expression')

    if tokens[pos].type == TokenType.MINUS:
        op = tokens[pos]
        pos += 1
        operand, pos = _parse_unary(tokens, pos)
        return _parse_postfix(tokens, pos, UnaryOp(op.type, operand, token=op))

    if tokens[pos].type == TokenType.PLUS:
        op = tokens[pos]
        pos += 1
        operand, pos = _parse_unary(tokens, pos)
        return _parse_postfix(tokens, pos, UnaryOp(op.type, operand, token=op))

    if tokens[pos].type in (TokenType.NOT, TokenType.TILDE):
        op = tokens[pos]
        pos += 1
        operand, pos = _parse_unary(tokens, pos)
        return _parse_postfix(tokens, pos, UnaryOp(op.type, operand, token=op))

    if tokens[pos].type == TokenType.POW:
        pos += 1
        operand, pos = _parse_unary(tokens, pos)
        inner = Deref(operand)
        return _parse_postfix(tokens, pos, Deref(inner))
    if tokens[pos].type in (TokenType.STAR, TokenType.AMPERSAND, TokenType.MINUS_MINUS):
        op = tokens[pos]
        pos += 1
        operand, pos = _parse_unary(tokens, pos)
        if op.type == TokenType.STAR:
            return _parse_postfix(tokens, pos, Deref(operand, token=op))
        elif op.type == TokenType.AMPERSAND:
            return _parse_postfix(tokens, pos, AddrOf(operand, token=op))
        return _parse_postfix(tokens, pos, UnaryOp(op.type, operand, token=op))

    node, pos = _parse_atom(tokens, pos)
    return _parse_postfix(tokens, pos, node)


def _parse_atom(tokens: list[Token], pos: int):
    tok = tokens[pos]

    if tok.type == TokenType.NUMBER and tok.value == '67':
        pos += 1
        if pos < len(tokens) and tokens[pos].type == TokenType.LPAREN:
            pos += 1
            if pos >= len(tokens) or tokens[pos].type != TokenType.RPAREN:
                raise ParseError('Expected ")" after 67()', tok)
            return Signed67(token=tok), pos + 1
        pos -= 1
        return Number('67', token=tok), pos + 1

    if tok.type == TokenType.NUMBER:
        return Number(tok.value, token=tok), pos + 1

    if tok.type == TokenType.STRING:
        return String(tok.value, token=tok), pos + 1

    if tok.type == TokenType.IDENTIFIER:
        return Variable(tok.value, token=tok), pos + 1

    if tok.type == TokenType.KEYWORD and tok.value in ('true', 'false', 'True', 'False', 'null'):
        if tok.value in ('true', 'True'):
            val = '1'
        elif tok.value in ('false', 'False'):
            val = '0'
        else:
            val = '0'
        return Number(val, token=tok), pos + 1

    if tok.type == TokenType.KEYWORD and tok.value == 'input':
        pos += 1
        if pos < len(tokens) and tokens[pos].type == TokenType.LPAREN:
            pos += 1
            if pos >= len(tokens) or tokens[pos].type != TokenType.RPAREN:
                raise ParseError('Expected ")" after input()', tok)
            return Input(token=tok), pos + 1
        raise ParseError('Expected "(" after input', tok)

    if tok.type == TokenType.KEYWORD and tok.value == 'input_str':
        pos += 1
        if pos < len(tokens) and tokens[pos].type == TokenType.LPAREN:
            pos += 1
            if pos >= len(tokens) or tokens[pos].type != TokenType.RPAREN:
                raise ParseError('Expected ")" after input_str()', tok)
            return InputStr(token=tok), pos + 1
        raise ParseError('Expected "(" after input_str', tok)

    if tok.type == TokenType.KEYWORD and tok.value == 'new':
        pos += 1
        base_type, pos = parse_type(tokens, pos)
        type_str = _type_to_str(base_type) if isinstance(base_type, tuple) else base_type
        size = None
        if pos < len(tokens) and tokens[pos].type == TokenType.LBRACKET:
            pos += 1
            size, pos = parse_expression(tokens, pos)
            if pos >= len(tokens) or tokens[pos].type != TokenType.RBRACKET:
                raise ParseError('Expected "]"', tok)
            pos += 1
        return NewExpr(type_str, size, token=tok), pos

    if tok.type == TokenType.KEYWORD and tok.value == 'sizeof':
        pos += 1
        if pos >= len(tokens) or tokens[pos].type != TokenType.LPAREN:
            raise ParseError('Expected "("', tok)
        pos += 1
        type_expr, pos = parse_type(tokens, pos)
        if pos >= len(tokens) or tokens[pos].type != TokenType.RPAREN:
            raise ParseError('Expected ")"', tok)
        pos += 1
        type_str = _type_to_str(type_expr) if isinstance(type_expr, tuple) else type_expr
        return SizeOf(type_str, token=tok), pos

    if tok.type == TokenType.LPAREN:
        pos += 1
        node, pos = parse_expression(tokens, pos)
        if pos >= len(tokens) or tokens[pos].type != TokenType.RPAREN:
            raise ParseError('Expected closing parenthesis', tok)
        return node, pos + 1

    raise ParseError(f'Unexpected token in expression: {tok.type.name} "{tok.value}"', tok)


def _parse_call_args(tokens: list[Token], pos: int, callee):
    tok = tokens[pos]
    pos += 1
    args = []
    while pos < len(tokens) and tokens[pos].type != TokenType.RPAREN:
        arg, pos = parse_expression(tokens, pos)
        args.append(arg)
        if pos < len(tokens) and tokens[pos].type == TokenType.COMMA:
            pos += 1
    if pos >= len(tokens) or tokens[pos].type != TokenType.RPAREN:
        raise ParseError('Expected ")" after arguments', tokens[pos] if pos < len(tokens) else None)
    return Call(callee, args, token=tok), pos + 1


def _parse_postfix(tokens: list[Token], pos: int, node):
    while pos < len(tokens) and tokens[pos].type in (TokenType.LPAREN, TokenType.LBRACKET, TokenType.DOT):
        tok = tokens[pos]
        if tok.type == TokenType.LPAREN:
            node, pos = _parse_call_args(tokens, pos, node)
        elif tok.type == TokenType.LBRACKET:
            pos += 1
            index, pos = parse_expression(tokens, pos)
            if pos >= len(tokens) or tokens[pos].type != TokenType.RBRACKET:
                raise ParseError('Expected "]"', tokens[pos] if pos < len(tokens) else None)
            pos += 1
            node = Index(node, index, token=tok)
        elif tok.type == TokenType.DOT:
            pos += 1
            if pos >= len(tokens) or tokens[pos].type != TokenType.IDENTIFIER:
                raise ParseError('Expected attribute name', tokens[pos] if pos < len(tokens) else None)
            name = tokens[pos].value
            pos += 1
            node = Attr(node, name, token=tok)
    return node, pos


def parse_file(tokens: list[Token], pos: int = 0):
    nodes = []
    while pos < len(tokens) and tokens[pos].type not in (TokenType.EOF, TokenType.DEDENT):
        while pos < len(tokens) and tokens[pos].type == TokenType.NEWLINE:
            pos += 1

        if pos >= len(tokens) or tokens[pos].type in (TokenType.EOF, TokenType.DEDENT):
            break

        tok = tokens[pos]
        if tok.type == TokenType.KEYWORD and tok.value == 'def':
            node, pos = parse_def(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value in ('public', 'private', 'static', 'virtual', 'override'):
            node, pos = parse_decorated_def(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value == 'class':
            node, pos = parse_class(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value == 'struct':
            node, pos = parse_struct_def(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value == 'if':
            node, pos = parse_if(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value == 'return':
            node, pos = parse_return(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value == 'while':
            node, pos = parse_while(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value == 'for':
            node, pos = parse_for(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value == 'import':
            node, pos = parse_import(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value == 'print':
            node, pos = parse_print(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value == 'break':
            node, pos = parse_break(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value == 'continue':
            node, pos = parse_continue(tokens, pos)
        elif tok.type == TokenType.KEYWORD and tok.value == 'switch':
            node, pos = parse_switch(tokens, pos)
        elif tok.type == TokenType.IDENTIFIER and pos + 1 < len(tokens):
            if tok.value in _TYPE_NAMES or _looks_like_type(tokens, pos):
                try:
                    save = pos
                    node, pos = parse_var_decl(tokens, pos)
                    if node is not None:
                        nodes.append(node)
                        while pos < len(tokens) and tokens[pos].type == TokenType.NEWLINE:
                            pos += 1
                        continue
                except ParseError:
                    pos = save
            node, pos = parse_expr_stmt(tokens, pos)
        else:
            node, pos = parse_expr_stmt(tokens, pos)

        if node is not None:
            nodes.append(node)

        while pos < len(tokens) and tokens[pos].type == TokenType.NEWLINE:
            pos += 1

    return nodes, pos


def _parse_func_with_visibility(tokens: list[Token], pos: int, visibility: str | None, tok: Token):
    if pos < len(tokens) and tokens[pos].type == TokenType.KEYWORD and tokens[pos].value == 'def':
        pos += 1
    name, pos = _parse_func_name(tokens, pos)
    params, pos = _parse_func_params(tokens, pos)
    rettype, pos = _parse_func_rettype(tokens, pos)
    body, pos = parse_suite(tokens, pos)
    return FuncDef(name, params, body, rettype, visibility, token=tok), pos


def _parse_func_name(tokens: list[Token], pos: int):
    if pos >= len(tokens) or tokens[pos].type != TokenType.IDENTIFIER:
        raise ParseError('Expected function name', tokens[pos] if pos < len(tokens) else None)
    name = tokens[pos].value
    return name, pos + 1


def _parse_func_params(tokens: list[Token], pos: int):
    if pos >= len(tokens) or tokens[pos].type != TokenType.LPAREN:
        raise ParseError('Expected "("', tokens[pos] if pos < len(tokens) else None)
    pos += 1
    params = {}
    while pos < len(tokens) and tokens[pos].type != TokenType.RPAREN:
        if tokens[pos].type != TokenType.IDENTIFIER:
            pos += 1
            continue
        param_name = tokens[pos].value
        pos += 1
        if pos < len(tokens) and tokens[pos].type == TokenType.COLON:
            pos += 1
        if pos < len(tokens):
            param_type, pos = parse_type(tokens, pos)
            param_type_str = _type_to_str(param_type) if isinstance(param_type, tuple) else param_type
            params[param_name] = param_type_str
        if pos < len(tokens) and tokens[pos].type == TokenType.COMMA:
            pos += 1
    if pos >= len(tokens) or tokens[pos].type != TokenType.RPAREN:
        raise ParseError('Expected ")"', tokens[pos] if pos < len(tokens) else None)
    return params, pos + 1


def _parse_func_rettype(tokens: list[Token], pos: int):
    rettype = None
    if pos < len(tokens) and tokens[pos].type == TokenType.RETURNTYPE:
        pos += 1
        if pos < len(tokens):
            rettype_val, pos = parse_type(tokens, pos)
            rettype = _type_to_str(rettype_val) if isinstance(rettype_val, tuple) else rettype_val
    return rettype, pos


def parse_decorated_def(tokens: list[Token], pos: int):
    tok = tokens[pos]
    visibility = None
    while pos < len(tokens) and tokens[pos].type == TokenType.KEYWORD and tokens[pos].value in ('public', 'private', 'static', 'virtual', 'override'):
        visibility = tokens[pos].value
        pos += 1
    return _parse_func_with_visibility(tokens, pos, visibility, tok)


def parse_class(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    if pos >= len(tokens) or tokens[pos].type != TokenType.IDENTIFIER:
        raise ParseError('Expected class name', tokens[pos] if pos < len(tokens) else None)
    name = tokens[pos].value
    pos += 1
    body, pos = parse_suite(tokens, pos)
    return {'type': 'class', 'name': name, 'body': body, '_token': tok}, pos


def parse_struct_def(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    if pos >= len(tokens) or tokens[pos].type != TokenType.IDENTIFIER:
        raise ParseError('Expected struct name', tokens[pos] if pos < len(tokens) else None)
    name = tokens[pos].value
    pos += 1

    generic_params = []
    if pos < len(tokens) and tokens[pos].type == TokenType.LESS:
        pos += 1
        while pos < len(tokens) and tokens[pos].type != TokenType.GREATER:
            if tokens[pos].type == TokenType.IDENTIFIER:
                generic_params.append(tokens[pos].value)
                pos += 1
            if pos < len(tokens) and tokens[pos].type == TokenType.COMMA:
                pos += 1
        if pos >= len(tokens) or tokens[pos].type != TokenType.GREATER:
            raise ParseError('Expected ">"', tok)
        pos += 1

    if pos >= len(tokens) or tokens[pos].type != TokenType.COLON:
        raise ParseError('Expected ":"', tokens[pos] if pos < len(tokens) else None)
    pos += 1
    if pos >= len(tokens) or tokens[pos].type != TokenType.NEWLINE:
        raise ParseError('Expected newline after ":"', tokens[pos] if pos < len(tokens) else None)
    pos += 1
    if pos >= len(tokens) or tokens[pos].type != TokenType.INDENT:
        raise ParseError('Expected indented block', tokens[pos] if pos < len(tokens) else None)
    pos += 1

    fields = []
    while pos < len(tokens) and tokens[pos].type not in (TokenType.DEDENT, TokenType.EOF):
        field_type, pos = parse_type(tokens, pos)
        if pos >= len(tokens) or tokens[pos].type != TokenType.IDENTIFIER:
            raise ParseError('Expected field name', tokens[pos] if pos < len(tokens) else None)
        field_name = tokens[pos].value
        pos += 1
        field_type_str = _type_to_str(field_type) if isinstance(field_type, tuple) else field_type
        fields.append(Field(field_name, field_type_str, token=tok))
        if pos < len(tokens) and tokens[pos].type == TokenType.NEWLINE:
            pos += 1

    if pos < len(tokens) and tokens[pos].type == TokenType.DEDENT:
        pos += 1

    return StructDef(name, fields, generic_params, token=tok), pos


def parse_while(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    cond, pos = parse_expression(tokens, pos)
    body, pos = parse_suite(tokens, pos)
    return While(cond, body, token=tok), pos


def parse_for(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    if pos >= len(tokens) or tokens[pos].type != TokenType.IDENTIFIER:
        raise ParseError('Expected loop variable', tokens[pos] if pos < len(tokens) else None)
    var = tokens[pos].value
    pos += 1
    if pos >= len(tokens) or tokens[pos].type != TokenType.KEYWORD or tokens[pos].value != 'in':
        raise ParseError('Expected "in"', tokens[pos] if pos < len(tokens) else None)
    pos += 1
    iterable, pos = parse_expression(tokens, pos)
    body, pos = parse_suite(tokens, pos)
    return {'type': 'for', 'var': var, 'iter': iterable, 'body': body, '_token': tok}, pos


def parse_import(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    if pos >= len(tokens):
        raise ParseError('Expected module name or quoted header path', tok)
    t = tokens[pos]
    if t.type == TokenType.STRING:
        module = t.value
        pos += 1
    elif t.type == TokenType.IDENTIFIER:
        module = t.value
        pos += 1
    else:
        raise ParseError('Expected module name or quoted header path', t)
    return Import(module, token=tok), pos


def parse_expression_list(tokens: list[Token], pos: int = 0, end_type: TokenType = TokenType.NEWLINE):
    exprs = []
    while pos < len(tokens) and tokens[pos].type != end_type:
        node, pos = parse_expression(tokens, pos)
        exprs.append(node)
        if pos < len(tokens) and tokens[pos].type == TokenType.COMMA:
            pos += 1
        elif pos < len(tokens) and tokens[pos].type not in (end_type, TokenType.RPAREN, TokenType.RBRACKET):
            raise ParseError('Expected comma or end', tokens[pos])
    return exprs, pos

class Assign:
    __slots__ = ('target', 'value', '_token')
    def __init__(self, target, value, token=None):
        self.target = target
        self.value = value
        self._token = token
    def __repr__(self):
        return f'Assign({self.target}, {self.value})'

class Return:
    __slots__ = ('value', '_token')
    def __init__(self, value, token=None):
        self.value = value
        self._token = token
    def __repr__(self):
        return f'Return({self.value})'

class If:
    __slots__ = ('cond', 'body', 'orelse', '_token')
    def __init__(self, cond, body, orelse=None, token=None):
        self.cond = cond
        self.body = body
        self.orelse = orelse
        self._token = token
    def __repr__(self):
        return f'If({self.cond}, {self.body}, {self.orelse})'

class FuncDef:
    __slots__ = ('name', 'params', 'rettype', 'body', 'visibility', '_token')
    def __init__(self, name: str, params: dict, body, rettype: str | None = None, visibility: str | None = None, token=None):
        self.name = name
        self.params = params
        self.rettype = rettype
        self.body = body
        self.visibility = visibility
        self._token = token
    def __repr__(self):
        vis = f'{self.visibility} ' if self.visibility else ''
        return f'FuncDef({vis}{self.name}, {self.params}, ->{self.rettype}, {self.body})'

class Print:
    __slots__ = ('value', '_token')
    def __init__(self, value, token=None):
        self.value = value
        self._token = token
    def __repr__(self):
        return f'Print({self.value})'

class Input:
    __slots__ = ('_token',)
    def __init__(self, token=None):
        self._token = token
    def __repr__(self):
        return 'Input()'


class InputStr:
    __slots__ = ('_token',)
    def __init__(self, token=None):
        self._token = token
    def __repr__(self):
        return 'InputStr()'


class Signed67:
    __slots__ = ('_token',)
    def __init__(self, token=None):
        self._token = token
    def __repr__(self):
        return 'Signed67()'

class While:
    __slots__ = ('cond', 'body', '_token')
    def __init__(self, cond, body, token=None):
        self.cond = cond
        self.body = body
        self._token = token
    def __repr__(self):
        return f'While({self.cond}, {self.body})'

class ExprStmt:
    __slots__ = ('expr', '_token')
    def __init__(self, expr, token=None):
        self.expr = expr
        self._token = token
    def __repr__(self):
        return f'ExprStmt({self.expr})'

class VarDecl:
    __slots__ = ('name', 'var_type', 'init', '_token')
    def __init__(self, name: str, var_type: str | None = None, init=None, token=None):
        self.name = name
        self.var_type = var_type
        self.init = init
        self._token = token
    def __repr__(self):
        return f'VarDecl({self.name}: {self.var_type} = {self.init})'

class Import:
    __slots__ = ('module', 'symbols', 'src_file', '_token', 'sub_ast')
    def __init__(self, module: str, symbols=None, token=None):
        self.module = module
        self.symbols = symbols or []
        self.src_file = None
        self._token = token
        self.sub_ast = None
    def __repr__(self):
        return f'Import({self.module})'


class NewExpr:
    __slots__ = ('type_expr', 'size', '_token')
    def __init__(self, type_expr, size=None, token=None):
        self.type_expr = type_expr
        self.size = size
        self._token = token
    def __repr__(self):
        return f'NewExpr({self.type_expr}, {self.size})'


class Deref:
    __slots__ = ('operand', '_token')
    def __init__(self, operand, token=None):
        self.operand = operand
        self._token = token
    def __repr__(self):
        return f'Deref({self.operand})'


class AddrOf:
    __slots__ = ('operand', '_token')
    def __init__(self, operand, token=None):
        self.operand = operand
        self._token = token
    def __repr__(self):
        return f'AddrOf({self.operand})'


class SizeOf:
    __slots__ = ('type_expr', '_token')
    def __init__(self, type_expr, token=None):
        self.type_expr = type_expr
        self._token = token
    def __repr__(self):
        return f'SizeOf({self.type_expr})'


class StructDef:
    __slots__ = ('name', 'fields', 'generic_params', '_token')
    def __init__(self, name: str, fields: list, generic_params: list | None = None, token=None):
        self.name = name
        self.fields = fields
        self.generic_params = generic_params or []
        self._token = token
    def __repr__(self):
        return f'StructDef({self.name}, {self.fields})'


class Field:
    __slots__ = ('name', 'type_expr', '_token')
    def __init__(self, name: str, type_expr, token=None):
        self.name = name
        self.type_expr = type_expr
        self._token = token
    def __repr__(self):
        return f'Field({self.name}: {self.type_expr})'


def parse_block(tokens: list[Token], pos: int = 0):
    stmts = []
    while pos < len(tokens) and tokens[pos].type not in (TokenType.DEDENT, TokenType.EOF):
        stmt, pos = parse_statement(tokens, pos)
        if stmt is not None:
            stmts.append(stmt)
        while pos < len(tokens) and tokens[pos].type == TokenType.NEWLINE:
            pos += 1
    if pos < len(tokens) and tokens[pos].type == TokenType.DEDENT:
        pos += 1
    return stmts, pos


def parse_suite(tokens: list[Token], pos: int):
    if pos >= len(tokens) or tokens[pos].type != TokenType.COLON:
        raise ParseError('Expected ":"', tokens[pos] if pos < len(tokens) else None)
    pos += 1

    if pos >= len(tokens) or tokens[pos].type != TokenType.NEWLINE:
        raise ParseError('Expected newline after ":"', tokens[pos] if pos < len(tokens) else None)
    pos += 1

    if pos >= len(tokens) or tokens[pos].type != TokenType.INDENT:
        raise ParseError('Expected indented block', tokens[pos] if pos < len(tokens) else None)
    pos += 1

    body, pos = parse_block(tokens, pos)
    return body, pos


_TYPE_NAMES = {'int', 'int64', 'uint64', 'float', 'double', 'bool', 'str', 'char', 'void', 'auto', 'string', 'unsigned', 'long', 'short', 'signed', 'size_t'}


def _looks_like_type(tokens, pos):
    if pos >= len(tokens) or tokens[pos].type != TokenType.IDENTIFIER:
        return False
    i = pos
    while i < len(tokens):
        t = tokens[i]
        if t.type == TokenType.IDENTIFIER:
            i += 1
            continue
        if t.type in (TokenType.STAR, TokenType.POW, TokenType.AMPERSAND, TokenType.LBRACKET):
            i += 1
            if t.type == TokenType.LBRACKET:
                if i < len(tokens) and tokens[i].type in (TokenType.RBRACKET, TokenType.NUMBER):
                    i += 1 if tokens[i].type == TokenType.RBRACKET else 2
                    continue
                return False
            continue
        if t.type == TokenType.LESS:
            depth = 1
            i += 1
            while i < len(tokens) and depth > 0:
                if tokens[i].type == TokenType.LESS:
                    depth += 1
                elif tokens[i].type == TokenType.GREATER:
                    depth -= 1
                i += 1
            continue
        break
    return i > pos + 1


def _type_to_str(t):
    if isinstance(t, tuple):
        name, params = t
        return f'{name}<{", ".join(_type_to_str(p) if isinstance(p, tuple) else p for p in params)}>'
    return t


def parse_type(tokens: list[Token], pos: int):
    if pos >= len(tokens):
        raise ParseError('Expected type', None)

    tok = tokens[pos]
    if tok.type == TokenType.IDENTIFIER:
        base = tok.value
        pos += 1
    else:
        raise ParseError(f'Expected type name, got {tok.value}', tok)

    while pos < len(tokens):
        t = tokens[pos]
        if t.type == TokenType.LBRACKET:
            if pos + 1 < len(tokens) and tokens[pos + 1].type == TokenType.RBRACKET:
                pos += 2
                base = base + '[]'
                continue
            break
        elif t.type == TokenType.STAR:
            base = base + '*'
            pos += 1
        elif t.type == TokenType.POW:
            base = base + '**'
            pos += 1
        elif t.type == TokenType.AMPERSAND:
            base = base + '&'
            pos += 1
        elif t.type == TokenType.LESS and isinstance(base, str):
            pos += 1
            params = []
            first_param, pos = parse_type(tokens, pos)
            params.append(first_param)
            while pos < len(tokens) and tokens[pos].type == TokenType.COMMA:
                pos += 1
                param_type, pos = parse_type(tokens, pos)
                params.append(param_type)
            if pos >= len(tokens) or tokens[pos].type != TokenType.GREATER:
                raise ParseError('Expected ">" to close generic type', t)
            pos += 1
            base = (base, tuple(params))
            break
        else:
            break
    return base, pos


def parse_var_decl(tokens: list[Token], pos: int):
    tok = tokens[pos]
    var_type, pos = parse_type(tokens, pos)
    if pos >= len(tokens) or tokens[pos].type != TokenType.IDENTIFIER:
        raise ParseError('Expected variable name after type', tok)
    name = tokens[pos].value
    pos += 1

    init = None
    if pos < len(tokens) and tokens[pos].type == TokenType.EQUAL:
        pos += 1
        init, pos = parse_expression(tokens, pos)

    _expect_newline(tokens, pos, tok)
    var_type_str = _type_to_str(var_type) if isinstance(var_type, tuple) else var_type
    return VarDecl(name, var_type_str, init, token=tok), pos


def parse_statement(tokens: list[Token], pos: int):
    if pos >= len(tokens):
        return None, pos

    tok = tokens[pos]

    if tok.type == TokenType.KEYWORD and tok.value == 'return':
        return parse_return(tokens, pos)

    if tok.type == TokenType.KEYWORD and tok.value == 'if':
        return parse_if(tokens, pos)

    if tok.type == TokenType.KEYWORD and tok.value == 'def':
        return parse_def(tokens, pos)

    if tok.type == TokenType.KEYWORD and tok.value == 'print':
        return parse_print(tokens, pos)

    if tok.type == TokenType.KEYWORD and tok.value == 'break':
        return parse_break(tokens, pos)

    if tok.type == TokenType.KEYWORD and tok.value == 'continue':
        return parse_continue(tokens, pos)

    if tok.type == TokenType.KEYWORD and tok.value == 'switch':
        return parse_switch(tokens, pos)

    if tok.type == TokenType.KEYWORD and tok.value in ('while', 'for', 'class', 'struct', 'import'):
        handler = {'while': parse_while, 'for': parse_for, 'class': parse_class, 'struct': parse_struct_def, 'import': parse_import}[tok.value]
        return handler(tokens, pos)

    if tok.type == TokenType.IDENTIFIER:
        if tok.value in _TYPE_NAMES or _looks_like_type(tokens, pos):
            try:
                return parse_var_decl(tokens, pos)
            except ParseError:
                pass

    return parse_expr_stmt(tokens, pos)


def _expect_newline(tokens: list[Token], pos: int, tok: Token):
    if pos < len(tokens) and tokens[pos].type not in (TokenType.NEWLINE, TokenType.DEDENT, TokenType.EOF):
        raise ParseError('Expected newline after statement', tok)


def parse_return(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    if pos < len(tokens) and tokens[pos].type not in (TokenType.NEWLINE, TokenType.DEDENT, TokenType.EOF):
        value, pos = parse_expression(tokens, pos)
    else:
        value = None
    _expect_newline(tokens, pos, tok)
    return Return(value, token=tok), pos


def parse_print(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    value, pos = parse_expression(tokens, pos)
    _expect_newline(tokens, pos, tok)
    return Print(value, token=tok), pos


def parse_if(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    cond, pos = parse_expression(tokens, pos)
    body, pos = parse_suite(tokens, pos)

    orelse = None
    while pos < len(tokens) and tokens[pos].type == TokenType.NEWLINE:
        pos += 1

    if pos < len(tokens) and tokens[pos].type == TokenType.KEYWORD:
        if tokens[pos].value == 'else':
            pos += 1
            if pos < len(tokens) and tokens[pos].type == TokenType.COLON:
                orelse_body, pos = parse_suite(tokens, pos)
                orelse = orelse_body
            else:
                body2, pos = parse_suite(tokens, pos)
                orelse = body2
        elif tokens[pos].value == 'elif':
            elif_node, pos = parse_if(tokens, pos)
            orelse = [elif_node]

    return If(cond, body, orelse, token=tok), pos


class Break:
    __slots__ = ('_token',)
    def __init__(self, token=None):
        self._token = token
    def __repr__(self):
        return 'Break()'


class Continue:
    __slots__ = ('_token',)
    def __init__(self, token=None):
        self._token = token
    def __repr__(self):
        return 'Continue()'


def parse_break(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    _expect_newline(tokens, pos, tok)
    return Break(token=tok), pos


def parse_continue(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    _expect_newline(tokens, pos, tok)
    return Continue(token=tok), pos


class Switch:
    def __init__(self, value, cases, token=None):
        self.value = value
        self.cases = cases
        self._token = token
    def __repr__(self):
        return f'Switch({self.value}, {self.cases})'
    def __repr__(self):
        return f'Switch({self.value}, {self.cases})'


def parse_switch(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    value, pos = parse_expression(tokens, pos)

    if pos >= len(tokens) or tokens[pos].type != TokenType.COLON:
        raise ParseError('Expected ":" after switch expression', tokens[pos] if pos < len(tokens) else None)
    pos += 1
    if pos >= len(tokens) or tokens[pos].type != TokenType.NEWLINE:
        raise ParseError('Expected newline after ":"', tokens[pos] if pos < len(tokens) else None)
    pos += 1
    if pos >= len(tokens) or tokens[pos].type != TokenType.INDENT:
        raise ParseError('Expected indented block', tokens[pos] if pos < len(tokens) else None)
    pos += 1

    cases = []
    while pos < len(tokens) and tokens[pos].type not in (TokenType.DEDENT, TokenType.EOF):
        if tokens[pos].type == TokenType.KEYWORD and tokens[pos].value == 'case':
            tok2 = tokens[pos]
            pos += 1
            case_val, pos = parse_expression(tokens, pos)
            if pos >= len(tokens) or tokens[pos].type != TokenType.COLON:
                raise ParseError('Expected ":" after case value', tokens[pos] if pos < len(tokens) else None)
            pos += 1
            case_body = _parse_case_body(tokens, pos)
            pos = case_body[1]
            cases.append((case_val, case_body[0]))
        elif tokens[pos].type == TokenType.KEYWORD and tokens[pos].value == 'default':
            pos += 1
            if pos >= len(tokens) or tokens[pos].type != TokenType.COLON:
                raise ParseError('Expected ":" after default', tokens[pos] if pos < len(tokens) else None)
            pos += 1
            case_body = _parse_case_body(tokens, pos)
            pos = case_body[1]
            cases.append((None, case_body[0]))
        else:
            raise ParseError('Expected case or default in switch block', tokens[pos])
        while pos < len(tokens) and tokens[pos].type == TokenType.NEWLINE:
            pos += 1

    if pos < len(tokens) and tokens[pos].type == TokenType.DEDENT:
        pos += 1

    return Switch(value, cases, token=tok), pos


def _parse_case_body(tokens: list[Token], pos: int):
    stmts = []
    if pos < len(tokens) and tokens[pos].type == TokenType.NEWLINE:
        pos += 1
        if pos < len(tokens) and tokens[pos].type == TokenType.INDENT:
            pos += 1
            stmts, pos = parse_block(tokens, pos)
            return stmts, pos
    stmt, pos = parse_statement(tokens, pos)
    if stmt is not None:
        stmts = [stmt]
    return stmts, pos


def parse_generic_params(tokens: list[Token], pos: int):
    if pos >= len(tokens) or tokens[pos].type != TokenType.LESS:
        return [], pos
    pos += 1
    params = []
    while pos < len(tokens) and tokens[pos].type != TokenType.GREATER:
        if tokens[pos].type == TokenType.IDENTIFIER:
            params.append(tokens[pos].value)
            pos += 1
        if pos < len(tokens) and tokens[pos].type == TokenType.COMMA:
            pos += 1
    if pos >= len(tokens) or tokens[pos].type != TokenType.GREATER:
        raise ParseError('Expected ">"', tokens[pos] if pos < len(tokens) else None)
    return params, pos + 1


def parse_def(tokens: list[Token], pos: int):
    tok = tokens[pos]
    pos += 1
    name, pos = _parse_func_name(tokens, pos)
    generic_params, pos = parse_generic_params(tokens, pos)
    params, pos = _parse_func_params(tokens, pos)
    rettype, pos = _parse_func_rettype(tokens, pos)
    body, pos = parse_suite(tokens, pos)
    return FuncDef(name, params, body, rettype, token=tok), pos


def _is_assignable(expr):
    return isinstance(expr, (Variable, Attr, Index, Deref))


def parse_expr_stmt(tokens: list[Token], pos: int):
    tok = tokens[pos]
    expr, pos = parse_expression(tokens, pos)

    if pos < len(tokens) and tokens[pos].type == TokenType.EQUAL:
        if not _is_assignable(expr):
            raise ParseError('Invalid assignment target', tokens[pos])
        target = expr
        pos += 1
        value, pos = parse_expression(tokens, pos)
        _expect_newline(tokens, pos, tok)
        return Assign(target, value, token=tok), pos

    compound_assign_ops = {
        TokenType.PLUS_EQ: '+=',
        TokenType.MINUS_EQ: '-=',
        TokenType.STAR_EQ: '*=',
        TokenType.SLASH_EQ: '/=',
        TokenType.SLASH_SLASH_EQ: '//=',
    }
    if pos < len(tokens) and tokens[pos].type in compound_assign_ops:
        if not _is_assignable(expr):
            raise ParseError('Invalid assignment target', tokens[pos])
        target = expr
        binop_type = tokens[pos].type
        pos += 1
        value, pos = parse_expression(tokens, pos)
        _expect_newline(tokens, pos, tok)
        binop_op = getattr(TokenType, binop_type.name.replace('_EQ', ''), TokenType.PLUS)
        return Assign(target, BinOp(Variable(target.name, token=expr._token) if isinstance(target, Variable) else target, binop_op, value, token=tok), token=tok), pos

    _expect_newline(tokens, pos, tok)
    return ExprStmt(expr, token=tok), pos


