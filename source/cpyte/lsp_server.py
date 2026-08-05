import logging
import os
import time
import traceback
from urllib.parse import unquote, urlparse

from lsprotocol import types as lsp
from pygls.cli import start_server
from pygls.lsp.server import LanguageServer

from .astparse import (
    parse_file, ParseError,
    FuncDef, StructDef, ClassDef, EnumDef, VarDecl, If, While, Switch, Try,
)
from .formatter import format_source
from .lexar import Lexer, LexerError, TokenType
from .semantic_analasis import SemanticAnalyzer

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _uri_to_path(uri: str) -> str:
    result = urlparse(uri)
    path = unquote(result.path)
    if os.name == 'nt' and path.startswith('/'):
        path = path[1:]
    return path

_KEYWORDS = [
    "def", "return", "if", "elif", "else", "while", "for", "break", "continue",
    "public", "private", "static",
    "import", "struct", "new", "sizeof",
    "true", "false", "null",
    "switch", "case", "default",
    "print", "input", "input_str",
]

_TYPES = ["int", "int64", "uint64", "float", "str", "void", "bool"]

_KEYWORD_DESC = {
    "def": "Define a function",
    "return": "Return a value from a function",
    "if": "Execute block if condition is true",
    "elif": "Additional condition if previous if/elif was false",
    "else": "Fallback when all conditions are false",
    "while": "Repeat block while condition is true",
    "for": "Iterate over a range or collection",
    "break": "Exit the innermost loop immediately",
    "continue": "Skip to the next iteration of the loop",
    "public": "Make a symbol accessible outside the module",
    "private": "Restrict symbol to the current module",
    "static": "Declare a class-level member",
    "import": "Bring a module's symbols into scope",
    "struct": "Define a composite data type with named fields",
    "new": "Allocate a new struct instance on the heap",
    "sizeof": "Return the byte size of a type",
    "true": "Boolean literal representing truth",
    "false": "Boolean literal representing falsehood",
    "null": "Null pointer literal",
    "switch": "Select one branch to execute based on a value",
    "case": "A labelled branch inside a switch statement",
    "default": "The fallback branch when no case matches",
    "print": "Write a string representation to stdout",
    "input": "Read a line of text from stdin",
    "input_str": "Read a line of text from stdin and return it as a string",
}

_TYPE_DESC = {
    "int": "Signed 32-bit integer",
    "int64": "Signed 64-bit integer",
    "uint64": "Unsigned 64-bit integer",
    "float": "64-bit floating-point number (IEEE 754)",
    "str": "Heap-allocated UTF-8 string",
    "void": "Absence of a value (used as return type)",
    "bool": "Boolean (true or false)",
}

_SNIPPETS = {
    "def": "def ${1:name}(${2:params}):\n    ${0:body}",
    "if": "if ${1:condition}:\n    ${0:body}",
    "elif": "elif ${1:condition}:\n    ${0:body}",
    "else": "else:\n    ${0:body}",
    "while": "while ${1:condition}:\n    ${0:body}",
    "for": "for ${1:var} in ${2:iter}:\n    ${0:body}",
    "struct": "struct ${1:Name}:\n    ${0:int field}",
    "public def": "public def ${1:name}(${2:params}) -> ${3:type}:\n    ${0:body}",
}


def _analyze(source, filepath=None, workspace_root=None):
    try:
        tokens = []
        parsed = []
        analyzer = None
        error = None
        try:
            tokens = Lexer(source).get_tokens()
        except LexerError as e:
            error = ("lexer", str(e), getattr(e, "token", None))
            return tokens, parsed, analyzer, error
        try:
            parsed, _ = parse_file(tokens)
        except ParseError as e:
            error = ("parser", str(e), e.token)
            return tokens, parsed, analyzer, error
        try:
            analyzer = SemanticAnalyzer(source, filepath=filepath, workspace_root=workspace_root)
            analyzer.analyze(parsed)
        except Exception as e:
            error = ("analyzer", str(e), None)
        return tokens, parsed, analyzer, error
    except Exception as e:
        return [], [], None, ("internal", str(e), None)


def _find_token_at(tokens, line, col):
    for tok in tokens:
        if tok is None:
            continue
        if tok.line - 1 == line:
            tok_start = tok.column - 1
            tok_end = tok_start + len(tok.value or "")
            if tok_start <= col < tok_end:
                return tok
    return None


