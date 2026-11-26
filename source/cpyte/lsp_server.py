import asyncio
import logging
import os
import pathlib
import re
import time
import traceback
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

from lsprotocol import types as lsp
from pygls.cli import start_server
from pygls.lsp.server import LanguageServer

from . import scorpion_abi
from .astparse import (
    Call,
    ClassDef,
    EnumDef,
    FuncDef,
    If,
    Import,
    Number,
    ParseError,
    String,
    StructDef,
    Switch,
    Try,
    VarDecl,
    Variable,
    While,
    parse_file,
)
from .clib import _BUILTIN_LIB_HEADERS
from .formatter import format_source
from .lexar import Lexer, LexerError, TokenType, get_keywords
from .package_manifest import cpm_module_roots, iter_cpm_version_dirs
from .semantic_analasis import SemanticAnalyzer

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _uri_to_path(uri: str) -> str:
    result = urlparse(uri)
    path = unquote(result.path)
    if os.name == "nt" and path.startswith("/"):
        path = path[1:]
    return path


def _fmt_params(params, const_params=None):
    """Render a signature parameter list, marking constant-view params."""
    cvs = set(const_params or ())
    return ", ".join(
        f"({n}): {t}" if n in cvs else f"{n}: {t}" for n, t in params.items()
    )


def _decorator_text(dec):
    """Render a parsed decorator expression for hover text."""
    if isinstance(dec, str):
        return dec
    if isinstance(dec, Call):
        name = _decorator_text(dec.callee)
        args = ", ".join(_decorator_text(arg) for arg in dec.args)
        return f"{name}({args})"
    if isinstance(dec, Number):
        return str(dec.value)
    if isinstance(dec, String):
        return repr(dec.value)
    name = getattr(dec, "name", None)
    if name:
        return str(name)
    value = getattr(dec, "value", None)
    if isinstance(value, str):
        return repr(value)
    if value is not None:
        return str(value)
    return str(dec)


_KEYWORDS = [
    "def",
    "return",
    "if",
    "elif",
    "else",
    "while",
    "for",
    "in",
    "break",
    "continue",
    "public",
    "private",
    "static",
    "import",
    "struct",
    "class",
    "enum",
    "new",
    "sizeof",
    "type",
    "true",
    "false",
    "null",
    "True",
    "False",
    "and",
    "or",
    "not",
    "switch",
    "case",
    "default",
    "try",
    "except",
    "raise",
    "assert",
    "let",
    "virtual",
    "override",
    "sealed",
    "dataclass",
    "property",
    "get",
    "set",
    "super",
    "isinstance",
    "as",
    "auto",
    "ccode",
    "llvm",
    "asm",
    "unsafe",
    "ref",
    "defer",
    "borrow",
    "move",
    "mut",
    "print",
    "input",
    "input_str",
    "input_big",
    "free",
    "del",
    "has",
    "get_attr",
    "runpy",
    "hotswap",
    "redirect",
]

_TYPES = [
    "int",
    "int8",
    "int16",
    "int32",
    "int64",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "float",
    "double",
    "str",
    "char",
    "void",
    "bool",
    "big",
    "ubig",
    "size_t",
    "dynamic",
    "auto",
]

_KEYWORD_DESC = {
    "def": "Define a function",
    "return": "Return a value from a function",
    "if": "Execute block if condition is true",
    "elif": "Additional condition if previous if/elif was false",
    "else": "Fallback when all conditions are false",
    "while": "Repeat block while condition is true",
    "for": "Iterate over a range or collection",
    "in": "Used in for-loops to iterate over a collection",
    "break": "Exit the innermost loop immediately",
    "continue": "Skip to the next iteration of the loop",
    "public": "Make a symbol accessible outside the module",
    "private": "Restrict symbol to the current module",
    "static": "Declare a class-level member",
    "import": "Bring a module's symbols into scope",
    "struct": "Define a composite data type with named fields",
    "class": "Define a class with fields and methods",
    "enum": "Define an enumeration of named integer constants",
    "new": "Allocate a new struct instance on the heap",
    "sizeof": "Return the byte size of a type",
    "type": "Define a type alias",
    "true": "Boolean literal representing truth",
    "false": "Boolean literal representing falsehood",
    "null": "Null pointer literal",
    "switch": "Select one branch to execute based on a value",
    "case": "A labelled branch inside a switch statement",
    "default": "The fallback branch when no case matches",
    "try": "Begin an exception handling block",
    "except": "Catch an exception thrown inside a try block",
    "raise": "Throw an exception with a message",
    "assert": "Verify a condition is true, trap with message if not",
    "let": "Declare a variable (expression-statement form)",
    "virtual": "Declare an overridable method",
    "override": "Override a parent class method",
    "sealed": "Declare a monomorphic class (static dispatch, no vtable)",
    "dataclass": "Auto-generate __init__/__eq__/__str__ for a class",
    "property": "Define a property with get/set accessors",
    "get": "Property getter accessor",
    "set": "Property setter accessor",
    "super": "Call a method from the parent class",
    "isinstance": "Check if an object is an instance of a class (RTTI)",
    "as": "Safe runtime downcast operator",
    "auto": "Type inference keyword",
    "ccode": "Embed raw C code block",
    "llvm": "Embed raw LLVM IR block",
    "asm": "Inline assembly block",
    "unsafe": "Mark code as unsafe (no bounds checking)",
    "print": "Write a string representation to stdout",
    "input": "Read a line of text from stdin",
    "input_str": "Read a line of text from stdin and return it as a string",
    "input_big": "Read a line of text from stdin and return it as an arbitrary-precision big integer",
    "free": "Deallocate heap memory",
    "del": "Delete/unbind a variable or clear a slot",
    "has": "Check if a field/variable exists",
    "get_attr": "Get attribute by name at runtime",
    "runpy": "Dynamic class runtime",
    "hotswap": "Hot swap class/table methods",
    "redirect": "Redirect class to another class/table",
    "True": "Boolean literal representing truth",
    "False": "Boolean literal representing falsehood",
    "and": "Logical AND (both sides must be true)",
    "or": "Logical OR (at least one side must be true)",
    "not": "Logical NOT (inverts a boolean)",
    "ref": "Take a reference to a value",
    "defer": "Schedule an action to run when the enclosing scope exits",
    "borrow": "Borrow a value (non-owning access)",
    "move": "Move ownership of a value",
    "mut": "Mark a binding as mutable",
}

_TYPE_DESC = {
    "int": "Signed 32-bit integer",
    "int64": "Signed 64-bit integer",
    "uint64": "Unsigned 64-bit integer",
    "float": "64-bit floating-point number (IEEE 754)",
    "double": "64-bit floating-point number (alias for float)",
    "str": "Heap-allocated UTF-8 string (native char*)",
    "char": "Single byte character (8-bit)",
    "void": "Absence of a value (used as return type)",
    "bool": "Boolean (true or false)",
    "big": "Arbitrary-precision integer (unlimited size)",
    "ubig": "Unsigned arbitrary-precision integer",
    "size_t": "Unsigned platform-dependent size type",
    "dynamic": "Dynamically-typed value (resolved at runtime)",
    "auto": "Type inferred from initializer",
}

_BUILTIN_FUNCS = {
    "print": "print(value: any) -> void",
    "input": "input() -> int",
    "input_str": "input_str() -> str",
    "input_big": "input_big() -> big",
    "str": "str(value: any) -> str",
    "int": "int(value: any) -> int",
    "float": "float(value: any) -> float",
    "double": "double(value: any) -> float",
    "range": "range(stop: int) -> int64[]",
    "str_split": "str_split(s: str, sep: str) -> dynamic[]",
    "append": "append(arr: T[], x: T) -> T[]",
    "len": "len(arr: T[]) -> int64",
    "sizeof": "sizeof(type: T) -> int",
    "new": "new T(...) -> T*",
    "free": "free(ptr: any*) -> void",
    "code": "code() -> dynamic",
    "has": "has(obj: any, name?: str) -> bool",
    "get_attr": "get_attr(obj: any, name: str) -> any",
    "isinstance": "isinstance(obj: any, cls: type) -> bool",
    "gc_collect": "gc_collect() -> void",
}

_BUILTIN_FUNC_PARAMS = {
    "print": {"value": "any"},
    "input": {},
    "input_str": {},
    "input_big": {},
    "str": {"value": "any"},
    "int": {"value": "any"},
    "float": {"value": "any"},
    "double": {"value": "any"},
    "range": {"stop": "int"},
    "str_split": {"s": "str", "sep": "str"},
    "append": {"arr": "T[]", "x": "T"},
    "len": {"arr": "T[]"},
    "sizeof": {"type": "T"},
    "new": {},
    "free": {"ptr": "any*"},
    "code": {},
    "has": {"obj": "any", "name?": "str"},
    "get_attr": {"obj": "any", "name": "str"},
    "isinstance": {"obj": "any", "cls": "type"},
    "gc_collect": {},
}

_SNIPPETS = {
    "def": "def ${1:name}(${2:params}):\n    ${0:body}",
    "if": "if ${1:condition}:\n    ${0:body}",
    "elif": "elif ${1:condition}:\n    ${0:body}",
    "else": "else:\n    ${0:body}",
    "while": "while ${1:condition}:\n    ${0:body}",
    "for": "for ${1:var} in ${2:iter}:\n    ${0:body}",
    "struct": "struct ${1:Name}:\n    ${0:int field}",
    "class": "class ${1:Name}:\n    ${0:field}",
    "enum": "enum ${1:Name}:\n    ${0:MEMBER}",
    "try": "try:\n    ${0:body}\nexcept:\n    pass",
    "switch": "switch ${1:value}:\n    case ${2:pattern}:\n        ${0:body}",
    "public def": "public def ${1:name}(${2:params}) -> ${3:type}:\n    ${0:body}",
}


def _load_manifest_keywords(workspace_root):
    """Register package manifest keywords so the lexer recognises extension
    keywords/operators before lexing — mirrors the compiler CLI path in
    mainpie. Safe to call repeatedly (global registry de-dupes by package).

    Global ``~/.cpm`` packages must be loaded even when the editor has no
    workspace folder open, so there is no workspace guard here.
    """
    try:
        from .mainpie import _load_package_manifests_from_source

        _load_package_manifests_from_source(workspace_root)
    except Exception:
        pass


def _analyze(source, filepath=None, workspace_root=None):
    try:
        tokens = []
        parsed = []
        analyzer = None
        error = None
        try:
            _load_manifest_keywords(workspace_root)
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
            analyzer = SemanticAnalyzer(
                source,
                filepath=filepath,
                workspace_root=workspace_root,
                scorpion=scorpion_abi.detect(source)[1],
            )
            analyzer.analyze(parsed)
        except Exception as e:
            error = ("analyzer", str(e), None)
        return tokens, parsed, analyzer, error
    except Exception as e:
        return [], [], None, ("internal", str(e), None)


_LEVEL_SEVERITY = {
    "error": lsp.DiagnosticSeverity.Error,
    "strict-error": lsp.DiagnosticSeverity.Error,
    "strict-warning": lsp.DiagnosticSeverity.Warning,
    "warning": lsp.DiagnosticSeverity.Warning,
}


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
        rng = lsp.Range(
            start=lsp.Position(line=0, character=0),
            end=lsp.Position(line=0, character=0),
        )
    severity = lsp.DiagnosticSeverity.Error
    return lsp.Diagnostic(message=msg, severity=severity, range=rng, source="cpyte")


def _reporter_diagnostics(analyzer):
    diagnostics = []
    for d in analyzer.reporter.diagnostics:
        if d.token:
            span = d.span if d.span > 0 else len(d.token.value or "")
            span = max(1, span)
            rng = lsp.Range(
                start=lsp.Position(line=d.token.line - 1, character=d.token.column - 1),
                end=lsp.Position(
                    line=d.token.line - 1,
                    character=d.token.column - 1 + span,
                ),
            )
        else:
            rng = lsp.Range(
                start=lsp.Position(line=0, character=0),
                end=lsp.Position(line=0, character=0),
            )
        message = d.message
        if d.note:
            message = f"{message}\n\nHelp: {d.note}"
        diagnostics.append(
            lsp.Diagnostic(
                message=message,
                severity=_LEVEL_SEVERITY.get(d.level, lsp.DiagnosticSeverity.Error),
                range=rng,
                code=d.code,
                source="cpyte",
            )
        )
    return diagnostics


