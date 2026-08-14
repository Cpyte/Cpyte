from enum import Enum, auto
from typing import Set


class TokenType(Enum):
    IDENTIFIER = auto()
    KEYWORD = auto()
    NUMBER = auto()
    STRING = auto()
    FSTRING = auto()

    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    SLASH_SLASH = auto()
    PERCENT = auto()
    POW = auto()
    MINUS_MINUS = auto()
    TILDE = auto()
    EQUAL = auto()
    PLUS_EQ = auto()
    MINUS_EQ = auto()
    STAR_EQ = auto()
    SLASH_EQ = auto()
    SLASH_SLASH_EQ = auto()
    COLON_EQ = auto()
    EQ_EQ = auto()
    NOT_EQ = auto()
    LESS = auto()
    GREATER = auto()
    LESS_EQ = auto()
    GREATER_EQ = auto()
    RETURNTYPE = auto()
    AMPERSAND = auto()
    PIPE = auto()
    CARET = auto()
    SHL = auto()
    SHR = auto()
    AND = auto()
    OR = auto()
    NOT = auto()

    LPAREN = auto()
    RPAREN = auto()
    LBRACKET = auto()
    RBRACKET = auto()
    LBRACE = auto()
    RBRACE = auto()
    COMMA = auto()
    COLON = auto()
    DOT = auto()
    AT_SIGN = auto()

    INDENT = auto()
    DEDENT = auto()
    NEWLINE = auto()
    EOF = auto()


# Base keywords - core language keywords that cannot be overridden
_BASE_KEYWORDS = {
    'def', 'class', 'return', 'if', 'else', 'elif',
    'while', 'for', 'in', 'break', 'continue',
    'public', 'private', 'static', 'virtual', 'override',
    'import', 'true', 'false', 'null', 'True', 'False',
    'and', 'or', 'not',
    'print',
    'input', 'input_str', 'input_big',
    'switch', 'case', 'default',
    'new', 'struct', 'sizeof', 'ref',
    'int64', 'uint64',
    'let',
    'try', 'except', 'raise',
    'asm',
    'enum', 'type',
}

# Additional keywords registered by packages
_additional_keywords: Set[str] = set()


def register_keywords(keywords: Set[str]) -> None:
    """Register additional keywords from packages."""
    _additional_keywords.update(keywords)
    # Update legacy KEYWORDS for backward compatibility
    global KEYWORDS
    KEYWORDS = get_keywords()


def unregister_keywords(keywords: Set[str]) -> None:
    """Unregister keywords from packages."""
    _additional_keywords.difference_update(keywords)
    # Update legacy KEYWORDS for backward compatibility
    global KEYWORDS
    KEYWORDS = get_keywords()


def get_keywords() -> Set[str]:
    """Get the complete set of keywords (base + additional)."""
    return _BASE_KEYWORDS | _additional_keywords


# Bruh, dead code.


# Legacy support - keep KEYWORDS as a set for backward compatibility
# This is updated when keywords are registered/unregistered LOl.
KEYWORDS = get_keywords()


class Token:
    __slots__ = ('type', 'value', 'line', 'column', 'raw')

    def __init__(self, type_: TokenType, value: str | None = None, line: int = 0, column: int = 0, raw: bool = False):
        self.type = type_
        self.value = value
        self.line = line
        self.column = column
        self.raw = raw

    def __repr__(self):
        return f'Token({self.type.name}, {self.value!r}, L{self.line}:{self.column})'


class LexerError(Exception):
    def __init__(self, message: str, line: int = 0, column: int = 0):
        self.line = line
        self.column = column
        super().__init__(f'LexerError at {line}:{column}: {message}')


_ESCAPE_MAP = {
    'n': '\n',
    't': '\t',
    'r': '\r',
    '\\': '\\',
    '"': '"',
    "'": "'",
    '0': '\0',
}


def _unescape_char(esc: str, lexer: '_LineLexer') -> str:
    """Decode a single escape sequence after the backslash."""
    if esc == 'x':
        digits = ''
        while len(digits) < 2 and not lexer.at_end() and (lexer.peek().isdigit() or lexer.peek().lower() in 'abcdef'):
            digits += lexer.advance()
        if digits:
            return chr(int(digits, 16))
        return '\\x'
    if esc == 'u':
        digits = ''
        while len(digits) < 4 and not lexer.at_end() and (lexer.peek().isdigit() or lexer.peek().lower() in 'abcdef'):
            digits += lexer.advance()
        if len(digits) == 4:
            return chr(int(digits, 16))
        return '\\u'
    return _ESCAPE_MAP.get(esc, esc)