def _dot_context(tokens, line, col):
    """Return the base identifier of a 'base.<member>' context at the cursor,
    or None if the cursor is not inside a member access."""
    tok = _find_token_at(tokens, line, col)
    if tok is not None and tok.type == TokenType.IDENTIFIER:
        idx = tokens.index(tok)
        if idx >= 2 and tokens[idx - 1] is not None and tokens[idx - 1].type == TokenType.DOT:
            base = tokens[idx - 2]
            if base is not None and base.type == TokenType.IDENTIFIER:
                return base.value
        return None
    before = None
    for t in tokens:
        if t is None:
            continue
        t_line = t.line - 1
        t_end = t.column - 1 + len(t.value or "")
        if t_line < line or (t_line == line and t_end <= col):
            before = t
        elif t_line > line or (t_line == line and t.column - 1 > col):
            break
    if before is not None and before.type == TokenType.DOT:
        idx = tokens.index(before)
        base = tokens[idx - 1] if idx >= 1 else None
        if base is not None and base.type == TokenType.IDENTIFIER:
            return base.value
    return None


def _iter_functions(parsed):
    for node in parsed:
        if isinstance(node, FuncDef):
            yield node
        elif isinstance(node, ClassDef):
            for m in node.methods:
                yield m


def _find_containing_function(parsed, line):
    funcs = list(_iter_functions(parsed))
    if not funcs:
        return None
    matches = []
    for f in funcs:
        start = getattr(getattr(f, '_token', None), 'line', None)
        if start is None:
            continue
        end = start
        for stmt in getattr(f, 'body', None) or []:
            l = getattr(getattr(stmt, '_token', None), 'line', None)
            if l is not None:
                end = max(end, l)
        if start <= line <= end:
            matches.append((start, end, f))
    if matches:
        matches.sort(key=lambda t: -t[0])
        return matches[0][2]
    for f in funcs:
        start = getattr(getattr(f, '_token', None), 'line', None)
        if start is not None and start <= line:
            return f
    return None


def _walk_stmt(stmt, cursor_line, out):
    if isinstance(stmt, VarDecl):
        tok = stmt._token
        if tok is not None and tok.line <= cursor_line:
            out[stmt.name] = stmt.var_type or 'int'
        return
    if isinstance(stmt, If):
        for s in stmt.body:
            _walk_stmt(s, cursor_line, out)
        for s in stmt.orelse or []:
            _walk_stmt(s, cursor_line, out)
        return
    if isinstance(stmt, While):
        for s in stmt.body:
            _walk_stmt(s, cursor_line, out)
        return
    if isinstance(stmt, Switch):
        for _, body in stmt.cases:
            for s in body:
                _walk_stmt(s, cursor_line, out)
        return
    if isinstance(stmt, Try):
        for s in stmt.body:
            _walk_stmt(s, cursor_line, out)
        for h in stmt.handlers:
            for s in h.body:
                _walk_stmt(s, cursor_line, out)
        return
    if isinstance(stmt, dict) and stmt.get('type') == 'for':
        if stmt.get('var'):
            out[stmt['var']] = 'int'
        for s in stmt.get('body') or []:
            _walk_stmt(s, cursor_line, out)
        return


def _collect_locals(func, cursor_line):
    out = {}
    for p, t in (func.params or {}).items():
        out[p] = t
    for stmt in getattr(func, 'body', None) or []:
        _walk_stmt(stmt, cursor_line, out)
    return out


def _const_value_text(sym):
    node = getattr(sym, 'node', None)
    if node is not None and getattr(node, 'init', None) is not None:
        val = getattr(node.init, 'value', None)
        if val is not None:
            return str(val)
    if sym.const_value is not None:
        return str(sym.const_value)
    return '?'