def _analyze_bundle(source, filepath=None, workspace_root=None):
    """Full lex+parse+analyze plus LSP diagnostics.

    Returns ``(source, tokens, parsed, analyzer, diagnostics, error)``.
    This is the expensive path; call it from a worker thread, never inline on
    the pygls event loop. Results are safe to hand back to the main thread
    (all objects are only read afterwards)."""
    t0 = time.time()
    tokens, parsed, analyzer, error = _analyze(
        source, filepath=filepath, workspace_root=workspace_root
    )
    diagnostics = []
    if error:
        # Skip the (0,0) fallback error if the analyzer already reported the
        # same message at a real location (raise-during-analyze cases often
        # leave the exact diagnostic in reporter.diagnostics).
        msg = str(error[1])
        reporter_diags = (
            getattr(getattr(analyzer, "reporter", None), "diagnostics", None) or []
        )
        if not any(d.message == msg for d in reporter_diags):
            diagnostics.append(_make_error_diagnostic(error))
    if analyzer:
        diagnostics.extend(_reporter_diagnostics(analyzer))
    elapsed = time.time() - t0
    logger.info(
        f"[analyze] {os.path.basename(filepath or '')}: "
        f"{len(diagnostics)} diag(s) in {elapsed * 1000:.0f}ms"
    )
    return (source, tokens, parsed, analyzer, diagnostics, error)


def _get_bundle(ls, uri):
    """Return the analysis bundle for ``uri``, reusing the cached result when
    the document source is unchanged. Falls back to a synchronous analysis on
    the caller's thread when the cache is stale (handlers that use this are
    thread-marked, so this never blocks the event loop)."""
    doc = ls.workspace.get_text_document(uri)
    source = doc.source
    filepath = _uri_to_path(uri)
    workspace_root = _uri_to_path(ls.workspace_root) if ls.workspace_root else None
    cache = getattr(ls, "_analysis_cache", None)
    if cache is not None:
        bundle = cache.get(uri)
        if bundle is not None and bundle[0] == source:
            return bundle
    bundle = _analyze_bundle(source, filepath=filepath, workspace_root=workspace_root)
    if cache is not None:
        cache[uri] = bundle
    return bundle


_ANALYSIS_DEBOUNCE = 0.25


def _schedule_analysis(ls, uri, delay=_ANALYSIS_DEBOUNCE):
    """Coalesce didOpen/didChange notifications: wait until the user pauses,
    then run the (off-loop) analysis and publish diagnostics."""
    loop = getattr(ls, "_loop", None)
    if loop is None or loop.is_closed():
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
    pending = getattr(ls, "_pending_analysis", None)
    if pending is None:
        pending = {}
        ls._pending_analysis = pending
    generations = getattr(ls, "_analysis_generation", None)
    if generations is None:
        generations = {}
        ls._analysis_generation = generations
    generations[uri] = generations.get(uri, 0) + 1
    prev = pending.get(uri)
    if prev is not None:
        prev.cancel()
    pending[uri] = loop.call_later(delay, _fire_analysis, ls, uri)


def _fire_analysis(ls, uri):
    pending = getattr(ls, "_pending_analysis", None)
    if pending is not None:
        pending.pop(uri, None)
    asyncio.ensure_future(_analyze_and_publish(ls, uri))


async def _analyze_and_publish(ls, uri):
    try:
        doc = ls.workspace.get_text_document(uri)
        source = doc.source
    except Exception:
        return
    filepath = _uri_to_path(uri)
    workspace_root = _uri_to_path(ls.workspace_root) if ls.workspace_root else None
    generation = (getattr(ls, "_analysis_generation", None) or {}).get(uri, 0)
    bundle = await asyncio.to_thread(
        _analyze_bundle, source, filepath=filepath, workspace_root=workspace_root
    )
    current = (getattr(ls, "_analysis_generation", None) or {}).get(uri, 0)
    if current != generation:
        return
    cache = getattr(ls, "_analysis_cache", None)
    if cache is not None:
        cache[uri] = bundle
    try:
        if ls.workspace.get_text_document(uri).source != source:
            return
    except Exception:
        return
    try:
        ls.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(uri=uri, diagnostics=bundle[4])
        )
    except Exception:
        pass


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


_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_MEMBER_CHAIN_RE = re.compile(
    r"([A-Za-z_]\w*(?:\s*(?:\.\s*[A-Za-z_]\w*|\([^()\n]*\))+)?)\s*\.\s*([A-Za-z_]\w*)?$"
)


def _last_ident_match(text):
    """The last identifier in *text*, or None (LSP prefixes can contain many)."""
    matches = list(_IDENT_RE.finditer(text))
    return matches[-1] if matches else None


def _line_prefix_before(source, line, col):
    """The raw line text before an LSP cursor (positions are after the char)."""
    lines = source.splitlines() if source else []
    if not (0 <= line < len(lines)):
        return ""
    return lines[line][: max(0, col)]


def _word_prefix_before(source, line, col):
    """Trailing identifier at the cursor, or '' when completing a new token."""
    prefix = _line_prefix_before(source, line, col)
    m = _last_ident_match(prefix)
    return m.group(0) if m else ""


def _member_chain_before(source, line, col):
    """Return ``(receiver_chain, member_prefix)`` for ``a.b.<member>``.

    Works at the real LSP cursor position (immediately *after* the typed text),
    unlike the token-contained lookup used by hover/definition.
    """
    prefix = _line_prefix_before(source, line, col)
    m = _MEMBER_CHAIN_RE.search(prefix)
    if m is None:
        return None
    chain = [part.strip() for part in m.group(1).split(".")]
    return chain, (m.group(2) or "")


def _call_snippet(name, params, const_params=(), skip=("this", "self")):
    """Build a snippet with one tab stop per call argument."""
    names = [n for n in (params or {}) if n not in skip]
    if not names:
        return f"{name}()"
    stops = ",".join(f"${{{i}:{n}}}" for i, n in enumerate(names, 1))
    return f"{name}({stops})"


def _completion_item(
    label,
    kind,
    *,
    detail="",
    insert_text=None,
    snippet=False,
    sort="5-other",
    documentation=None,
):
    item = lsp.CompletionItem(
        label=label,
        kind=kind,
        detail=detail or None,
        sort_text=sort,
        filter_text=label,
    )
    if insert_text is not None:
        item.insert_text = insert_text
        item.insert_text_format = (
            lsp.InsertTextFormat.Snippet if snippet else lsp.InsertTextFormat.PlainText
        )
    if documentation:
        item.documentation = documentation
    return item


def _dot_context(tokens, line, col):
    """Return the base identifier of a 'base.<member>' context at the cursor,
    or None if the cursor is not inside a member access."""
    tok = _find_token_at(tokens, line, col)
    if tok is not None and tok.type == TokenType.IDENTIFIER:
        idx = tokens.index(tok)
        if (
            idx >= 2
            and tokens[idx - 1] is not None
            and tokens[idx - 1].type == TokenType.DOT
        ):
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
        start = getattr(getattr(f, "_token", None), "line", None)
        if start is None:
            continue
        end = start
        for stmt in getattr(f, "body", None) or []:
            l = getattr(getattr(stmt, "_token", None), "line", None)
            if l is not None:
                end = max(end, l)
        if start <= line <= end:
            matches.append((start, end, f))
    if matches:
        matches.sort(key=lambda t: -t[0])
        return matches[0][2]
    for f in funcs:
        start = getattr(getattr(f, "_token", None), "line", None)
        if start is not None and start <= line:
            return f
    return None


_TOPLEVEL_KEYWORDS = {"def", "class", "struct", "enum", "import"}
_LOOP_ONLY_KEYWORDS = {"break", "continue"}
_BLOCK_KEYWORDS = {
    "def",
    "class",
    "struct",
    "enum",
    "if",
    "elif",
    "else",
    "for",
    "while",
    "switch",
    "try",
    "except",
}


def _scope_at(parsed, line):
    """Best statement-kind at the cursor: whether we're at module top level or
    inside a function/class/struct body."""
    for node in parsed:
        if isinstance(node, FuncDef):
            start = getattr(getattr(node, "_token", None), "line", None)
            end = max(
                [start or 0]
                + [
                    getattr(getattr(s, "_token", None), "line", 0) or 0
                    for s in getattr(node, "body", None) or []
                ]
            )
            if start is not None and start <= line <= end:
                return "function"
        elif isinstance(node, ClassDef) or isinstance(node, StructDef):
            start = getattr(getattr(node, "_token", None), "line", None)
            if start is not None and start <= line:
                return "type"
            if isinstance(node, ClassDef):
                for m in node.methods:
                    ms = getattr(getattr(m, "_token", None), "line", None)
                    me = max(
                        [ms or 0]
                        + [
                            getattr(getattr(s, "_token", None), "line", 0) or 0
                            for s in getattr(m, "body", None) or []
                        ]
                    )
                    if ms is not None and ms <= line <= me:
                        return "function"
    return "top"


def _inside_loop(parsed, line):
    """True if the cursor line sits inside a for/while loop body."""
    for node in parsed:
        if _stmt_contains_loop(node, line):
            return True
    return False


def _stmt_line(stmt):
    """Resolve the source line of a statement node or dict-like statement."""
    if stmt is None:
        return None
    tok = getattr(stmt, "_token", None)
    if tok is None and isinstance(stmt, dict):
        tok = stmt.get("_token") or stmt.get("token")
    tok = getattr(tok, "line", None) or getattr(tok, "lineno", None)
    return tok


def _stmt_children(stmt):
    """Yield direct child statements from a node or dict-like statement."""
    if isinstance(stmt, dict):
        for key in ("body", "orelse"):
            for c in stmt.get(key) or []:
                yield c
        return
    for attr in ("body", "orelse"):
        for c in getattr(stmt, attr, None) or []:
            yield c


def _is_loop_stmt(stmt):
    if isinstance(stmt, While):
        return True
    return isinstance(stmt, dict) and stmt.get("type") == "for"


def _stmt_contains_loop(stmt, line):
    if _is_loop_stmt(stmt):
        start = _stmt_line(stmt) or 0
        end = start
        for b in _stmt_children(stmt):
            l = _stmt_line(b)
            if l:
                end = max(end, l)
        if start <= line <= end:
            return True
        for b in _stmt_children(stmt):
            if _stmt_contains_loop(b, line):
                return True
        return False
    for child in _stmt_children(stmt):
        if _stmt_contains_loop(child, line):
            return True
    return False


def _walk_stmt(stmt, cursor_line, out):
    if isinstance(stmt, VarDecl):
        tok = stmt._token
        if tok is not None and tok.line <= cursor_line:
            out[stmt.name] = stmt.var_type or "int"
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
    if isinstance(stmt, dict) and stmt.get("type") == "for":
        if stmt.get("var"):
            out[stmt["var"]] = "int"
        for s in stmt.get("body") or []:
            _walk_stmt(s, cursor_line, out)
        return


def _collect_locals(func, cursor_line):
    out = {}
    for p, t in (func.params or {}).items():
        out[p] = t
    for stmt in getattr(func, "body", None) or []:
        _walk_stmt(stmt, cursor_line, out)
    return out


def _node_end_line(node):
    """Best-effort last source line of a declaration."""
    start = _stmt_line(node)
    if start is None:
        return None
    end = start
    extras = []
    for attr in ("fields", "methods", "properties"):
        extras.extend(getattr(node, attr, None) or [])
    for child in list(_stmt_children(node)) + extras:
        child_line = _stmt_line(child)
        if child_line is not None:
            end = max(end, child_line)
        for sub in _stmt_children(child):
            sub_line = _stmt_line(sub)
            if sub_line is not None:
                end = max(end, sub_line)
    return end


def _find_containing_class(parsed, line):
    for node in parsed:
        if not isinstance(node, ClassDef):
            continue
        start = _stmt_line(node)
        end = _node_end_line(node) or start
        if start is not None and start <= line <= end:
            return node
    return None


def _resolve_completion_symbol(analyzer, parsed, name, cursor_line):
    """Resolve an expression-root identifier to something with a type/node."""
    if analyzer is None or not name:
        return None
    sym = analyzer.globals.lookup(name)
    if sym is not None:
        return sym
    func = _find_containing_function(parsed, cursor_line)
    if func is not None and name in (func.params or {}):
        return SimpleNamespace(kind="param", type=func.params[name], node=None)
    locs = _collect_locals(func, cursor_line) if func else {}
    if name in locs:
        return SimpleNamespace(kind="variable", type=locs[name], node=None)
    if name == "this":
        cls = _find_containing_class(parsed, cursor_line)
        if cls is not None:
            return SimpleNamespace(kind="class", type=cls.name, node=cls)
    return None


def _class_member_nodes(analyzer, node):
    """Yield ``(name, kind, type_text, func_or_field)`` for a class/struct node,
    including inherited class members."""
    seen = set()
    while node is not None:
        for f in getattr(node, "fields", None) or []:
            if f.name not in seen:
                seen.add(f.name)
                yield f.name, "field", f.type_expr, f
        for m in getattr(node, "methods", None) or []:
            if m.name not in seen:
                seen.add(m.name)
                yield m.name, "method", m.rettype or "void", m
        for p in getattr(node, "properties", None) or []:
            if p.name not in seen:
                seen.add(p.name)
                yield p.name, "property", getattr(p, "_ptype", None) or "auto", p
        base = getattr(node, "base", None)
        if not base:
            break
        base_sym = analyzer.globals.lookup(base) if analyzer is not None else None
        node = getattr(base_sym, "node", None) if base_sym is not None else None