def _unescape_run(text: str) -> str:
    """Decode escape sequences in a run of f-string literal text."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '\\' and i + 1 < n:
            esc = text[i + 1]
            if esc == 'x':
                digits = ''
                j = i + 2
                while len(digits) < 2 and j < n and (text[j].isdigit() or text[j].lower() in 'abcdef'):
                    digits += text[j]
                    j += 1
                if digits:
                    out.append(chr(int(digits, 16)))
                    i = j
                    continue
                out.append('\\x')
                i += 2
                continue
            if esc == 'u':
                digits = ''
                j = i + 2
                while len(digits) < 4 and j < n and (text[j].isdigit() or text[j].lower() in 'abcdef'):
                    digits += text[j]
                    j += 1
                if len(digits) == 4:
                    out.append(chr(int(digits, 16)))
                    i = j
                    continue
                out.append('\\u')
                i += 2
                continue
            out.append(_ESCAPE_MAP.get(esc, '\\' + esc))
            i += 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


class _LineLexer:
    def __init__(self, line: str, line_number: int):
        self.line = line
        self.line_number = line_number
        self.pos = 0
        self.column = 1

    def peek(self, offset: int = 0) -> str:
        idx = self.pos + offset
        return self.line[idx] if idx < len(self.line) else '\0'

    def advance(self) -> str:
        ch = self.line[self.pos]
        self.pos += 1
        self.column += 1
        return ch

    def at_end(self) -> bool:
        return self.pos >= len(self.line)

    def scan_number(self) -> Token:
        start_col = self.column
        start = self.pos

        # Check for hexadecimal literal (0x or 0X)
        if self.peek() == '0' and self.pos + 1 < len(self.line) and self.peek(1).lower() == 'x':
            self.advance()  # consume '0'
            self.advance()  # consume 'x'
            while self.pos < len(self.line) and (self.peek().isdigit() or self.peek().lower() in 'abcdef'):
                self.advance()
            value = self.line[start:self.pos]
            return Token(TokenType.NUMBER, value, self.line_number, start_col)

        # Regular decimal number
        while self.pos < len(self.line) and self.peek().isdigit():
            self.advance()

        if self.peek() == '.' and self.pos + 1 < len(self.line) and self.peek(1).isdigit():
            self.advance()
            while self.pos < len(self.line) and self.peek().isdigit():
                self.advance()

        value = self.line[start:self.pos]
        return Token(TokenType.NUMBER, value, self.line_number, start_col)

    def scan_string(self, quote: str, raw: bool = False) -> Token:
        start_col = self.column
        start_line = self.line_number
        self.advance()
        chars = []

        while self.pos < len(self.line):
            ch = self.advance()
            if ch == '\\':
                if self.pos >= len(self.line):
                    raise LexerError('Unterminated string escape', start_line, start_col)
                esc = self.advance()
                if raw:
                    chars.append('\\')
                    chars.append(esc)
                else:
                    chars.append(_unescape_char(esc, self))
            elif ch == quote:
                return Token(TokenType.STRING, ''.join(chars), start_line, start_col)
            else:
                chars.append(ch)

        raise LexerError('Unterminated string literal', start_line, start_col)

    def scan_fstring(self, quote: str) -> Token:
        start_col = self.column
        start_line = self.line_number
        self.advance()
        chars = []
        depth = 0
        expr_quote = None  # quote char of a string literal inside {expr}

        while self.pos < len(self.line):
            ch = self.advance()
            if expr_quote is not None:
                chars.append(ch)
                if ch == '\\':
                    if self.pos >= len(self.line):
                        raise LexerError('Unterminated string escape', start_line, start_col)
                    chars.append(self.advance())
                elif ch == expr_quote:
                    expr_quote = None
                continue
            if depth > 0:
                chars.append(ch)
                if ch in ('"', "'"):
                    expr_quote = ch
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                continue
            if ch == '\\':
                if self.pos >= len(self.line):
                    raise LexerError('Unterminated string escape', start_line, start_col)
                chars.append('\\')
                chars.append(self.advance())
            elif ch == quote:
                return Token(TokenType.FSTRING, ''.join(chars), start_line, start_col)
            elif ch == '{':
                chars.append(ch)
                if self.pos < len(self.line) and self.peek() == '{':
                    chars.append(self.advance())
                else:
                    depth += 1
            elif ch == '}':
                chars.append(ch)
                if self.pos < len(self.line) and self.peek() == '}':
                    chars.append(self.advance())
            else:
                chars.append(ch)

        raise LexerError('Unterminated string literal', start_line, start_col)

    def scan_identifier(self) -> Token:
        start_col = self.column
        start = self.pos
        while self.pos < len(self.line) and (self.peek().isalnum() or self.peek() == '_'):
            self.advance()

        value = self.line[start:self.pos]
        if value in get_keywords():
            if value == 'and':
                return Token(TokenType.AND, value, self.line_number, start_col)
            if value == 'or':
                return Token(TokenType.OR, value, self.line_number, start_col)
            if value == 'not':
                return Token(TokenType.NOT, value, self.line_number, start_col)
            # int64 and uint64 are treated as keywords but should be identifiers for type parsing
            if value in ('int64', 'uint64'):
                return Token(TokenType.IDENTIFIER, value, self.line_number, start_col)
            return Token(TokenType.KEYWORD, value, self.line_number, start_col)
        return Token(TokenType.IDENTIFIER, value, self.line_number, start_col)

    def scan_operator_or_punct(self) -> Token:
        ch = self.advance()
        col = self.column - 1

        three_char = ch + self.peek() + self.peek(1) if self.pos + 1 < len(self.line) else ''
        two_char = ch + self.peek() if not self.at_end() else ''

        op_map_3 = {
            '//=': TokenType.SLASH_SLASH_EQ,
        }

        op_map_2 = {
            '**': TokenType.POW,
            '->': TokenType.RETURNTYPE,
            '//': TokenType.SLASH_SLASH,
            '<<': TokenType.SHL,
            '>>': TokenType.SHR,
            '==': TokenType.EQ_EQ,
            '!=': TokenType.NOT_EQ,
            '<=': TokenType.LESS_EQ,
            '>=': TokenType.GREATER_EQ,
            '+=': TokenType.PLUS_EQ,
            '-=': TokenType.MINUS_EQ,
            '*=': TokenType.STAR_EQ,
            '/=': TokenType.SLASH_EQ,
            ':=': TokenType.COLON_EQ,
            '--': TokenType.MINUS_MINUS,
        }

        if three_char in op_map_3:
            self.advance()
            self.advance()
            return Token(op_map_3[three_char], three_char, self.line_number, col)

        if two_char == '--' and self.pos + 1 < len(self.line) and self.peek(1).isdigit():
            return Token(TokenType.MINUS, '-', self.line_number, col)

        if two_char in op_map_2:
            self.advance()
            return Token(op_map_2[two_char], two_char, self.line_number, col)

        single_map = {
            '+': TokenType.PLUS,
            '-': TokenType.MINUS,
            '*': TokenType.STAR,
            '/': TokenType.SLASH,
            '%': TokenType.PERCENT,
            '=': TokenType.EQUAL,
            '<': TokenType.LESS,
            '>': TokenType.GREATER,
            '&': TokenType.AMPERSAND,
            '|': TokenType.PIPE,
            '^': TokenType.CARET,
            '~': TokenType.TILDE,
        }

        if ch in single_map:
            return Token(single_map[ch], ch, self.line_number, col)

        punct_map = {
            '(': TokenType.LPAREN,
            ')': TokenType.RPAREN,
            '[': TokenType.LBRACKET,
            ']': TokenType.RBRACKET,
            '{': TokenType.LBRACE,
            '}': TokenType.RBRACE,
            ',': TokenType.COMMA,
            ':': TokenType.COLON,
            '.': TokenType.DOT,
            '@': TokenType.AT_SIGN,
        }

        if ch in punct_map:
            return Token(punct_map[ch], ch, self.line_number, col)

        raise LexerError(f"Unexpected character: {ch!r}", self.line_number, col)

    def tokenize(self) -> list[Token]:
        tokens = []
        while not self.at_end():
            ch = self.peek()

            if ch in ' \t\r':
                self.advance()
                continue

            if ch == '#':
                break

            if ch == '\\':
                self.advance()
                while not self.at_end() and self.peek() in ' \t\r':
                    self.advance()
                break

            if ch.isdigit():
                tokens.append(self.scan_number())
                continue

            if ch in '"\'':
                tokens.append(self.scan_string(ch))
                continue

            if ch in 'fr' and self.pos + 1 < len(self.line) and self.peek(1) in '"\'':
                prefix = self.advance()
                quote = self.peek()
                if prefix == 'f':
                    tokens.append(self.scan_fstring(quote))
                else:
                    tokens.append(self.scan_string(quote, raw=True))
                continue

            if (
                ch in 'fr'
                and self.pos + 1 < len(self.line)
                and self.peek(1) in 'fr'
                and self.pos + 2 < len(self.line)
                and self.peek(2) in '"\'' 
            ):
                is_f = ch == 'f' or self.peek(1) == 'f'
                self.advance()
                self.advance()
                quote = self.peek()
                if is_f:
                    tok = self.scan_fstring(quote)
                    tok.raw = True
                    tokens.append(tok)
                else:
                    tokens.append(self.scan_string(quote, raw=True))
                continue

            if ch.isalpha() or ch == '_':
                tokens.append(self.scan_identifier())
                continue

            tokens.append(self.scan_operator_or_punct())

        return tokens


class Lexer:
    def __init__(self, source: str, filename: str = '<string>', tab_size: int = 4, enable_extensions: bool = True):
        self.source = source
        self.filename = filename
        self.tab_size = tab_size
        self.tokens: list[Token] = []
        self.enable_extensions = enable_extensions
        self._tokenize()
    
    @staticmethod
    def register_package_keywords(keywords: Set[str]) -> None:
        """Register keywords from a package."""
        register_keywords(keywords)
    
    @staticmethod  
    def unregister_package_keywords(keywords: Set[str]) -> None:
        """Unregister keywords from a package."""
        unregister_keywords(keywords)

    def _validate_indentation(self, raw_lines: list[str]):
        for i, line in enumerate(raw_lines):
            stripped = line.lstrip()
            if not stripped or stripped.startswith('#'):
                continue

            leading = line[:len(line) - len(stripped)]
            has_tabs = '\t' in leading
            has_spaces = ' ' in leading

            if has_tabs and has_spaces:
                ln = i + 1
                tab_col = leading.index('\t') + 1
                raise LexerError(
                    f'Mixing tabs and spaces in indentation (tab at column {tab_col})',
                    ln, tab_col
                )

            if has_tabs and self.tab_size == 0:
                raise LexerError(
                    'Tabs are not allowed for indentation',
                    i + 1, 1
                )

    def _normalize_leading(self, line: str) -> str:
        if self.tab_size == 0 or '\t' not in line:
            return line

        stripped = line.lstrip()
        leading = line[:len(line) - len(stripped)]

        if '\t' not in leading:
            return line

        result = ''
        for ch in leading:
            if ch == '\t':
                result += ' ' * (self.tab_size - (len(result) % self.tab_size))
            else:
                result += ch

        return result + stripped

    def _tokenize(self):
        raw_lines = self.source.split('\n')

        self._validate_indentation(raw_lines)

        norm_lines = [self._normalize_leading(line) for line in raw_lines]

        indent_stack = [0]
        last_non_blank = 0

        bracket_depth = 0
        continuation = False
        for i, norm_line in enumerate(norm_lines):
            line_number = i + 1
            stripped = norm_line.lstrip(' \t')

            if not stripped or stripped.startswith('#'):
                continue

            trimmed = stripped.rstrip()
            ends_with_bs = trimmed.endswith('\\')

            indent = len(norm_line) - len(stripped)

            is_continuation = continuation or bracket_depth > 0
            if not is_continuation:
                while indent < indent_stack[-1]:
                    indent_stack.pop()
                    self.tokens.append(Token(TokenType.DEDENT, None, line_number, 1))

                if indent > indent_stack[-1]:
                    indent_stack.append(indent)
                    self.tokens.append(Token(TokenType.INDENT, None, line_number, 1))

            line_lexer = _LineLexer(norm_line, line_number)
            line_tokens = line_lexer.tokenize()

            for t in line_tokens:
                if t.type in (TokenType.LPAREN, TokenType.LBRACKET, TokenType.LBRACE):
                    bracket_depth += 1
                elif t.type in (TokenType.RPAREN, TokenType.RBRACKET, TokenType.RBRACE):
                    bracket_depth -= 1

            self.tokens.extend(line_tokens)

            if not (ends_with_bs or bracket_depth > 0):
                self.tokens.append(Token(TokenType.NEWLINE, None, line_number, len(norm_line) + 1))
                last_non_blank = line_number

            continuation = ends_with_bs

        while len(indent_stack) > 1:
            indent_stack.pop()
            self.tokens.append(Token(TokenType.DEDENT, None, last_non_blank, 1))

        self.tokens.append(Token(TokenType.EOF, None, last_non_blank, 1))

    def get_tokens(self) -> list[Token]:
        return self.tokens