def _member_completions(analyzer, parsed, base, prefix, cursor_line):
    items = []
    if not base or analyzer is None:
        return items
    sym = analyzer.globals.lookup(base)
    if sym is None:
        func = _find_containing_function(parsed, cursor_line)
        if func is not None and base in func.params:
            from types import SimpleNamespace
            sym = SimpleNamespace(kind='param', type=func.params[base], node=None)
        else:
            locs = _collect_locals(func, cursor_line) if func else {}
            if base in locs:
                from types import SimpleNamespace
                sym = SimpleNamespace(kind='variable', type=locs[base], node=None)
    if sym is None:
        return items
    seen = set()
    if sym.kind == 'enum':
        pfx = f'{base}.'
        for name, m in analyzer.globals.symbols.items():
            if name.startswith(pfx):
                label = name[len(pfx):]
                if label.startswith(prefix) and label not in seen:
                    items.append(lsp.CompletionItem(
                        label=label,
                        kind=lsp.CompletionItemKind.EnumMember,
                        detail=f'enum member = {m.const_value}',
                        insert_text=label,
                    ))
                    seen.add(label)
        return items
    type_name = sym.type
    if type_name and type_name.endswith('*'):
        type_name = type_name[:-1]
    node = None
    if type_name:
        type_sym = analyzer.globals.lookup(type_name)
        if type_sym is not None:
            node = type_sym.node
    if node is None:
        node = getattr(sym, 'node', None)
    if node is None:
        return items
    for f in getattr(node, 'fields', None) or []:
        if f.name.startswith(prefix) and f.name not in seen:
            items.append(lsp.CompletionItem(
                label=f.name,
                kind=lsp.CompletionItemKind.Field,
                detail=f'field: {f.type_expr}',
                insert_text=f.name,
            ))
            seen.add(f.name)
    for m in getattr(node, 'methods', None) or []:
        if m.name.startswith(prefix) and m.name not in seen:
            sig = ", ".join(f"{n}: {t}" for n, t in m.params.items() if n != 'this')
            items.append(lsp.CompletionItem(
                label=m.name,
                kind=lsp.CompletionItemKind.Method,
                detail=f"({sig}) -> {m.rettype or 'void'}",
                insert_text=f"{m.name}(",
            ))
            seen.add(m.name)
    return items


class CpyLanguageServer(LanguageServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace_root: str | None = None

    def get_workspace_root(self) -> str | None:
        return self.workspace_root


server = CpyLanguageServer("cpyte-lsp", "0.1",
                           text_document_sync_kind=lsp.TextDocumentSyncKind.Full)


@server.feature(lsp.INITIALIZE)
def initialize(ls: CpyLanguageServer, params: lsp.InitializeParams):
    if params.root_uri:
        ls.workspace_root = _uri_to_path(params.root_uri)
    elif params.root_path:
        ls.workspace_root = params.root_path
    logger.info(f"[initialize] workspace_root={ls.workspace_root}")


def _make_error_diagnostic(error):
    kind, msg, token = error
    if token and hasattr(token, "line"):
        line = token.line - 1
        col = token.column - 1
        rng = lsp.Range(
            start=lsp.Position(line=line, character=col),
            end=lsp.Position(line=line, character=col + len(token.value or "")),
        )
    else:
        rng = lsp.Range(start=lsp.Position(line=0, character=0),
                        end=lsp.Position(line=0, character=0))
    severity = lsp.DiagnosticSeverity.Error
    return lsp.Diagnostic(message=msg, severity=severity, range=rng)


def _do_analyze(ls, uri):
    try:
        doc = ls.workspace.get_text_document(uri)
    except Exception:
        return
    filepath = _uri_to_path(uri)
    workspace_root = _uri_to_path(ls.workspace_root) if ls.workspace_root else None
    t0 = time.time()
    _, _, analyzer, error = _analyze(doc.source, filepath=filepath, workspace_root=workspace_root)
    elapsed = time.time() - t0
    diagnostics = []
    if error:
        diagnostics.append(_make_error_diagnostic(error))
    if analyzer:
        for d in analyzer.reporter.diagnostics:
            if d.token:
                rng = lsp.Range(
                    start=lsp.Position(line=d.token.line - 1, character=d.token.column - 1),
                    end=lsp.Position(line=d.token.line - 1,
                                     character=d.token.column - 1 + len(d.token.value or "")),
                )
            else:
                rng = lsp.Range(start=lsp.Position(line=0, character=0),
                                end=lsp.Position(line=0, character=0))
            diagnostics.append(lsp.Diagnostic(
                message=d.message,
                severity=lsp.DiagnosticSeverity.Error,
                range=rng,
            ))
    logger.info(f"[diag] {os.path.basename(filepath)}: {len(diagnostics)} diag(s) in {elapsed*1000:.0f}ms")
    try:
        ls.text_document_publish_diagnostics(lsp.PublishDiagnosticsParams(
            uri=uri, diagnostics=diagnostics))
    except Exception:
        pass


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: CpyLanguageServer, params: lsp.DidOpenTextDocumentParams):
    try:
        logger.info(f"[didOpen] {os.path.basename(_uri_to_path(params.text_document.uri))}")
        _do_analyze(ls, params.text_document.uri)
    except Exception:
        logger.error(f"did_open error:\n{traceback.format_exc()}")


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: CpyLanguageServer, params: lsp.DidChangeTextDocumentParams):
    try:
        logger.info(f"[didChange] {os.path.basename(_uri_to_path(params.text_document.uri))}")
        _do_analyze(ls, params.text_document.uri)
    except Exception:
        logger.error(f"did_change error:\n{traceback.format_exc()}")