def _const_value_text(sym):
    node = getattr(sym, "node", None)
    if node is not None and getattr(node, "init", None) is not None:
        val = getattr(node.init, "value", None)
        if val is not None:
            return str(val)
    if sym.const_value is not None:
        return str(sym.const_value)
    return "?"


def _type_node_for(analyzer, type_name):
    """Resolve a type name (with optional `*` / `[]` decorations) to its
    struct/class/enum AST declaration node, or None."""
    if not type_name:
        return None
    t = type_name
    while t.endswith("*") or t.endswith("[]"):
        t = t[:-2] if t.endswith("[]") else t[:-1]
    t = t.strip("@")
    if t in (
        "int",
        "int64",
        "uint64",
        "float",
        "double",
        "str",
        "char",
        "void",
        "bool",
        "big",
        "size_t",
        "dynamic",
    ):
        return None
    type_sym = analyzer.globals.lookup(t)
    if type_sym is not None and type_sym.node is not None:
        return type_sym.node
    return None


def _iter_import_nodes(nodes):
    """Yield ``Import`` nodes recursively (including transitive sub-imports)."""
    for node in nodes or []:
        if isinstance(node, Import):
            yield node
        for sub in getattr(node, "sub_ast", None) or []:
            yield from _iter_import_nodes([sub])


def _collect_imported_symbols(parsed):
    """Collect public symbols (functions/structs/classes/enums/constants) re-exported
    through `import` statements so completion can offer them even though they live in
    another file/package."""
    out = {}
    for imp in _iter_import_nodes(parsed):
        for fname, sig in getattr(imp, "symbols", None) or []:
            if fname in out:
                continue
            if isinstance(sig, tuple) and len(sig) >= 2:
                ret, params = sig[0], sig[1]
                if isinstance(params, dict):
                    out[fname] = ("function", f"-> {ret or ''}", params)
                else:
                    out[fname] = ("function", f"-> {ret or ''}", {})
            else:
                out[fname] = ("function", "", {})
    return out


def _member_completions(analyzer, parsed, base, prefix, cursor_line):
    items = []
    if not base or analyzer is None:
        return items

    # 1) Resolve the base to a symbol: global, let-binding (import), function
    #    parameter, or a local variable in the enclosing function.
    sym = analyzer.globals.lookup(base)
    if sym is None:
        func = _find_containing_function(parsed, cursor_line)
        if func is not None and base in func.params:
            from types import SimpleNamespace

            sym = SimpleNamespace(kind="param", type=func.params[base], node=None)
        else:
            locs = _collect_locals(func, cursor_line) if func else {}
            if base in locs:
                from types import SimpleNamespace

                sym = SimpleNamespace(kind="variable", type=locs[base], node=None)
    if sym is None:
        return items

    seen = set()
    type_name = sym.type

    # 2) Enum member completion — both for an enum-typed value and when the base
    #    is the enum type name itself.
    is_enum = sym.kind == "enum"
    if type_name and not is_enum:
        t0 = type_name
        while t0.endswith("*") or t0.endswith("[]"):
            t0 = t0[:-2] if t0.endswith("[]") else t0[:-1]
        tsym = analyzer.globals.lookup(t0.strip("@"))
        if tsym is not None and tsym.kind == "enum":
            is_enum = True
    if is_enum:
        pfx = f"{base}."
        for name, m in analyzer.globals.symbols.items():
            if name.startswith(pfx):
                label = name[len(pfx) :]
                if label.startswith(prefix) and label not in seen:
                    items.append(
                        lsp.CompletionItem(
                            label=label,
                            kind=lsp.CompletionItemKind.EnumMember,
                            detail=f"enum member = {m.const_value}",
                            insert_text=label,
                        )
                    )
                    seen.add(label)
        if items:
            return items

    node = None
    # 3) If the base is itself a struct/class/enum type name (e.g. `Vec.` or
    #    `MyStruct.`), offer its members directly.
    if sym.kind in ("struct", "class", "enum", "type_alias"):
        node = sym.node or _type_node_for(analyzer, base)
    # 4) Otherwise resolve the base value's declared type to its node.
    if node is None:
        node = _type_node_for(analyzer, type_name)
    if node is None:
        node = getattr(sym, "node", None)
    if node is None:
        return items

    for f in getattr(node, "fields", None) or []:
        if f.name.startswith(prefix) and f.name not in seen:
            items.append(
                lsp.CompletionItem(
                    label=f.name,
                    kind=lsp.CompletionItemKind.Field,
                    detail=f"field: {f.type_expr}",
                    insert_text=f.name,
                )
            )
            seen.add(f.name)
    for m in getattr(node, "methods", None) or []:
        if m.name.startswith(prefix) and m.name not in seen:
            sig = _fmt_params(m.params, getattr(m, "const_params", None))
            items.append(
                lsp.CompletionItem(
                    label=m.name,
                    kind=lsp.CompletionItemKind.Method,
                    detail=f"({sig}) -> {m.rettype or 'void'}",
                    insert_text=f"{m.name}(",
                )
            )
            seen.add(m.name)
    return items


def _package_member_completions(chain, prefix, ls, uri, parsed):
    """Module/package namespace completions for chains such as ``std.``."""
    base = ".".join(p.lstrip("@") for p in chain)
    labels = {}

    def add(label, detail, kind):
        if label and label.startswith(prefix) and label not in labels:
            labels[label] = _completion_item(
                label,
                kind,
                detail=detail,
                insert_text=label,
                sort="3-module",
            )

    for imp in _iter_import_nodes(parsed):
        module = (getattr(imp, "module", None) or "").replace("/", ".")
        if module.startswith("@"):
            module = module[1:]
        if base and module.startswith(base + "."):
            add(module[len(base) + 1 :].split(".")[0], "imported module", lsp.CompletionItemKind.Module)
        elif not base and module:
            add(module.split(".")[0], "imported module", lsp.CompletionItemKind.Module)

    filepath = _uri_to_path(uri) if uri else None
    file_dir = os.path.dirname(filepath) if filepath else None
    workspace_root = _uri_to_path(ls.workspace_root) if ls is not None else None
    roots = cpm_module_roots(workspace_root, file_dir)
    for root in roots:
        for package, version_dir in iter_cpm_version_dirs(root):
            module = package.replace("/", ".")
            if base and module.startswith(base + "."):
                add(module[len(base) + 1 :].split(".")[0], "CPM package", lsp.CompletionItemKind.Module)
            elif not base and module:
                add(module.split(".")[0], "CPM package", lsp.CompletionItemKind.Module)
            elif base and module == base and os.path.isdir(version_dir):
                try:
                    entries = sorted(os.listdir(version_dir))
                except OSError:
                    entries = []
                for entry in entries:
                    if entry.endswith(".cpy"):
                        add(entry[:-4], "CPM submodule", lsp.CompletionItemKind.File)
                    elif os.path.isdir(os.path.join(version_dir, entry)):
                        add(entry, "CPM submodule", lsp.CompletionItemKind.Module)

    return list(labels.values())


def _member_items_for_symbol(analyzer, sym, prefix):
    """Render fields/methods/properties/enum members for a resolved symbol."""
    items = []
    seen = set()
    type_name = getattr(sym, "type", None)
    node = getattr(sym, "node", None)

    base_type = type_name or ""
    while base_type.endswith("*") or base_type.endswith("[]"):
        base_type = base_type[:-2] if base_type.endswith("[]") else base_type[:-1]
    base_type = base_type.strip("@")

    is_enum = getattr(sym, "kind", None) == "enum"
    tsym = analyzer.globals.lookup(base_type) if analyzer is not None and base_type else None
    if tsym is not None and tsym.kind == "enum":
        is_enum = True
        node = getattr(tsym, "node", None) or node

    if is_enum and base_type:
        enum_prefix = f"{base_type}."
        for name, member in (analyzer.globals.symbols.items() if analyzer else []):
            if not name.startswith(enum_prefix):
                continue
            label = name[len(enum_prefix) :]
            if label.startswith(prefix) and label not in seen:
                seen.add(label)
                items.append(
                    _completion_item(
                        label,
                        lsp.CompletionItemKind.EnumMember,
                        detail=f"enum member = {getattr(member, 'const_value', None)}",
                        insert_text=label,
                        sort="1-field",
                    )
                )
        if items:
            return items

    if node is None and tsym is not None:
        node = getattr(tsym, "node", None)
    if node is None and type_name:
        node = _type_node_for(analyzer, type_name)
    if node is None:
        return items

    for name, kind, type_text, obj in _class_member_nodes(analyzer, node):
        if not name.startswith(prefix) or name in seen:
            continue
        seen.add(name)
        if kind == "method":
            params = getattr(obj, "params", None) or {}
            const_params = getattr(obj, "const_params", None)
            detail = f"({_fmt_params(params, const_params)}) -> {type_text}"
            items.append(
                _completion_item(
                    name,
                    lsp.CompletionItemKind.Method,
                    detail=detail,
                    insert_text=_call_snippet(name, params, const_params or ()),
                    snippet=True,
                    sort="2-method",
                )
            )
        elif kind == "property":
            items.append(
                _completion_item(
                    name,
                    lsp.CompletionItemKind.Property,
                    detail=f"property: {type_text}",
                    insert_text=name,
                    sort="1-field",
                )
            )
        else:
            items.append(
                _completion_item(
                    name,
                    lsp.CompletionItemKind.Field,
                    detail=f"field: {type_text}",
                    insert_text=name,
                    sort="1-field",
                )
            )
    return items


def _chain_member_completions(analyzer, parsed, chain, prefix, cursor_line, ls, uri):
    """Resolve ``a.b.c`` / ``a.b().c`` receivers and offer member completions."""
    if not chain:
        return []

    def resolve_root(name):
        called = name.endswith("()")
        plain = name[:-2] if called else name
        sym = _resolve_completion_symbol(analyzer, parsed, plain, cursor_line)
        if sym is None or not called:
            return sym
        rettype = getattr(sym, "type", None) or getattr(
            getattr(sym, "node", None), "rettype", None
        )
        return SimpleNamespace(
            kind="variable", type=rettype, node=_type_node_for(analyzer, rettype)
        )

    sym = resolve_root(chain[0])
    if sym is None:
        return _package_member_completions(chain, prefix, ls, uri, parsed)

    for part in chain[1:]:
        node = getattr(sym, "node", None) or _type_node_for(
            analyzer, getattr(sym, "type", None)
        )
        if node is None:
            return _package_member_completions(chain, prefix, ls, uri, parsed)
        called = part.endswith("()")
        plain = part[:-2] if called else part
        found = None
        for name, kind, type_text, obj in _class_member_nodes(analyzer, node):
            if name != plain:
                continue
            if kind == "method":
                type_text = getattr(obj, "rettype", None) or type_text
            if called:
                found = SimpleNamespace(
                    kind="variable",
                    type=type_text,
                    node=_type_node_for(analyzer, type_text),
                )
            else:
                # Fields/properties have no useful declaration node; resolve the
                # declared type so the next chain segment sees the right class.
                member_node = getattr(obj, "node", None)
                if kind in ("field", "property") or member_node is None:
                    member_node = _type_node_for(analyzer, type_text)
                found = SimpleNamespace(kind=kind, type=type_text, node=member_node)
            break
        if found is None:
            owner = getattr(node, "name", None) or chain[-2]
            member = (
                analyzer.globals.lookup(f"{owner}.{plain}")
                if analyzer is not None
                else None
            )
            if member is not None:
                found = member
        if found is None:
            return []
        sym = found

    return _member_items_for_symbol(analyzer, sym, prefix)


_IMPORT_EXTENSIONS = (".cpy", ".c", ".cc", ".h", ".hpp", ".cpp")


def _import_string_context(source, line, col):
    """Return ``(partial, start_col, end_col)`` when the cursor is inside the
    quoted string of an `import "..."` statement (raw line scan, so it also
    matches while the string is still open/mid-typing):

    - ``partial`` — the module path typed so far inside the quotes,
    - ``start_col/end_col`` — the character range the quotes occupy
      (``end_col`` may extend past the *cursor* for an unterminated quote).

    Returns ``None`` for any other line or if the closing quote was already
    typed and the cursor sits after it.
    """
    lines = source.splitlines() if source else []
    if not (0 <= line < len(lines)):
        return None
    text = lines[line]
    # A strip of leading whitespace, then "import", then optional sdk(...).
    head = text.lstrip()
    indent = len(text) - len(head)
    if not head.startswith("import"):
        return None
    # find the opening quote that starts the module path (skipping the keyword)
    import_end = indent + len("import")
    rest = text[import_end:]
    q = rest.find('"')
    if q == -1:
        q = rest.find("'")
        quote = "'"
    else:
        quote = '"'
    if q == -1:
        return None
    open_col = import_end + q
    # module path fills from open_col+1 to the next quote or end of line
    tail = text[open_col + 1 :]
    close = tail.find(quote)
    if close == -1:
        # unterminated string — treat through the cursor
        content_end = len(text)
    else:
        content_end = open_col + 1 + close
    if col <= open_col:
        return None
    if close != -1 and col > content_end:
        return None
    end = len(text) if close == -1 else content_end
    partial = text[open_col + 1 : min(col, end)]
    return partial, open_col, end


