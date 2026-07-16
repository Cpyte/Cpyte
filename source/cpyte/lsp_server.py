import logging
import os
import time
import traceback
from urllib.parse import unquote, urlparse

from lsprotocol import types as lsp
from pygls.cli import start_server
from pygls.lsp.server import LanguageServer

from .astparse import parse_file, ParseError, FuncDef, StructDef
from .lexar import Lexer, LexerError
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
def did_change(ls: CpyLanguageServer, params: lsp.DidOpenTextDocumentParams):
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
        prefix = tok.value if tok else ""

        items = []
        seen = set()

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
                sig = ", ".join(f"{n}: {t}" for n, t in node.params.items())
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

        if analyzer:
            for name, sym in analyzer.globals.symbols.items():
                if name.startswith(prefix) and name not in seen:
                    kind = lsp.CompletionItemKind.Function if sym.kind == "function" else lsp.CompletionItemKind.Variable
                    items.append(lsp.CompletionItem(
                        label=name,
                        kind=kind,
                        detail=sym.type or sym.kind,
                    ))
                    seen.add(name)

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
            if not content:
                sym = analyzer.globals.lookup(word) if analyzer else None
                if sym:
                    t = sym.type or sym.kind
                    if sym.kind == "function":
                        content = f"**`{word}`** → `{sym.type if sym.type != 'void' else ''}`"
                    else:
                        content = f"**`{word}`**: `{t}`"
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
            if isinstance(node, FuncDef):
                sig = f"({', '.join(f'{n}: {t}' for n, t in node.params.items())})"
                symbols.append(lsp.SymbolInformation(
                    name=f"{node.name}{sig}",
                    kind=lsp.SymbolKind.Function,
                    location=lsp.Location(
                        uri=uri,
                        range=lsp.Range(start=lsp.Position(line=0, character=0),
                                        end=lsp.Position(line=0, character=0)),
                    ),
                ))
            elif isinstance(node, StructDef):
                symbols.append(lsp.SymbolInformation(
                    name=node.name,
                    kind=lsp.SymbolKind.Struct,
                    location=lsp.Location(
                        uri=uri,
                        range=lsp.Range(start=lsp.Position(line=0, character=0),
                                        end=lsp.Position(line=0, character=0)),
                    ),
                ))
        elapsed = time.time() - t0
        logger.info(f"[symbols] {os.path.basename(filepath)}: {len(symbols)} symbol(s) in {elapsed*1000:.0f}ms")
        return symbols
    except Exception:
        logger.error(f"document_symbols error:\n{traceback.format_exc()}")
        return []


if __name__ == "__main__":
    start_server(server)