@server.feature(lsp.TEXT_DOCUMENT_COMPLETION,
                lsp.CompletionOptions(trigger_characters=[".", " "]))
def completions(ls: CpyLanguageServer, params: lsp.CompletionParams):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        doc = ls.workspace.get_text_document(uri)

        filepath = _uri_to_path(uri)
        workspace_root = _uri_to_path(ls.workspace_root) if ls.workspace_root else None
        t0 = time.time()
        tokens, parsed, analyzer, _ = _analyze(doc.source, filepath=filepath, workspace_root=workspace_root)
        tok = _find_token_at(tokens, line, col)
        prefix = (tok.value or "") if tok else ""

        items = []
        seen = set()

        base = _dot_context(tokens, line, col)
        if base is not None:
            items = _member_completions(analyzer, parsed, base, prefix, line + 1)
            elapsed = time.time() - t0
            logger.info(f"[completion] {os.path.basename(filepath)} @L{line+1}:{col}: "
                        f"{len(items)} member item(s) for `{base}.` in {elapsed*1000:.0f}ms")
            return lsp.CompletionList(is_incomplete=False, items=items)

        for kw in _KEYWORDS:
            if kw.startswith(prefix):
                snippet = _SNIPPETS.get(kw)
                items.append(lsp.CompletionItem(
                    label=kw,
                    kind=lsp.CompletionItemKind.Keyword,
                    insert_text_format=lsp.InsertTextFormat.Snippet if snippet else lsp.InsertTextFormat.PlainText,
                    insert_text=snippet or kw,
                ))
                seen.add(kw)

        for t in _TYPES:
            if t.startswith(prefix) and t not in seen:
                items.append(lsp.CompletionItem(
                    label=t,
                    kind=lsp.CompletionItemKind.TypeParameter,
                    insert_text=t,
                ))
                seen.add(t)

        for node in parsed:
            if isinstance(node, FuncDef) and node.name.startswith(prefix) and node.name not in seen:
                sig = ", ".join(f"{n}: {t}" for n, t in node.params.items() if n != 'this')
                items.append(lsp.CompletionItem(
                    label=node.name,
                    kind=lsp.CompletionItemKind.Function,
                    detail=f"({sig}) -> {node.rettype or 'void'}",
                    insert_text=f"{node.name}(",
                ))
                seen.add(node.name)
            elif isinstance(node, StructDef) and node.name.startswith(prefix) and node.name not in seen:
                items.append(lsp.CompletionItem(
                    label=node.name,
                    kind=lsp.CompletionItemKind.Class,
                    detail="struct",
                ))
                seen.add(node.name)
            elif isinstance(node, ClassDef) and node.name.startswith(prefix) and node.name not in seen:
                items.append(lsp.CompletionItem(
                    label=node.name,
                    kind=lsp.CompletionItemKind.Class,
                    detail=f"class{(' extends ' + node.base) if node.base else ''}",
                ))
                seen.add(node.name)
            elif isinstance(node, EnumDef) and node.name.startswith(prefix) and node.name not in seen:
                items.append(lsp.CompletionItem(
                    label=node.name,
                    kind=lsp.CompletionItemKind.Enum,
                    detail="enum",
                ))
                seen.add(node.name)

        if analyzer:
            for name, sym in analyzer.globals.symbols.items():
                if '.' in name:
                    continue
                if name.startswith(prefix) and name not in seen:
                    if sym.kind == 'const':
                        kind = lsp.CompletionItemKind.Constant
                        detail = f"const {sym.type} = {_const_value_text(sym)}"
                    elif sym.kind == 'function':
                        kind = lsp.CompletionItemKind.Function
                        detail = f"-> {sym.type or ''}"
                    elif sym.kind == 'enum':
                        kind = lsp.CompletionItemKind.Enum
                        detail = 'enum'
                    elif sym.kind in ('struct', 'class'):
                        kind = lsp.CompletionItemKind.Class
                        detail = sym.kind
                    else:
                        kind = lsp.CompletionItemKind.Variable
                        detail = sym.type or sym.kind
                    items.append(lsp.CompletionItem(
                        label=name,
                        kind=kind,
                        detail=detail,
                    ))
                    seen.add(name)

        func = _find_containing_function(parsed, line + 1)
        if func is not None:
            for p, pt in (func.params or {}).items():
                if p == 'this':
                    continue
                if p.startswith(prefix) and p not in seen:
                    items.append(lsp.CompletionItem(
                        label=p,
                        kind=lsp.CompletionItemKind.Variable,
                        detail=f"parameter: {pt}",
                    ))
                    seen.add(p)
            for lname, ltype in _collect_locals(func, line + 1).items():
                if lname.startswith(prefix) and lname not in seen:
                    items.append(lsp.CompletionItem(
                        label=lname,
                        kind=lsp.CompletionItemKind.Variable,
                        detail=ltype or 'int',
                    ))
                    seen.add(lname)

        elapsed = time.time() - t0
        logger.info(f"[completion] {os.path.basename(filepath)} @L{line+1}:{col}: {len(items)} items in {elapsed*1000:.0f}ms")
        return lsp.CompletionList(is_incomplete=False, items=items)
    except Exception:
        logger.error(f"completions error:\n{traceback.format_exc()}")
        return lsp.CompletionList(is_incomplete=False, items=[])