_IMPORT_MODULE_RE = re.compile(r"^\s*import\s+([A-Za-z_@][\w@/.\-]*)$")


def _import_module_context(source, line, col):
    """Return ``(partial, start_col)`` for a bare/canonical `import ...`."""
    prefix = _line_prefix_before(source, line, col)
    if '"' in prefix or "'" in prefix:
        return None
    m = _IMPORT_MODULE_RE.match(prefix)
    if m is None:
        return None
    partial = m.group(1)
    return partial, len(prefix) - len(partial)


def _completion_with_edit(item, line, start, end, new_text):
    item.text_edit = lsp.TextEdit(
        range=lsp.Range(
            start=lsp.Position(line=line, character=start),
            end=lsp.Position(line=line, character=end),
        ),
        new_text=new_text,
    )
    return item


def _import_module_completions(partial, line, start, end, ls, uri, parsed):
    """Completions for `import std`, `import @std/math`, `import std.random.pr`."""
    items = []
    seen = set()

    def add(value, detail, kind):
        if value in seen or not value.startswith(partial):
            return
        seen.add(value)
        item = _completion_item(
            value,
            kind,
            detail=detail,
            insert_text=value,
            sort="3-module",
        )
        items.append(_completion_with_edit(item, line, start, end, value))

    if "/" not in partial and "." not in partial and "@" not in partial:
        for lib in sorted(_BUILTIN_LIB_HEADERS):
            add(lib, "C library", lsp.CompletionItemKind.Module)

    filepath = _uri_to_path(uri) if uri else None
    file_dir = os.path.dirname(filepath) if filepath else None
    workspace_root = _uri_to_path(ls.workspace_root) if ls is not None else None
    for root in cpm_module_roots(workspace_root, file_dir):
        for package, version_dir in iter_cpm_version_dirs(root):
            plain = package[1:] if package.startswith("@") else package
            canonical = f"@{plain}" if "/" in plain else plain
            dotted = plain.replace("/", ".")
            add(canonical, "CPM package", lsp.CompletionItemKind.Module)
            add(dotted, "CPM package", lsp.CompletionItemKind.Module)
            if partial.startswith(f"{dotted}.") or partial.startswith(f"{canonical}/"):
                try:
                    entries = sorted(os.listdir(version_dir))
                except OSError:
                    entries = []
                for entry in entries:
                    if entry.endswith(".cpy"):
                        stem = entry[:-4]
                        add(f"{dotted}.{stem}", "CPM submodule", lsp.CompletionItemKind.File)
                        add(f"{canonical}/{stem}", "CPM submodule", lsp.CompletionItemKind.File)
                    elif os.path.isdir(os.path.join(version_dir, entry)):
                        add(f"{dotted}.{entry}", "CPM submodule", lsp.CompletionItemKind.Module)
                        add(f"{canonical}/{entry}", "CPM submodule", lsp.CompletionItemKind.Module)

    for imp in _iter_import_nodes(parsed):
        module = getattr(imp, "module", None)
        if module:
            plain = module[1:] if module.startswith("@") else module
            add(plain, "imported module", lsp.CompletionItemKind.Module)
            if "/" in plain:
                add(f"@{plain}", "imported module", lsp.CompletionItemKind.Module)
            add(plain.replace("/", "."), "imported module", lsp.CompletionItemKind.Module)

    if file_dir and os.path.isdir(file_dir):
        for entry in sorted(os.listdir(file_dir)):
            if entry.endswith(".cpy") and entry.startswith(partial):
                add(entry[:-4], "local module", lsp.CompletionItemKind.File)

    return items


def _import_dir_completions(partial, file_dir, workspace_root):
    """Path completions for the text inside `import "..."`.

    ``partial`` is the module-path text typed so far (e.g. ``"lib/mat"``).
    Candidates are the entries of each base directory *scoped to the partial's
    directory part*: `import "lib/` lists the contents of ``./lib``, and
    directories are offered with a trailing ``/``. Builtin C-library names are
    offered at the top level (no ``/`` in the partial).

    The insert text is scoped to the *last path segment* (editors replace the
    current word, preserving a typed directory prefix); the label shows the
    full relative path for clarity.
    """
    items = []
    prefix = partial.rsplit("/", 1)[-1]
    dirpart = partial.rsplit("/", 1)[0] if "/" in partial else ""
    dir_label = (dirpart + "/") if dirpart else ""
    seen = set()

    def add(label, kind, snippet, detail):
        if label in seen:
            return
        seen.add(label)
        items.append(
            lsp.CompletionItem(
                label=label,
                kind=kind,
                detail=detail,
                insert_text=snippet,
                insert_text_format=lsp.InsertTextFormat.PlainText,
            )
        )

    if not dirpart:
        for lib, header in sorted(_BUILTIN_LIB_HEADERS.items()):
            if lib.startswith(prefix):
                add(lib, lsp.CompletionItemKind.Module, lib, f"C library ({header})")

    bases = []
    if file_dir:
        bases.append(file_dir)
    if workspace_root:
        bases.append(workspace_root)
    # CPM direct installs are global (~/.cpm/modules); manifest-driven ones
    # stay under the workspace. Offer completions from both.
    bases.extend(cpm_module_roots(workspace_root, file_dir))

    for base in bases:
        if not base:
            continue
        d = os.path.join(base, dirpart) if dirpart else base
        if not os.path.isdir(d):
            continue
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for entry in entries:
            if entry.startswith("."):
                continue
            if not entry.startswith(prefix):
                continue
            p = os.path.join(d, entry)
            if os.path.isdir(p):
                add(
                    dir_label + entry + "/",
                    lsp.CompletionItemKind.Module,
                    entry + "/",
                    "directory",
                )
            elif entry.endswith(_IMPORT_EXTENSIONS):
                add(dir_label + entry, lsp.CompletionItemKind.File, entry, "file")
    return items


# Block-starting keywords that expand to a snippet. If the user is already
# typing one of these on the current line (e.g. mid-`for` loop), re-offering the
# same snippet is noise — suppress it.
_SNIPPET_KEYWORDS = set(_SNIPPETS) | {"public def"}


def _keyword_on_line(tokens, line, col, keyword):
    """True if `keyword` appears earlier on the cursor's line (before col).

    Used to avoid re-suggesting a block-keyword snippet (for/if/while/...)
    when the user has already started typing that construct."""
    for t in tokens:
        if t is None or t.value != keyword:
            continue
        if (t.line - 1) == line and (t.column - 1) < col:
            return True
    return False


def _is_new_context(tokens, line, col):
    """True if the cursor is completing the type name in a `new T` expression
    (the token under/just before the cursor is an identifier immediately
    preceded by the `new` keyword)."""
    tok = _find_token_at(tokens, line, col)
    if tok is not None and tok.type in (TokenType.IDENTIFIER, TokenType.KEYWORD):
        idx = tokens.index(tok)
    else:
        prev = None
        for t in tokens:
            if t is None:
                continue
            if (t.line - 1) > line or ((t.line - 1) == line and (t.column - 1) >= col):
                break
            prev = t
        if prev is None:
            return False
        idx = tokens.index(prev)
    if idx - 2 >= 0:
        a = tokens[idx - 2]
        b = tokens[idx - 1]
        if (
            a is not None
            and b is not None
            and a.type == TokenType.KEYWORD
            and a.value == "new"
            and b.type == TokenType.IDENTIFIER
        ):
            return True
    return False


def _type_context(source, line, col):
    """True when the cursor is completing a type name rather than a value."""
    prefix = _line_prefix_before(source, line, col)
    return bool(
        re.search(r"\bnew\s+[A-Za-z_]\w*$", prefix)
        or re.search(r"\bas\s+[A-Za-z_]\w*$", prefix)
        or re.search(r"\bsizeof\s*\(\s*[A-Za-z_]\w*$", prefix)
        or re.search(r"->\s*[A-Za-z_]\w*$", prefix)
        or re.search(r":\s*[A-Za-z_]\w*$", prefix)
        or re.search(r"<\s*[A-Za-z_]\w*$", prefix)
    )


def _repair_source_for_completion(source, line, col):
    """Make a parseable copy of an in-progress line for completion.

    The editor asks for completions after every keystroke, so the document is
    often temporarily invalid (``obj.``, ``obj.fi``, ``new Th``, ``import
    std.``).  The analyzer cannot resolve symbols from a parse failure, so
    replace just the token under the cursor with a placeholder and analyze
    that copy.  The original source is still used to compute the prefix and
    context, so the placeholder never leaks into completion labels.
    """
    lines = source.splitlines(keepends=True)
    if not (0 <= line < len(lines)):
        return source
    if _import_string_context(source, line, col) is not None:
        # Quoted import paths are handled from the raw line; no analysis needed.
        return source
    raw = lines[line]
    newline = "\n" if raw.endswith("\n") else ""
    body = raw[:-1] if newline else raw
    prefix = body[: max(0, col)]
    suffix = body[max(0, col) :]

    if (bare_import := _import_module_context(source, line, col)) is not None:
        _partial, start = bare_import
        new_prefix = f"{prefix[:start]}std"
    elif _member_chain_before(source, line, col) is not None:
        if re.search(r"\.\s*$", prefix):
            new_prefix = f"{prefix}value"
        else:
            m = _last_ident_match(prefix)
            new_prefix = f"{prefix[: m.start()]}value" if m else prefix
    elif _type_context(source, line, col):
        m = _last_ident_match(prefix)
        new_prefix = f"{prefix[: m.start()]}int" if m else f"{prefix}int"
    else:
        m = _last_ident_match(prefix)
        if m is None:
            return source
        new_prefix = f"{prefix[: m.start()]}value"

    lines[line] = f"{new_prefix}{suffix}{newline}"
    return "".join(lines)


class CpyLanguageServer(LanguageServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace_root: str | None = None
        # Analysis cache and single-flight state. Handlers are thread-marked, so
        # several requests can arrive while a document is being analyzed; the
        # generation counter discards a worker whose source is already stale.
        self._analysis_cache = {}
        self._pending_analysis = {}
        self._analysis_generation = {}

    def get_workspace_root(self) -> str | None:
        return self.workspace_root


server = CpyLanguageServer(
    "cpyte-lsp", "0.1", text_document_sync_kind=lsp.TextDocumentSyncKind.Incremental
)


@server.feature(lsp.INITIALIZE)
def initialize(ls: CpyLanguageServer, params: lsp.InitializeParams):
    if params.root_uri:
        ls.workspace_root = _uri_to_path(params.root_uri)
    elif params.root_path:
        ls.workspace_root = params.root_path
    try:
        ls._loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    logger.info(f"[initialize] workspace_root={ls.workspace_root}")


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: CpyLanguageServer, params: lsp.DidOpenTextDocumentParams):
    try:
        logger.info(
            f"[didOpen] {os.path.basename(_uri_to_path(params.text_document.uri))}"
        )
        _schedule_analysis(ls, params.text_document.uri, 0.0)
    except Exception:
        logger.error(f"did_open error:\n{traceback.format_exc()}")


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: CpyLanguageServer, params: lsp.DidChangeTextDocumentParams):
    try:
        logger.info(
            f"[didChange] {os.path.basename(_uri_to_path(params.text_document.uri))}"
        )
        _schedule_analysis(ls, params.text_document.uri, _ANALYSIS_DEBOUNCE)
    except Exception:
        logger.error(f"did_change error:\n{traceback.format_exc()}")


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: CpyLanguageServer, params: lsp.DidCloseTextDocumentParams):
    try:
        uri = params.text_document.uri
        pending = getattr(ls, "_pending_analysis", None)
        if pending is not None:
            handle = pending.pop(uri, None)
            if handle is not None:
                handle.cancel()
        cache = getattr(ls, "_analysis_cache", None)
        if cache is not None:
            cache.pop(uri, None)
        generations = getattr(ls, "_analysis_generation", None)
        if generations is not None:
            generations[uri] = generations.get(uri, 0) + 1
    except Exception:
        logger.error(f"did_close error:\n{traceback.format_exc()}")


@server.feature(
    lsp.TEXT_DOCUMENT_COMPLETION,
    lsp.CompletionOptions(
        trigger_characters=[".", " ", ":", "<", "(", ","],
        resolve_provider=False,
    ),
)
@server.thread()
def completions(ls: CpyLanguageServer, params: lsp.CompletionParams):
    ranked = {}
    uri = None
    t0 = time.time()

    def add(name, item, rank):
        prior = ranked.get(name)
        if prior is None or rank < prior[0]:
            ranked[name] = (rank, item)

    def finish(label="items"):
        items = [item for _rank, item in sorted(ranked.values(), key=lambda p: (p[0], p[1].label))]
        elapsed = time.time() - t0
        name = os.path.basename(_uri_to_path(uri)) if uri else "?"
        logger.info(f"[completion] {name}: {len(items)} {label} in {elapsed * 1000:.0f}ms")
        return lsp.CompletionList(is_incomplete=False, items=items)

    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        source, tokens, parsed, analyzer, diagnostics, error = _get_bundle(ls, uri)
        if (not parsed or analyzer is None) and (error or diagnostics):
            # A half-typed expression usually leaves the document unparsable.
            # Analyze a repaired copy so completion still sees live symbols.
            repaired = _repair_source_for_completion(source, line, col)
            if repaired != source:
                filepath = _uri_to_path(uri)
                workspace_root = (
                    _uri_to_path(ls.workspace_root) if ls.workspace_root else None
                )
                (
                    _repaired_source,
                    repaired_tokens,
                    parsed,
                    analyzer,
                    _,
                    _,
                ) = _analyze_bundle(
                    repaired, filepath=filepath, workspace_root=workspace_root
                )
                tokens = repaired_tokens
        prefix = _word_prefix_before(source, line, col)
        scope = _scope_at(parsed, line + 1)
        in_loop = _inside_loop(parsed, line + 1)
        want_types = _type_context(source, line, col)

        # Quoted import paths work even while the string is still open.
        import_ctx = _import_string_context(source, line, col)
        if import_ctx is not None:
            partial, _start, _end = import_ctx
            filepath = _uri_to_path(uri)
            file_dir = os.path.dirname(filepath) if filepath else None
            workspace_root = (
                _uri_to_path(ls.workspace_root) if ls.workspace_root else None
            )
            ranked.update(
                {
                    item.label: (0, item)
                    for item in _import_dir_completions(
                        partial, file_dir, workspace_root
                    )
                }
            )
            return finish("import-path item(s)")

        bare_import = _import_module_context(source, line, col)
        if bare_import is not None:
            partial, start = bare_import
            for item in _import_module_completions(
                partial, line, start, col, ls, uri, parsed
            ):
                ranked[item.label] = (0, item)
            return finish("import item(s)")

        chain_ctx = _member_chain_before(source, line, col)
        if chain_ctx is not None:
            chain, member_prefix = chain_ctx
            for item in _chain_member_completions(
                analyzer, parsed, chain, member_prefix, line + 1, ls, uri
            ):
                ranked[item.label] = (0, item)
            return finish(f"member item(s) for `{'.'.join(chain)}.`")

        # Types are always available (new/as/sizeof/annotations/generics) and
        # get a low-priority slot in ordinary expression positions.
        type_names = {}
        for t in _TYPES:
            type_names[t] = _TYPE_DESC.get(t, "")
        for node in parsed:
            if isinstance(node, StructDef):
                type_names[node.name] = "struct"
            elif isinstance(node, ClassDef):
                type_names[node.name] = f"class{(' extends ' + node.base) if node.base else ''}"
            elif isinstance(node, EnumDef):
                type_names[node.name] = "enum"
            elif isinstance(node, FuncDef) and getattr(node, "generic_params", None):
                for gp in node.generic_params:
                    type_names[gp] = "type parameter"
        if analyzer is not None:
            for name, sym in analyzer.globals.symbols.items():
                if "." in name:
                    continue
                if sym.kind in ("struct", "class", "enum", "protocol", "type_alias"):
                    type_names.setdefault(name, sym.kind)
        for name, detail in type_names.items():
            if name.startswith(prefix):
                add(
                    name,
                    _completion_item(
                        name,
                        lsp.CompletionItemKind.TypeParameter,
                        detail=detail,
                        insert_text=name,
                        sort="7-type",
                    ),
                    70,
                )

        if want_types:
            return finish("type item(s)")

        func = _find_containing_function(parsed, line + 1)
        if func is not None:
            for p, pt in (func.params or {}).items():
                if p == "this" or not p.startswith(prefix):
                    continue
                add(
                    p,
                    _completion_item(
                        p,
                        lsp.CompletionItemKind.Variable,
                        detail=f"parameter: {pt}",
                        sort="0-local",
                    ),
                    0,
                )
            for lname, ltype in _collect_locals(func, line + 1).items():
                if lname.startswith(prefix):
                    add(
                        lname,
                        _completion_item(
                            lname,
                            lsp.CompletionItemKind.Variable,
                            detail=ltype or "int",
                            sort="0-local",
                        ),
                        0,
                    )

        cls = _find_containing_class(parsed, line + 1)
        if cls is not None:
            for name, kind, type_text, _obj in _class_member_nodes(analyzer, cls):
                if not name.startswith(prefix) or name == "this":
                    continue
                item_kind = {
                    "field": lsp.CompletionItemKind.Field,
                    "method": lsp.CompletionItemKind.Method,
                    "property": lsp.CompletionItemKind.Property,
                }.get(kind, lsp.CompletionItemKind.Field)
                add(
                    name,
                    _completion_item(
                        name,
                        item_kind,
                        detail=f"{kind}: {type_text}",
                        insert_text=name,
                        sort="1-member",
                    ),
                    10,
                )

        for node in parsed:
            if not isinstance(node, FuncDef) or not node.name.startswith(prefix):
                continue
            params = getattr(node, "params", None) or {}
            const_params = getattr(node, "const_params", None) or ()
            add(
                node.name,
                _completion_item(
                    node.name,
                    lsp.CompletionItemKind.Function,
                    detail=f"({_fmt_params(params, const_params)}) -> {node.rettype or 'void'}",
                    insert_text=_call_snippet(node.name, params, const_params),
                    snippet=True,
                    sort="2-function",
                ),
                20,
            )

        if analyzer is not None:
            for name, sym in analyzer.globals.symbols.items():
                if "." in name or not name.startswith(prefix):
                    continue
                if sym.kind in ("struct", "class", "enum", "protocol", "type_alias"):
                    continue
                if sym.kind == "const":
                    kind = lsp.CompletionItemKind.Constant
                    detail = f"const {sym.type} = {_const_value_text(sym)}"
                    rank, sort = 30, "3-global"
                elif sym.kind in ("function", "builtin_func"):
                    kind = lsp.CompletionItemKind.Function
                    snode = getattr(sym, "node", None)
                    params = getattr(snode, "params", None) or {}
                    detail = f"-> {sym.type or ''}"
                    rank, sort = 35, "3-global"
                else:
                    kind = lsp.CompletionItemKind.Variable
                    detail = sym.type or sym.kind
                    rank, sort = 30, "3-global"
                item = _completion_item(
                    name, kind, detail=detail, sort=sort
                )
                if rank == 35:
                    item.insert_text = _call_snippet(name, params)
                    item.insert_text_format = lsp.InsertTextFormat.Snippet
                add(name, item, rank)

        for name, (kind, detail, params) in _collect_imported_symbols(parsed).items():
            if not name.startswith(prefix):
                continue
            item = _completion_item(
                name,
                lsp.CompletionItemKind.Function
                if kind == "function"
                else lsp.CompletionItemKind.Class,
                detail=f"{detail} (imported)",
                sort="4-imported",
            )
            if kind == "function" and params:
                item.insert_text = _call_snippet(name, params)
                item.insert_text_format = lsp.InsertTextFormat.Snippet
            add(name, item, 40)

        for name, sig in _BUILTIN_FUNCS.items():
            if not name.startswith(prefix):
                continue
            params = _BUILTIN_FUNC_PARAMS.get(name, {})
            add(
                name,
                _completion_item(
                    name,
                    lsp.CompletionItemKind.Function,
                    detail=sig,
                    insert_text=_call_snippet(name, params),
                    snippet=True,
                    sort="5-builtin",
                ),
                50,
            )

        for kw in sorted(set(_KEYWORDS) | set(get_keywords())):
            if not kw.startswith(prefix):
                continue
            if _SNIPPETS.get(kw) and _keyword_on_line(tokens, line, col, kw):
                continue
            if kw in _TOPLEVEL_KEYWORDS and scope != "top":
                continue
            if kw in _LOOP_ONLY_KEYWORDS and not in_loop:
                continue
            snippet = _SNIPPETS.get(kw)
            add(
                kw,
                _completion_item(
                    kw,
                    lsp.CompletionItemKind.Keyword,
                    detail=_KEYWORD_DESC.get(kw, ""),
                    insert_text=snippet or kw,
                    snippet=bool(snippet),
                    documentation=_KEYWORD_DESC.get(kw),
                    sort="9-keyword",
                ),
                90,
            )

        return finish()
    except Exception:
        logger.error(f"completions error:\n{traceback.format_exc()}")
        return finish("items before error")


@server.feature(lsp.TEXT_DOCUMENT_HOVER)
@server.thread()
def hover(ls: CpyLanguageServer, params: lsp.HoverParams):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        t0 = time.time()
        source, tokens, parsed, analyzer, _, _ = _get_bundle(ls, uri)
        tok = _find_token_at(tokens, line, col)
        if not tok or not tok.value:
            return None

        word = tok.value
        content = None

        if word in _TYPE_DESC:
            content = f"**`{word}`** — {_TYPE_DESC[word]}"
        elif word in _KEYWORD_DESC:
            content = f"**`{word}`** — {_KEYWORD_DESC[word]}"
        elif word in _BUILTIN_FUNCS:
            content = f"**`{_BUILTIN_FUNCS[word]}`**  \n*built-in function*"
        else:
            base = _dot_context(tokens, line, col)
            if base is not None and analyzer:
                name = f"{base}.{word}"
                sym = analyzer.globals.lookup(name)
                if sym:
                    if sym.kind == "field":
                        content = f"**`{word}`**: `{sym.type}`  \n*field of {base}*"
                    elif sym.kind == "function":
                        content = f"**`{word}`**: `{sym.type}`  \n*method*"
                if not content:
                    base_sym = analyzer.globals.lookup(base)
                    if (
                        base_sym
                        and hasattr(base_sym, "node")
                        and base_sym.node is not None
                    ):
                        node = base_sym.node
                        for f in getattr(node, "fields", None) or []:
                            if f.name == word:
                                content = f"**`{word}`**: `{f.type_expr}`  \n*field of {base}*"
                                break
                        if not content:
                            for m in getattr(node, "methods", None) or []:
                                if m.name == word:
                                    sig = _fmt_params(
                                        m.params, getattr(m, "const_params", None)
                                    )
                                    content = f"**`{m.name}({sig}) → {m.rettype or 'void'}`**  \n*method of {base}*"
                                    break

            if not content:
                for node in parsed:
                    if isinstance(node, FuncDef) and node.name == word:
                        sig = _fmt_params(
                            node.params, getattr(node, "const_params", None)
                        )
                        ret = node.rettype or "void"
                        content = f"**`{node.name}({sig}) → {ret}`**"
                        if node.decorators:
                            decs = ", ".join(
                                f"@{_decorator_text(d)}" for d in node.decorators
                            )
                            content += f"  \n*decorators: {decs}*"
                        if node.visibility:
                            content += f"  \n*visibility: `{node.visibility}`*"
                        break
                    elif isinstance(node, StructDef) and node.name == word:
                        fields = ", ".join(
                            f"`{f.name}`: `{f.type_expr}`" for f in node.fields
                        )
                        content = f"**`struct {node.name}`**  \n`{{ {fields} }}`"
                        break
                    elif isinstance(node, ClassDef) and node.name == word:
                        fields = ", ".join(
                            f"`{f.name}`: `{f.type_expr}`" for f in node.fields
                        )
                        methods = ", ".join(f"`{m.name}()`" for m in node.methods)
                        content = f"**`class {node.name}`**  \n`{{ {fields} }}`"
                        if node.base:
                            content += f"  \nextends `{node.base}`"
                        if node.methods:
                            content += f"  \nmethods: {methods}"
                        break
                    elif isinstance(node, EnumDef) and node.name == word:
                        members = ", ".join(
                            f"`{m['name']}` = `{m.get('_const_value')}`"
                            for m in node.members
                        )
                        content = f"**`enum {node.name}`**  \n{{ {members} }}"
                        break
            if not content:
                sym = analyzer.globals.lookup(word) if analyzer else None
                if sym:
                    if sym.kind == "function":
                        content = (
                            f"**`{word}`** → `{sym.type if sym.type != 'void' else ''}`"
                        )
                    elif sym.kind == "const":
                        content = f"**`{word}`**: `{sym.type}` = `{_const_value_text(sym)}`  \n*constant*"
                    elif sym.kind == "enum":
                        content = f"**`enum {word}`**"
                    elif sym.kind == "enum_member":
                        content = f"**`{word}`**: `{sym.type}` = `{_const_value_text(sym)}`  \n*enum member*"
                    elif sym.kind == "package":
                        content = f"**`{word}`**  \n*imported package*"
                    else:
                        content = f"**`{word}`**: `{sym.type or sym.kind}`"
                else:
                    func = _find_containing_function(parsed, line + 1)
                    if func is not None:
                        if word in (func.params or {}):
                            content = (
                                f"**`{word}`**: `{func.params[word]}`  \n*parameter*"
                            )
                        else:
                            locs = _collect_locals(func, line + 1)
                            if word in locs:
                                content = f"**`{word}`**: `{locs[word] or 'int'}`  \n*local variable*"

        elapsed = time.time() - t0
        logger.info(
            f"[hover] {os.path.basename(_uri_to_path(uri))} @L{line + 1}:{col}: "
            f"{len(content or '')} chars in {elapsed * 1000:.0f}ms"
        )
        if content:
            return lsp.Hover(
                contents=lsp.MarkupContent(kind=lsp.MarkupKind.Markdown, value=content)
            )
        return None
    except Exception:
        logger.error(f"hover error:\n{traceback.format_exc()}")
        return None