@server.feature(lsp.TEXT_DOCUMENT_HOVER)
def hover(ls: CpyLanguageServer, params: lsp.HoverParams):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        doc = ls.workspace.get_text_document(uri)

        filepath = _uri_to_path(uri)
        workspace_root = _uri_to_path(ls.workspace_root) if ls.workspace_root else None
        t0 = time.time()
        tokens, parsed, analyzer, _ = _analyze(doc.source, filepath=filepath, workspace_root=workspace_root)
        tok = _find_token_at(tokens, line, col)
        if not tok or not tok.value:
            return None

        word = tok.value
        content = None

        if word in _TYPE_DESC:
            content = f"**`{word}`** — {_TYPE_DESC[word]}"
        elif word in _KEYWORD_DESC:
            content = f"**`{word}`** — {_KEYWORD_DESC[word]}"
        else:
            for node in parsed:
                if isinstance(node, FuncDef) and node.name == word:
                    sig = ", ".join(f"{n}: {t}" for n, t in node.params.items())
                    ret = node.rettype or "void"
                    content = f"**`{node.name}({sig}) → {ret}`**"
                    if node.visibility:
                        content += f"  \n*visibility: `{node.visibility}`*"
                    break
                elif isinstance(node, StructDef) and node.name == word:
                    fields = ", ".join(f"`{f.name}`: `{f.type_expr}`" for f in node.fields)
                    content = f"**`struct {node.name}`**  \n`{{ {fields} }}`"
                    break
                elif isinstance(node, EnumDef) and node.name == word:
                    members = ", ".join(f"`{m['name']}` = `{m.get('_const_value')}`" for m in node.members)
                    content = f"**`enum {node.name}`**  \n{{ {members} }}"
                    break
            if not content:
                sym = analyzer.globals.lookup(word) if analyzer else None
                if sym:
                    if sym.kind == "function":
                        content = f"**`{word}`** → `{sym.type if sym.type != 'void' else ''}`"
                    elif sym.kind == "const":
                        content = f"**`{word}`**: `{sym.type}` = `{_const_value_text(sym)}`  \n*constant*"
                    elif sym.kind == "enum":
                        content = f"**`enum {word}`**"
                    else:
                        content = f"**`{word}`**: `{sym.type or sym.kind}`"
                else:
                    for node in parsed:
                        if isinstance(node, FuncDef) and word in node.params:
                            ptype = node.params[word]
                            content = f"**`{word}`**: `{ptype}`  \n*parameter*"
                            break

        elapsed = time.time() - t0
        logger.info(f"[hover] {os.path.basename(filepath)} @L{line+1}:{col}: {len(content or '')} chars in {elapsed*1000:.0f}ms")
        if content:
            return lsp.Hover(contents=lsp.MarkupContent(
                kind=lsp.MarkupKind.Markdown, value=content))
        return None
    except Exception:
        logger.error(f"hover error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_FORMATTING)
def formatting(ls: CpyLanguageServer, params: lsp.DocumentFormattingParams):
    try:
        uri = params.text_document.uri
        doc = ls.workspace.get_text_document(uri)
        t0 = time.time()
        result = format_source(doc.source, tab_size=params.options.tab_size or 4)
        if result.errors:
            logger.info(f"[format] {os.path.basename(_uri_to_path(uri))}: "
                        f"{len(result.errors)} error(s): {result.errors[0]}")
            return None
        lines = doc.source.splitlines()
        if not lines:
            end = lsp.Position(line=0, character=0)
        else:
            end = lsp.Position(line=len(lines) - 1, character=len(lines[-1]))
        rng = lsp.Range(start=lsp.Position(line=0, character=0), end=end)
        elapsed = time.time() - t0
        logger.info(f"[format] {os.path.basename(_uri_to_path(uri))}: "
                    f"{len(result.formatted)} chars in {elapsed*1000:.0f}ms")
        return [lsp.TextEdit(range=rng, new_text=result.formatted)]
    except Exception:
        logger.error(f"formatting error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
def document_symbols(ls: CpyLanguageServer, params: lsp.DocumentSymbolParams):
    try:
        uri = params.text_document.uri
        doc = ls.workspace.get_text_document(uri)

        filepath = _uri_to_path(uri)
        workspace_root = _uri_to_path(ls.workspace_root) if ls.workspace_root else None
        t0 = time.time()
        _, parsed, _, _ = _analyze(doc.source, filepath=filepath, workspace_root=workspace_root)
        symbols = []
        for node in parsed:
            tok = getattr(node, '_token', None)
            loc = lsp.Location(
                uri=uri,
                range=lsp.Range(
                    start=lsp.Position(line=(tok.line - 1 if tok else 0),
                                       character=(tok.column - 1 if tok else 0)),
                    end=lsp.Position(line=(tok.line - 1 if tok else 0),
                                     character=(tok.column - 1 + len(tok.value or "") if tok else 0)),
                ),
            )
            if isinstance(node, FuncDef):
                sig = f"({', '.join(f'{n}: {t}' for n, t in node.params.items() if n != 'this')})"
                symbols.append(lsp.SymbolInformation(
                    name=f"{node.name}{sig}",
                    kind=lsp.SymbolKind.Function,
                    location=loc,
                ))
            elif isinstance(node, StructDef):
                symbols.append(lsp.SymbolInformation(
                    name=node.name,
                    kind=lsp.SymbolKind.Struct,
                    location=loc,
                ))
            elif isinstance(node, ClassDef):
                symbols.append(lsp.SymbolInformation(
                    name=node.name,
                    kind=lsp.SymbolKind.Class,
                    location=loc,
                ))
                for m in node.methods:
                    mtok = getattr(m, '_token', None)
                    mloc = lsp.Location(
                        uri=uri,
                        range=lsp.Range(
                            start=lsp.Position(line=(mtok.line - 1 if mtok else 0),
                                               character=(mtok.column - 1 if mtok else 0)),
                            end=lsp.Position(line=(mtok.line - 1 if mtok else 0),
                                             character=(mtok.column - 1 + len(mtok.value or "") if mtok else 0)),
                        ),
                    )
                    sig = f"({', '.join(f'{n}: {t}' for n, t in m.params.items() if n != 'this')})"
                    symbols.append(lsp.SymbolInformation(
                        name=f"{node.name}.{m.name}{sig}",
                        kind=lsp.SymbolKind.Method,
                        location=mloc,
                    ))
            elif isinstance(node, EnumDef):
                symbols.append(lsp.SymbolInformation(
                    name=node.name,
                    kind=lsp.SymbolKind.Enum,
                    location=loc,
                ))
            elif isinstance(node, VarDecl) and node.is_const:
                symbols.append(lsp.SymbolInformation(
                    name=f"const {node.name}",
                    kind=lsp.SymbolKind.Constant,
                    location=loc,
                ))
        elapsed = time.time() - t0
        logger.info(f"[symbols] {os.path.basename(filepath)}: {len(symbols)} symbol(s) in {elapsed*1000:.0f}ms")
        return symbols
    except Exception:
        logger.error(f"document_symbols error:\n{traceback.format_exc()}")
        return []


if __name__ == "__main__":
    start_server(server)