@server.feature(
    lsp.TEXT_DOCUMENT_SIGNATURE_HELP,
    lsp.SignatureHelpOptions(trigger_characters=["(", ","]),
)
@server.thread()
def signature_help(ls: CpyLanguageServer, params: lsp.SignatureHelpParams):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        source, tokens, parsed, analyzer, _, _ = _get_bundle(ls, uri)

        paren_depth = 0
        active_param = 0
        func_name = None
        for tok in reversed(tokens):
            if tok is None:
                continue
            t_line = tok.line - 1
            t_end = tok.column - 1 + len(tok.value or "")
            if t_line > line or (t_line == line and t_end > col):
                continue
            if t_line < line or (t_line == line and t_end <= col):
                if tok.type == TokenType.RPAREN:
                    paren_depth += 1
                elif tok.type == TokenType.LPAREN:
                    if paren_depth == 0:
                        if tok.column >= 2:
                            prev = None
                            for t2 in tokens:
                                if t2 is None:
                                    continue
                                if t2.line == tok.line and t2.column < tok.column:
                                    if prev is None or t2.column > prev.column:
                                        prev = t2
                            if (
                                prev
                                and prev.type == TokenType.IDENTIFIER
                                or (
                                    prev
                                    and prev.type == TokenType.KEYWORD
                                    and prev.value in _BUILTIN_FUNC_PARAMS
                                )
                            ):
                                func_name = prev.value
                        break
                    paren_depth -= 1
                elif tok.type == TokenType.COMMA and paren_depth == 0:
                    active_param += 1

        if func_name is None:
            return None

        params_map = None
        sig_label = None

        if func_name in _BUILTIN_FUNC_PARAMS:
            params_map = _BUILTIN_FUNC_PARAMS[func_name]
            sig_label = _BUILTIN_FUNCS.get(func_name, f"{func_name}(...)")
        elif analyzer:
            sym = analyzer.globals.lookup(func_name)
            if (
                sym
                and sym.kind == "function"
                and hasattr(sym, "node")
                and sym.node is not None
            ):
                node = sym.node
                if hasattr(node, "params"):
                    params_map = node.params
                    const_params = getattr(node, "const_params", None) or set()
                    parts = []
                    for pname, ptype in params_map.items():
                        parts.append(
                            f"({pname}): {ptype}"
                            if pname in const_params
                            else f"{pname}: {ptype}"
                        )
                    sig_label = (
                        f"{func_name}({', '.join(parts)}) -> {node.rettype or 'void'}"
                    )

        if params_map is None:
            return None

        sig_params = []
        for pname, ptype in params_map.items():
            sig_params.append(
                lsp.ParameterInformation(
                    label=pname,
                    documentation=f"{pname}: {ptype}",
                )
            )

        sig = lsp.SignatureInformation(
            label=sig_label,
            parameters=sig_params,
        )
        return lsp.SignatureHelp(
            signatures=[sig],
            active_signature=0,
            active_parameter=active_param,
        )
    except Exception:
        logger.error(f"signature_help error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_FORMATTING)
@server.thread()
def formatting(ls: CpyLanguageServer, params: lsp.DocumentFormattingParams):
    try:
        uri = params.text_document.uri
        doc = ls.workspace.get_text_document(uri)
        t0 = time.time()
        result = format_source(doc.source, tab_size=params.options.tab_size or 4)
        if result.errors:
            logger.info(
                f"[format] {os.path.basename(_uri_to_path(uri))}: "
                f"{len(result.errors)} error(s): {result.errors[0]}"
            )
            return None
        lines = doc.source.splitlines()
        if not lines:
            end = lsp.Position(line=0, character=0)
        else:
            end = lsp.Position(line=len(lines) - 1, character=len(lines[-1]))
        rng = lsp.Range(start=lsp.Position(line=0, character=0), end=end)
        elapsed = time.time() - t0
        logger.info(
            f"[format] {os.path.basename(_uri_to_path(uri))}: "
            f"{len(result.formatted)} chars in {elapsed * 1000:.0f}ms"
        )
        return [lsp.TextEdit(range=rng, new_text=result.formatted)]
    except Exception:
        logger.error(f"formatting error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
@server.thread()
def document_symbols(ls: CpyLanguageServer, params: lsp.DocumentSymbolParams):
    try:
        uri = params.text_document.uri
        t0 = time.time()
        _, _, parsed, _, _, _ = _get_bundle(ls, uri)
        symbols = []
        for node in parsed:
            tok = getattr(node, "_token", None)
            loc = lsp.Location(
                uri=uri,
                range=lsp.Range(
                    start=lsp.Position(
                        line=(tok.line - 1 if tok else 0),
                        character=(tok.column - 1 if tok else 0),
                    ),
                    end=lsp.Position(
                        line=(tok.line - 1 if tok else 0),
                        character=(tok.column - 1 + len(tok.value or "") if tok else 0),
                    ),
                ),
            )
            if isinstance(node, FuncDef):
                sig = (
                    f"({_fmt_params(node.params, getattr(node, 'const_params', None))})"
                )
                symbols.append(
                    lsp.SymbolInformation(
                        name=f"{node.name}{sig}",
                        kind=lsp.SymbolKind.Function,
                        location=loc,
                    )
                )
            elif isinstance(node, StructDef):
                symbols.append(
                    lsp.SymbolInformation(
                        name=node.name,
                        kind=lsp.SymbolKind.Struct,
                        location=loc,
                    )
                )
            elif isinstance(node, ClassDef):
                symbols.append(
                    lsp.SymbolInformation(
                        name=node.name,
                        kind=lsp.SymbolKind.Class,
                        location=loc,
                    )
                )
                for m in node.methods:
                    mtok = getattr(m, "_token", None)
                    mloc = lsp.Location(
                        uri=uri,
                        range=lsp.Range(
                            start=lsp.Position(
                                line=(mtok.line - 1 if mtok else 0),
                                character=(mtok.column - 1 if mtok else 0),
                            ),
                            end=lsp.Position(
                                line=(mtok.line - 1 if mtok else 0),
                                character=(
                                    mtok.column - 1 + len(mtok.value or "")
                                    if mtok
                                    else 0
                                ),
                            ),
                        ),
                    )
                    sig = f"({_fmt_params(m.params, getattr(m, 'const_params', None))})"
                    symbols.append(
                        lsp.SymbolInformation(
                            name=f"{node.name}.{m.name}{sig}",
                            kind=lsp.SymbolKind.Method,
                            location=mloc,
                        )
                    )
            elif isinstance(node, EnumDef):
                symbols.append(
                    lsp.SymbolInformation(
                        name=node.name,
                        kind=lsp.SymbolKind.Enum,
                        location=loc,
                    )
                )
            elif isinstance(node, VarDecl) and node.is_const:
                symbols.append(
                    lsp.SymbolInformation(
                        name=f"const {node.name}",
                        kind=lsp.SymbolKind.Constant,
                        location=loc,
                    )
                )
        elapsed = time.time() - t0
        logger.info(
            f"[symbols] {os.path.basename(_uri_to_path(uri))}: "
            f"{len(symbols)} symbol(s) in {elapsed * 1000:.0f}ms"
        )
        return symbols
    except Exception:
        logger.error(f"document_symbols error:\n{traceback.format_exc()}")
        return []


def _iter_var_decls(stmt):
    """Yield VarDecl nodes nested inside a statement (if/while/switch/try/for)."""
    if isinstance(stmt, VarDecl):
        yield stmt
        return
    if isinstance(stmt, If):
        for s in stmt.body:
            yield from _iter_var_decls(s)
        for s in stmt.orelse or []:
            yield from _iter_var_decls(s)
        return
    if isinstance(stmt, While):
        for s in stmt.body:
            yield from _iter_var_decls(s)
        return
    if isinstance(stmt, Switch):
        for _, body in stmt.cases:
            for s in body:
                yield from _iter_var_decls(s)
        return
    if isinstance(stmt, Try):
        for s in stmt.body:
            yield from _iter_var_decls(s)
        for h in stmt.handlers:
            for s in h.body:
                yield from _iter_var_decls(s)
        return
    if isinstance(stmt, dict) and stmt.get("type") == "for":
        for s in stmt.get("body") or []:
            yield from _iter_var_decls(s)


def _find_local_decl(func, name):
    for stmt in getattr(func, "body", None) or []:
        for decl in _iter_var_decls(stmt):
            if decl.name == name:
                return decl
    return None


def _find_param_token(tokens, func, name):
    """Resolve a parameter name to its declaration token by scanning the
    function signature (identifier tokens directly followed by `:`)."""
    if name not in func.params:
        return None
    sig_line = getattr(func._token, "line", None)
    if sig_line is None:
        return None
    order = list(func.params)
    idx = {o: i for i, o in enumerate(order)}
    seen = 0
    within = False
    for i, t in enumerate(tokens):
        if t is None or t.line != sig_line or t.type != TokenType.IDENTIFIER:
            continue
        if not within:
            if t.value == func.name:
                within = True
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if nxt is not None and nxt.type == TokenType.COLON:
            if idx.get(t.value, -1) == seen:
                return t
            seen += 1
    return None


def _location_for_file(path):
    """An lsp.Location pointing at the first line of ``path``."""
    uri = pathlib.Path(path).as_uri()
    start = lsp.Position(line=0, character=0)
    return lsp.Location(uri=uri, range=lsp.Range(start=start, end=start))


def _import_resolve_target(source, line, col):
    """If the cursor is inside the quoted path of an `import "..."`, resolve and
    return the relative target-file path; else None.

    The quoted path is just concatenated onto the source file's directory
    (mirroring `_resolve_module_path`), so `subdir/lib.cpy` resolves relative to
    the importing file. Builtin C-library names (no `/.`) have no local target
    and return None.
    """
    ctx = _import_string_context(source, line, col)
    if ctx is None:
        return None
    partial, start_col, end_col = ctx
    # Resolve against the full module path typed in the quotes (the cursor may
    # be mid-name), not just the pre-cursor slice.
    lines = source.splitlines()
    text = lines[line]
    content = (
        text[start_col + 1 : end_col] if end_col > start_col + 1 else partial
    ).strip()
    if not content:
        return None
    root = content.rstrip("/")
    # A bare module name (no extension, e.g. a C library like `stdio` or
    # `sys/time`) has no local project file — don't fabricate a target. Only
    # explicit file paths (a `.` in the final segment) or existing
    # files/dirs are jumped to.
    last = root.rsplit("/", 1)[-1]
    if "." not in last and "/" not in root:
        return None
    return root


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
@server.thread()
def definition(ls: CpyLanguageServer, params: lsp.DefinitionParams):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        bundle = _get_bundle(ls, uri)
        source, tokens, parsed, analyzer, _, _ = bundle
        # If the cursor is inside the quoted path of an `import "..."`, jump to
        # the target file (still works while the string is unterminated, since
        # those lines never reach the analyzer's parsed AST).
        import_path = _import_resolve_target(source, line, col)
        if import_path is not None:
            filepath = _uri_to_path(uri)
            import_path = os.path.normpath(
                os.path.join(os.path.dirname(filepath) or ".", import_path)
            )
            if os.path.exists(import_path):
                return _location_for_file(import_path)
        tok = _find_token_at(tokens, line, col)
        if not tok or not tok.value:
            return None
        word = tok.value
        target = None
        target_uri = uri
        if analyzer is not None:
            name = word
            base = _dot_context(tokens, line, col)
            if base is not None:
                name = f"{base}.{word}"
            sym = analyzer.globals.lookup(name)
            if sym is not None and getattr(sym, "node", None) is not None:
                target = sym.node
                node_file = getattr(target, "_source_file", None)
                if node_file and node_file != _uri_to_path(uri):
                    target_uri = pathlib.Path(node_file).as_uri()
                else:
                    import_node = getattr(sym, "_import_node", None)
                    if import_node is None and analyzer:
                        for imp in getattr(analyzer, "_imports", []):
                            sub_ast = getattr(imp, "sub_ast", None)
                            if sub_ast and hasattr(target, "name"):
                                for n in sub_ast:
                                    if getattr(n, "name", None) == target.name:
                                        src = getattr(imp, "src_file", None)
                                        if src:
                                            target_uri = pathlib.Path(src).as_uri()
                                        break
        if target is None:
            func = _find_containing_function(parsed, line + 1)
            if func is not None:
                target = _find_local_decl(func, word)
                if target is None:
                    target = _find_param_token(tokens, func, word)
        if target is None:
            return None
        t = getattr(target, "_token", None)
        if t is None and getattr(target, "type", None) is not None:
            t = target  # raw lexer token (e.g. a parameter declaration)
        if t is None or t.line is None:
            return None
        start = lsp.Position(line=t.line - 1, character=t.column - 1)
        end = lsp.Position(line=t.line - 1, character=t.column - 1 + len(t.value or ""))
        return lsp.Location(uri=target_uri, range=lsp.Range(start=start, end=end))
    except Exception:
        logger.error(f"definition error:\n{traceback.format_exc()}")
        return None


def _walk_all_tokens(tokens, word, current_line, current_col):
    """Find all occurrences of `word` in tokens, yielding (line, col, length)."""
    if not word:
        return
    for tok in tokens:
        if tok is None or tok.value != word:
            continue
        yield (tok.line - 1, tok.column - 1, len(tok.value or ""))


@server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_HIGHLIGHT)
@server.thread()
def document_highlight(ls: CpyLanguageServer, params: lsp.DocumentHighlightParams):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        _, tokens, parsed, analyzer, _, _ = _get_bundle(ls, uri)
        tok = _find_token_at(tokens, line, col)
        if not tok or not tok.value:
            return []
        word = tok.value
        if word in _KEYWORDS or word in _TYPES or word in _BUILTIN_FUNCS:
            return []
        results = []
        for t_line, t_col, t_len in _walk_all_tokens(tokens, word, line, col):
            rng = lsp.Range(
                start=lsp.Position(line=t_line, character=t_col),
                end=lsp.Position(line=t_line, character=t_col + t_len),
            )
            results.append(
                lsp.DocumentHighlight(range=rng, kind=lsp.DocumentHighlightKind.Read)
            )
        return results
    except Exception:
        logger.error(f"document_highlight error:\n{traceback.format_exc()}")
        return []


_CALL_WALK_ATTRS = ("body", "orelse", "handlers", "items", "cases")
_EXPR_WALK_ATTRS = (
    "expr",
    "value",
    "cond",
    "target",
    "callee",
    "obj",
    "operand",
    "index",
)
_FOLD_KIND = {}
for _cls in (FuncDef, ClassDef, StructDef, EnumDef):
    _FOLD_KIND[_cls] = lsp.FoldingRangeKind.Region


def _block_end_line(stmt, cur=0):
    """Deepest source line spanned by a statement's body (for folding)."""
    top = max(cur, _stmt_line(stmt) or 0)
    for child in _stmt_children(stmt):
        top = max(top, _block_end_line(child, top))
    return top


def _iter_calls_in_stmt(stmt, _seen=None):
    """Yield every Call node nested inside a statement (bodies, conditions,
    initializers, call arguments, attribute chains ...)."""
    if _seen is None:
        _seen = set()
    s_id = id(stmt)
    if s_id in _seen:
        return
    _seen.add(s_id)
    if isinstance(stmt, (list, tuple)):
        for s in stmt:
            yield from _iter_calls_in_stmt(s, _seen)
        return
    if isinstance(stmt, dict):
        for v in stmt.values():
            yield from _iter_calls_in_stmt(v, _seen)
        return
    if isinstance(stmt, Call):
        yield stmt
    for attr in _CALL_WALK_ATTRS:
        v = getattr(stmt, attr, False)
        if v is not None:
            yield from _iter_calls_in_stmt(v, _seen)
    for attr in _EXPR_WALK_ATTRS:
        v = getattr(stmt, attr, False)
        if v is not None and hasattr(v, "_token"):
            if id(v) != s_id:
                yield from _iter_calls_in_stmt(v, _seen)


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (used for 'did you mean' quick fixes)."""
    if a == b:
        return 0
    n, m = len(a), len(b)
    if not n:
        return m
    if not m:
        return n
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cur[j] = min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (a[i - 1] != b[j - 1]),
            )
        prev = cur
    return prev[m]


def _similar_names(word, candidates, max_results=3):
    out = []
    for cand in candidates:
        if cand == word:
            continue
        if cand.startswith("_") and not word.startswith("_"):
            continue
        if "." in cand or cand in _KEYWORD_DESC:
            continue
        d = _edit_distance(word, cand)
        if d <= 1 or (len(word) >= 4 and d <= 2):
            out.append((d, cand))
    out.sort()
    return [c for _, c in out[:max_results]]


def _candidate_names(analyzer, parsed, line):
    names: set = set(_KEYWORDS) | set(_TYPES) | set(_BUILTIN_FUNCS)
    if analyzer:
        names.update(n for n in analyzer.globals.symbols if "." not in n)
    func = _find_containing_function(parsed, line)
    if func is not None:
        names.update(func.params)
        names.update(_collect_locals(func, line))
    return names


def _rng_from_token(tok):
    if tok is None:
        return lsp.Range(
            start=lsp.Position(line=0, character=0),
            end=lsp.Position(line=0, character=0),
        )
    line = tok.line - 1
    col = tok.column - 1
    return lsp.Range(
        start=lsp.Position(line=line, character=col),
        end=lsp.Position(line=line, character=col + len(tok.value or "")),
    )


@server.feature(lsp.TEXT_DOCUMENT_REFERENCES)
@server.thread()
def references(ls: CpyLanguageServer, params: lsp.ReferenceParams):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        _, tokens, _, analyzer, _, _ = _get_bundle(ls, uri)
        tok = _find_token_at(tokens, line, col)
        if (
            not tok
            or not tok.value
            or tok.type
            not in (
                TokenType.IDENTIFIER,
                TokenType.KEYWORD,
            )
        ):
            return None
        word = tok.value
        if word in _TYPES or word in _BUILTIN_FUNCS or word in _KEYWORD_DESC:
            return None
        results = []
        for t_line, t_col, t_len in _walk_all_tokens(tokens, word, line, col):
            results.append(
                lsp.Location(
                    uri=uri,
                    range=lsp.Range(
                        start=lsp.Position(line=t_line, character=t_col),
                        end=lsp.Position(line=t_line, character=t_col + t_len),
                    ),
                )
            )
        t0 = time.time()
        logger.info(
            f"[references] {os.path.basename(_uri_to_path(uri))} `{word}`: "
            f"{len(results)} ref(s) in {(time.time() - t0) * 1000:.0f}ms"
        )
        return results or None
    except Exception:
        logger.error(f"references error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_PREPARE_RENAME)
@server.thread()
def prepare_rename(ls: CpyLanguageServer, params: lsp.PrepareRenameParams):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        _, tokens, _, _, _, _ = _get_bundle(ls, uri)
        tok = _find_token_at(tokens, line, col)
        if not tok or not tok.value or tok.type != TokenType.IDENTIFIER:
            return None
        word = tok.value
        if word in _KEYWORDS or word in _TYPES or word in _BUILTIN_FUNCS:
            return None
        return lsp.Range(
            start=lsp.Position(line=tok.line - 1, character=tok.column - 1),
            end=lsp.Position(line=tok.line - 1, character=tok.column - 1 + len(word)),
        )
    except Exception:
        logger.error(f"prepare_rename error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_RENAME)
@server.thread()
def rename(ls: CpyLanguageServer, params: lsp.RenameParams):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        new_name = params.new_name
        _, tokens, _, _, _, _ = _get_bundle(ls, uri)
        tok = _find_token_at(tokens, line, col)
        if not tok or not tok.value or tok.type != TokenType.IDENTIFIER:
            return None
        word = tok.value
        if word in _KEYWORDS or word in _TYPES or word in _BUILTIN_FUNCS:
            return None
        edits = []
        for t_line, t_col, t_len in _walk_all_tokens(tokens, word, line, col):
            edits.append(
                lsp.TextEdit(
                    range=lsp.Range(
                        start=lsp.Position(line=t_line, character=t_col),
                        end=lsp.Position(line=t_line, character=t_col + t_len),
                    ),
                    new_text=new_name,
                )
            )
        if not edits:
            return None
        return lsp.WorkspaceEdit(changes={uri: edits})
    except Exception:
        logger.error(f"rename error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_IMPLEMENTATION)
@server.thread()
def implementation(ls: CpyLanguageServer, params: lsp.ImplementationParams):
    try:
        return definition(ls, params)
    except Exception:
        logger.error(f"implementation error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_TYPE_DEFINITION)
@server.thread()
def type_definition(ls: CpyLanguageServer, params: lsp.TypeDefinitionParams):
    try:
        return definition(ls, params)
    except Exception:
        logger.error(f"type_definition error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_FOLDING_RANGE)
@server.thread()
def folding_range(ls: CpyLanguageServer, params: lsp.FoldingRangeParams):
    try:
        uri = params.text_document.uri
        _, _, parsed, _, _, _ = _get_bundle(ls, uri)
        out = []
        for node in parsed:
            start = _stmt_line(node)
            if start is None:
                continue
            end = _block_end_line(node, start)
            if end <= start:
                continue
            kind = _FOLD_KIND.get(type(node)) or (
                lsp.FoldingRangeKind.Region
                if isinstance(node, (If, While, Switch, Try))
                else None
            )
            out.append(
                lsp.FoldingRange(
                    start_line=start - 1,
                    start_character=0,
                    end_line=end - 1,
                    end_character=0,
                    kind=kind,
                )
            )
            for child in _stmt_children(node):
                cs = _stmt_line(child)
                if cs is None:
                    continue
                ce = _block_end_line(child, cs)
                if ce > cs:
                    out.append(
                        lsp.FoldingRange(
                            start_line=cs - 1,
                            start_character=0,
                            end_line=ce - 1,
                            end_character=0,
                            kind=lsp.FoldingRangeKind.Region,
                        )
                    )
        return out or None
    except Exception:
        logger.error(f"folding_range error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_INLAY_HINT)
@server.thread()
def inlay_hints(ls: CpyLanguageServer, params: lsp.InlayHintParams):
    try:
        uri = params.text_document.uri
        _, _, parsed, analyzer, _, _ = _get_bundle(ls, uri)
        if analyzer is None:
            return None
        hints = []
        for node in parsed:
            for call in _iter_calls_in_stmt(node):
                if not isinstance(call.callee, Variable):
                    continue
                sym = analyzer.globals.lookup(call.callee.name)
                if not sym or sym.kind != "function":
                    continue
                fnode = getattr(sym, "node", None)
                params_map = getattr(fnode, "params", None) or {}
                pnames = list(params_map)
                n = 0
                for arg in call.args:
                    if n >= len(pnames):
                        break
                    if isinstance(arg, Variable):
                        n += 1
                        continue
                    tok = getattr(arg, "_token", None)
                    if tok is None:
                        n += 1
                        continue
                    hints.append(
                        lsp.InlayHint(
                            position=lsp.Position(
                                line=tok.line - 1, character=tok.column - 1
                            ),
                            label=f"{pnames[n]}:",
                            kind=lsp.InlayHintKind.Parameter,
                            padding_right=True,
                        )
                    )
                    n += 1
        return hints or None
    except Exception:
        logger.error(f"inlay_hints error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: CpyLanguageServer, params: lsp.DidSaveTextDocumentParams):
    """Cross-file invalidation: when an imported file is saved, drop the
    cached analysis of every document that imports it and re-run it."""
    try:
        saved = _uri_to_path(params.text_document.uri)
        cache = getattr(ls, "_analysis_cache", None) or {}
        for uri, bundle in list(cache.items()):
            if uri == params.text_document.uri:
                continue
            deps = set()
            for imp in _iter_import_nodes(bundle[2]):
                deps.add(getattr(imp, "src_file", None))
                deps.add(getattr(imp, "_source_file", None))
            if saved in deps:
                cache.pop(uri, None)
                _schedule_analysis(ls, uri, _ANALYSIS_DEBOUNCE)
    except Exception:
        logger.error(f"did_save error:\n{traceback.format_exc()}")


def _ranges_intersect(r1: lsp.Range, r2: lsp.Range) -> bool:
    if r2 is None:
        return True
    if r1 is None:
        return False
    return not (
        r1.end.line < r2.start.line
        or r2.end.line < r1.start.line
        or (r1.end.line == r2.start.line and r1.end.character < r2.start.character)
        or (r2.end.line == r1.start.line and r2.end.character < r1.start.character)
    )


@server.feature(
    lsp.TEXT_DOCUMENT_CODE_ACTION,
    lsp.CodeActionOptions(
        code_action_kinds=[
            lsp.CodeActionKind.QuickFix,
            lsp.CodeActionKind.Refactor,
            lsp.CodeActionKind.RefactorExtract,
            lsp.CodeActionKind.RefactorInline,
            lsp.CodeActionKind.SourceOrganizeImports,
        ]
    ),
)
@server.thread()
def code_action(ls: CpyLanguageServer, params: lsp.CodeActionParams):
    try:
        import re

        uri = params.text_document.uri
        _, tokens, parsed, analyzer, diagnostics, _ = _get_bundle(ls, uri)
        if not diagnostics:
            return None
        doc = ls.workspace.get_text_document(uri)
        actions = []
        for d in diagnostics:
            if not d.code or not d.range:
                continue
            if not _ranges_intersect(d.range, params.range):
                continue
            line0 = d.range.start.line
            text = d.message or ""
            if d.code == "W1001":
                m = re.search(r"local `(\w+)` is assigned but never used", text)
                if m:
                    name = m.group(1)
                    actions.append(
                        lsp.CodeAction(
                            title=f"Rename to `_{name}`",
                            kind=lsp.CodeActionKind.QuickFix,
                            edit=lsp.WorkspaceEdit(
                                changes={
                                    uri: [
                                        lsp.TextEdit(range=d.range, new_text=f"_{name}")
                                    ]
                                }
                            ),
                        )
                    )
            elif d.code in ("E0001", "E1001"):
                m = re.search(r"undeclared identifier `(\w+)`", text)
                if not m:
                    m = re.search(r"identifier `(\w+)`", text)
                if m and analyzer:
                    word = m.group(1)
                    for cand in _similar_names(
                        word, _candidate_names(analyzer, parsed, line0 + 1)
                    ):
                        actions.append(
                            lsp.CodeAction(
                                title=f"Did you mean `{cand}`?",
                                kind=lsp.CodeActionKind.QuickFix,
                                edit=lsp.WorkspaceEdit(
                                    changes={
                                        uri: [
                                            lsp.TextEdit(range=d.range, new_text=cand)
                                        ]
                                    }
                                ),
                            )
                        )
            elif d.code == "W1002":
                m = re.search(r"missing `return` in function `(\w+)`", text)
                if m:
                    func = _find_containing_function(parsed, line0 + 1)
                    end_line = 0
                    for stmt in getattr(func, "body", None) or []:
                        l = _stmt_line(stmt)
                        if l:
                            end_line = max(end_line, l)
                    if func is not None and end_line:
                        tail_range = lsp.Range(
                            start=lsp.Position(line=end_line - 1, character=0),
                            end=lsp.Position(line=end_line - 1, character=0),
                        )
                        indent = "    "
                        actions.append(
                            lsp.CodeAction(
                                title="Append `return 0`",
                                kind=lsp.CodeActionKind.QuickFix,
                                edit=lsp.WorkspaceEdit(
                                    changes={
                                        uri: [
                                            lsp.TextEdit(
                                                range=tail_range,
                                                new_text=f"\n{indent}return 0",
                                            )
                                        ]
                                    }
                                ),
                            )
                        )
        return actions or None
    except Exception:
        logger.error(f"code_action error:\n{traceback.format_exc()}")
        return None


if __name__ == "__main__":
    start_server(server)


@server.feature(lsp.TEXT_DOCUMENT_PREPARE_CALL_HIERARCHY)
@server.thread()
def prepare_call_hierarchy(
    ls: CpyLanguageServer, params: lsp.CallHierarchyPrepareParams
):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        _, tokens, parsed, analyzer, _, _ = _get_bundle(ls, uri)
        tok = _find_token_at(tokens, line, col)
        if not tok or not tok.value or tok.type != TokenType.IDENTIFIER:
            return None
        word = tok.value
        for node in parsed:
            if isinstance(node, FuncDef) and node.name == word:
                tok_node = getattr(node, "_token", None) or tok
                rng = lsp.Range(
                    start=lsp.Position(
                        line=tok_node.line - 1, character=tok_node.column - 1
                    ),
                    end=lsp.Position(
                        line=tok_node.line - 1,
                        character=tok_node.column - 1 + len(tok_node.value or ""),
                    ),
                )
                return [
                    lsp.CallHierarchyItem(
                        name=node.name,
                        kind=lsp.SymbolKind.Function,
                        uri=uri,
                        range=rng,
                        selection_range=rng,
                    )
                ]
        return None
    except Exception:
        logger.error(f"prepare_call_hierarchy error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.CALL_HIERARCHY_INCOMING_CALLS)
@server.thread()
def incoming_calls(ls: CpyLanguageServer, params: lsp.CallHierarchyIncomingCallsParams):
    try:
        uri = params.item.uri
        func_name = params.item.name
        _, tokens, parsed, analyzer, _, _ = _get_bundle(ls, uri)
        results = []
        for node in parsed:
            if isinstance(node, FuncDef):
                for call in _iter_calls_in_stmt(node):
                    if (
                        isinstance(call.callee, Variable)
                        and call.callee.name == func_name
                    ):
                        tok = getattr(call.callee, "_token", None)
                        if tok:
                            rng = lsp.Range(
                                start=lsp.Position(
                                    line=tok.line - 1, character=tok.column - 1
                                ),
                                end=lsp.Position(
                                    line=tok.line - 1,
                                    character=tok.column - 1 + len(tok.value or ""),
                                ),
                            )
                            ntrng = lsp.Range(
                                start=lsp.Position(
                                    line=(getattr(node, "_token") or tok).line - 1,
                                    character=(getattr(node, "_token") or tok).column
                                    - 1,
                                ),
                                end=lsp.Position(
                                    line=(getattr(node, "_token") or tok).line - 1,
                                    character=(getattr(node, "_token") or tok).column
                                    - 1
                                    + len(node.name),
                                ),
                            )
                            results.append(
                                lsp.CallHierarchyIncomingCall(
                                    from_=lsp.CallHierarchyItem(
                                        name=node.name,
                                        kind=lsp.SymbolKind.Function,
                                        uri=uri,
                                        range=ntrng,
                                        selection_range=ntrng,
                                    ),
                                    from_ranges=[rng],
                                )
                            )
        return results or None
    except Exception:
        logger.error(f"incoming_calls error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.CALL_HIERARCHY_OUTGOING_CALLS)
@server.thread()
def outgoing_calls(ls: CpyLanguageServer, params: lsp.CallHierarchyOutgoingCallsParams):
    try:
        uri = params.item.uri
        func_name = params.item.name
        _, tokens, parsed, analyzer, _, _ = _get_bundle(ls, uri)
        results = []
        for node in parsed:
            if isinstance(node, FuncDef) and node.name == func_name:
                for call in _iter_calls_in_stmt(node):
                    if isinstance(call.callee, Variable):
                        tok = getattr(call.callee, "_token", None)
                        if tok:
                            rng = lsp.Range(
                                start=lsp.Position(
                                    line=tok.line - 1, character=tok.column - 1
                                ),
                                end=lsp.Position(
                                    line=tok.line - 1,
                                    character=tok.column - 1 + len(tok.value or ""),
                                ),
                            )
                            results.append(
                                lsp.CallHierarchyOutgoingCall(
                                    to=lsp.CallHierarchyItem(
                                        name=call.callee.name,
                                        kind=lsp.SymbolKind.Function,
                                        uri=uri,
                                        range=rng,
                                        selection_range=rng,
                                    ),
                                    from_ranges=[rng],
                                )
                            )
        return results or None
    except Exception:
        logger.error(f"outgoing_calls error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TEXT_DOCUMENT_PREPARE_TYPE_HIERARCHY)
@server.thread()
def prepare_type_hierarchy(
    ls: CpyLanguageServer, params: lsp.TypeHierarchyPrepareParams
):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        _, tokens, parsed, analyzer, _, _ = _get_bundle(ls, uri)
        tok = _find_token_at(tokens, line, col)
        if not tok or not tok.value or tok.type != TokenType.IDENTIFIER:
            return None
        word = tok.value
        for node in parsed:
            if isinstance(node, ClassDef) and node.name == word:
                tok_node = getattr(node, "_token", None) or tok
                rng = lsp.Range(
                    start=lsp.Position(
                        line=tok_node.line - 1, character=tok_node.column - 1
                    ),
                    end=lsp.Position(
                        line=tok_node.line - 1,
                        character=tok_node.column - 1 + len(tok_node.value or ""),
                    ),
                )
                return [
                    lsp.TypeHierarchyItem(
                        name=node.name,
                        kind=lsp.SymbolKind.Class,
                        uri=uri,
                        range=rng,
                        selection_range=rng,
                    )
                ]
        return None
    except Exception:
        logger.error(f"prepare_type_hierarchy error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TYPE_HIERARCHY_SUPERTYPES)
@server.thread()
def supertypes(ls: CpyLanguageServer, params: lsp.TypeHierarchySupertypesParams):
    try:
        uri = params.item.uri
        class_name = params.item.name
        _, tokens, parsed, analyzer, _, _ = _get_bundle(ls, uri)
        results = []
        for node in parsed:
            if (
                isinstance(node, ClassDef)
                and node.name == class_name
                and getattr(node, "base", None)
            ):
                base_name = node.base
                for bnode in parsed:
                    if isinstance(bnode, ClassDef) and bnode.name == base_name:
                        btok = getattr(bnode, "_token", None)
                        if btok:
                            rng = lsp.Range(
                                start=lsp.Position(
                                    line=btok.line - 1, character=btok.column - 1
                                ),
                                end=lsp.Position(
                                    line=btok.line - 1,
                                    character=btok.column - 1 + len(btok.value or ""),
                                ),
                            )
                            results.append(
                                lsp.TypeHierarchyItem(
                                    name=bnode.name,
                                    kind=lsp.SymbolKind.Class,
                                    uri=uri,
                                    range=rng,
                                    selection_range=rng,
                                )
                            )
        return results or None
    except Exception:
        logger.error(f"supertypes error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.TYPE_HIERARCHY_SUBTYPES)
@server.thread()
def subtypes(ls: CpyLanguageServer, params: lsp.TypeHierarchySubtypesParams):
    try:
        uri = params.item.uri
        class_name = params.item.name
        _, tokens, parsed, analyzer, _, _ = _get_bundle(ls, uri)
        results = []
        for node in parsed:
            if isinstance(node, ClassDef) and getattr(node, "base", None) == class_name:
                tok_node = getattr(node, "_token", None)
                if tok_node:
                    rng = lsp.Range(
                        start=lsp.Position(
                            line=tok_node.line - 1, character=tok_node.column - 1
                        ),
                        end=lsp.Position(
                            line=tok_node.line - 1,
                            character=tok_node.column - 1 + len(tok_node.value or ""),
                        ),
                    )
                    results.append(
                        lsp.TypeHierarchyItem(
                            name=node.name,
                            kind=lsp.SymbolKind.Class,
                            uri=uri,
                            range=rng,
                            selection_range=rng,
                        )
                    )
        return results or None
    except Exception:
        logger.error(f"subtypes error:\n{traceback.format_exc()}")
        return None


@server.feature(lsp.WORKSPACE_SYMBOL)
@server.thread()
def workspace_symbol(ls: CpyLanguageServer, params: lsp.WorkspaceSymbolParams):
    try:
        query = (params.query or "").lower()
        results = []
        for uri, bundle in getattr(ls, "_analysis_cache", {}).items():
            try:
                _, tokens, parsed, analyzer, _, _ = bundle
                for node in parsed:
                    if isinstance(node, (FuncDef, ClassDef, StructDef, EnumDef)):
                        name = node.name
                        if query and query not in name.lower():
                            continue
                        tok = getattr(node, "_token", None)
                        if tok:
                            if isinstance(node, FuncDef):
                                kind = lsp.SymbolKind.Function
                            elif isinstance(node, ClassDef):
                                kind = lsp.SymbolKind.Class
                            elif isinstance(node, StructDef):
                                kind = lsp.SymbolKind.Struct
                            elif isinstance(node, EnumDef):
                                kind = lsp.SymbolKind.Enum
                            else:
                                kind = lsp.SymbolKind.Variable
                            results.append(
                                lsp.WorkspaceSymbol(
                                    name=name,
                                    kind=kind,
                                    location=lsp.Location(
                                        uri=uri,
                                        range=lsp.Range(
                                            start=lsp.Position(
                                                line=tok.line - 1,
                                                character=tok.column - 1,
                                            ),
                                            end=lsp.Position(
                                                line=tok.line - 1,
                                                character=tok.column
                                                - 1
                                                + len(tok.value or ""),
                                            ),
                                        ),
                                    ),
                                )
                            )
            except Exception:
                continue
        return results or None
    except Exception:
        logger.error(f"workspace_symbol error:\n{traceback.format_exc()}")
        return None
