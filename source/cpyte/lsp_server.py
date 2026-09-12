import asyncio
import logging
import os
import pathlib
import time
import traceback
from urllib.parse import unquote, urlparse

from lsprotocol import types as lsp
from pygls.cli import start_server
from pygls.lsp.server import LanguageServer

from .astparse import (
    Assign,
    Call,
    ClassDef,
    EnumDef,
    FuncDef,
    If,
    Import,
    NewExpr,
    ParseError,
    StructDef,
    Switch,
    Try,
    VarDecl,
    Variable,
    While,
    parse_file,
)
from .formatter import format_source
from .lexar import Lexer, LexerError, TokenType
from .semantic_analasis import SemanticAnalyzer
from .clib import _BUILTIN_LIB_HEADERS

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
]

_TYPES = [
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
    "ccode": "Embed raw C code block",
    "llvm": "Embed raw LLVM IR block",
    "asm": "Inline assembly block",
    "unsafe": "Mark code as unsafe (no bounds checking)",
    "print": "Write a string representation to stdout",
    "input": "Read a line of text from stdin",
    "input_str": "Read a line of text from stdin and return it as a string",
    "input_big": "Read a line of text from stdin and return it as an arbitrary-precision big integer",
    "free": "Deallocate heap memory",
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
    "size_t": "Unsigned platform-dependent size type",
    "dynamic": "Dynamically-typed value (resolved at runtime)",
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
    mainpie. Safe to call repeatedly (global registry de-dupes by package)."""
    if not workspace_root:
        return
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
                source, filepath=filepath, workspace_root=workspace_root
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
            rng = lsp.Range(
                start=lsp.Position(line=d.token.line - 1, character=d.token.column - 1),
                end=lsp.Position(
                    line=d.token.line - 1,
                    character=d.token.column - 1 + len(d.token.value or ""),
                ),
            )
        else:
            rng = lsp.Range(
                start=lsp.Position(line=0, character=0),
                end=lsp.Position(line=0, character=0),
            )
        diagnostics.append(
            lsp.Diagnostic(
                message=d.message,
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
    bundle = await asyncio.to_thread(
        _analyze_bundle, source, filepath=filepath, workspace_root=workspace_root
    )
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


def _collect_imported_symbols(parsed):
    """Collect public symbols (functions/structs/classes/enums/constants) re-exported
    through `import` statements so completion can offer them even though they live in
    another file/package."""
    out = {}
    for node in parsed:
        if isinstance(node, Import) and getattr(node, "symbols", None):
            for fname, _sig in node.symbols:
                out.setdefault(fname, ("function", ""))
        for sub in getattr(node, "sub_ast", None) or []:
            if isinstance(sub, Import) and getattr(sub, "symbols", None):
                for fname, _sig in sub.symbols:
                    out.setdefault(fname, ("function", ""))
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
    if workspace_root:
        bases.append(os.path.join(workspace_root, ".cpm", "modules"))

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


class CpyLanguageServer(LanguageServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace_root: str | None = None

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
    except Exception:
        logger.error(f"did_close error:\n{traceback.format_exc()}")


@server.feature(
    lsp.TEXT_DOCUMENT_COMPLETION, lsp.CompletionOptions(trigger_characters=[".", " "])
)
@server.thread()
def completions(ls: CpyLanguageServer, params: lsp.CompletionParams):
    try:
        uri = params.text_document.uri
        line = params.position.line
        col = params.position.character
        t0 = time.time()
        source, tokens, parsed, analyzer, _, _ = _get_bundle(ls, uri)
        tok = _find_token_at(tokens, line, col)
        prefix = (tok.value or "") if tok else ""

        scope = _scope_at(parsed, line + 1)
        in_loop = _inside_loop(parsed, line + 1)

        # Contextual: are we completing the argument of a `new T` / `new Foo`?
        new_context = _is_new_context(tokens, line, col)

        items = []
        seen = set()

        # Import path completions: cursor is inside the quoted module path of
        # `import "..."` — offer files/dirs/libraries without needing a valid
        # (closed) string, since the file isn't parsed until lexing succeeds.
        import_ctx = _import_string_context(source, line, col)
        if import_ctx is not None:
            partial, _start, _end = import_ctx
            filepath = _uri_to_path(uri)
            file_dir = os.path.dirname(filepath) if filepath else None
            workspace_root = (
                _uri_to_path(ls.workspace_root) if ls.workspace_root else None
            )
            items = _import_dir_completions(partial, file_dir, workspace_root)
            elapsed = time.time() - t0
            logger.info(
                f"[completion] {os.path.basename(_uri_to_path(uri))} @L{line + 1}:{col}: "
                f"{len(items)} import-path item(s) for `{partial!r}` in {elapsed * 1000:.0f}ms"
            )
            return lsp.CompletionList(is_incomplete=False, items=items)

        base = _dot_context(tokens, line, col)
        if base is not None:
            items = _member_completions(analyzer, parsed, base, prefix, line + 1)
            elapsed = time.time() - t0
            logger.info(
                f"[completion] {os.path.basename(_uri_to_path(uri))} @L{line + 1}:{col}: "
                f"{len(items)} member item(s) for `{base}.` in {elapsed * 1000:.0f}ms"
            )
            return lsp.CompletionList(is_incomplete=False, items=items)

        for kw in _KEYWORDS:
            if kw.startswith(prefix):
                if _SNIPPETS.get(kw) and _keyword_on_line(tokens, line, col, kw):
                    continue
                if kw in _TOPLEVEL_KEYWORDS and scope != "top":
                    continue
                if kw in _LOOP_ONLY_KEYWORDS and not in_loop:
                    continue
                snippet = _SNIPPETS.get(kw)
                items.append(
                    lsp.CompletionItem(
                        label=kw,
                        kind=lsp.CompletionItemKind.Keyword,
                        insert_text_format=lsp.InsertTextFormat.Snippet
                        if snippet
                        else lsp.InsertTextFormat.PlainText,
                        insert_text=snippet or kw,
                    )
                )
                seen.add(kw)

        for t in _TYPES:
            if t.startswith(prefix) and t not in seen:
                items.append(
                    lsp.CompletionItem(
                        label=t,
                        kind=lsp.CompletionItemKind.TypeParameter,
                        detail=_TYPE_DESC.get(t, ""),
                        insert_text=t,
                    )
                )
                seen.add(t)

        for name, sig in _BUILTIN_FUNCS.items():
            if name.startswith(prefix) and name not in seen:
                items.append(
                    lsp.CompletionItem(
                        label=name,
                        kind=lsp.CompletionItemKind.Function,
                        detail=sig,
                        insert_text=f"{name}(",
                    )
                )
                seen.add(name)

        for node in parsed:
            if (
                isinstance(node, FuncDef)
                and node.name.startswith(prefix)
                and node.name not in seen
            ):
                if not new_context:
                    sig = _fmt_params(node.params, getattr(node, "const_params", None))
                    items.append(
                        lsp.CompletionItem(
                            label=node.name,
                            kind=lsp.CompletionItemKind.Function,
                            detail=f"({sig}) -> {node.rettype or 'void'}",
                            insert_text=f"{node.name}(",
                        )
                    )
                    seen.add(node.name)
            elif (
                isinstance(node, StructDef)
                and node.name.startswith(prefix)
                and node.name not in seen
            ):
                items.append(
                    lsp.CompletionItem(
                        label=node.name,
                        kind=lsp.CompletionItemKind.Class,
                        detail="struct",
                    )
                )
                seen.add(node.name)
            elif (
                isinstance(node, ClassDef)
                and node.name.startswith(prefix)
                and node.name not in seen
            ):
                items.append(
                    lsp.CompletionItem(
                        label=node.name,
                        kind=lsp.CompletionItemKind.Class,
                        detail=f"class{(' extends ' + node.base) if node.base else ''}",
                    )
                )
                seen.add(node.name)
            elif (
                isinstance(node, EnumDef)
                and node.name.startswith(prefix)
                and node.name not in seen
            ):
                items.append(
                    lsp.CompletionItem(
                        label=node.name,
                        kind=lsp.CompletionItemKind.Enum,
                        detail="enum",
                    )
                )
                seen.add(node.name)

        if analyzer:
            for name, sym in analyzer.globals.symbols.items():
                if "." in name:
                    continue
                if name.startswith(prefix) and name not in seen:
                    if sym.kind in ("struct", "class", "enum"):
                        kind = {
                            "struct": lsp.CompletionItemKind.Class,
                            "class": lsp.CompletionItemKind.Class,
                            "enum": lsp.CompletionItemKind.Enum,
                        }[sym.kind]
                        items.append(
                            lsp.CompletionItem(label=name, kind=kind, detail=sym.kind)
                        )
                        seen.add(name)
                        continue
                    if new_context:
                        continue
                    if sym.kind == "const":
                        kind = lsp.CompletionItemKind.Constant
                        detail = f"const {sym.type} = {_const_value_text(sym)}"
                    elif sym.kind == "function":
                        kind = lsp.CompletionItemKind.Function
                        detail = f"-> {sym.type or ''}"
                    else:
                        kind = lsp.CompletionItemKind.Variable
                        detail = sym.type or sym.kind
                    items.append(
                        lsp.CompletionItem(
                            label=name,
                            kind=kind,
                            detail=detail,
                        )
                    )
                    seen.add(name)

        # Symbols re-exported by imports (functions/types from imported files
        # and installed CPM packages).
        for name, (kind, _detail) in _collect_imported_symbols(parsed).items():
            if name.startswith(prefix) and name not in seen:
                items.append(
                    lsp.CompletionItem(
                        label=name,
                        kind=lsp.CompletionItemKind.Function
                        if kind == "function"
                        else lsp.CompletionItemKind.Class,
                        detail="imported",
                    )
                )
                seen.add(name)

        func = _find_containing_function(parsed, line + 1)
        if func is not None:
            for p, pt in (func.params or {}).items():
                if p == "this":
                    continue
                if p.startswith(prefix) and p not in seen:
                    items.append(
                        lsp.CompletionItem(
                            label=p,
                            kind=lsp.CompletionItemKind.Variable,
                            detail=f"parameter: {pt}",
                        )
                    )
                    seen.add(p)
            for lname, ltype in _collect_locals(func, line + 1).items():
                if lname.startswith(prefix) and lname not in seen:
                    items.append(
                        lsp.CompletionItem(
                            label=lname,
                            kind=lsp.CompletionItemKind.Variable,
                            detail=ltype or "int",
                        )
                    )
                    seen.add(lname)

        elapsed = time.time() - t0
        logger.info(
            f"[completion] {os.path.basename(_uri_to_path(uri))} @L{line + 1}:{col}: "
            f"{len(items)} items in {elapsed * 1000:.0f}ms"
        )
        return lsp.CompletionList(is_incomplete=False, items=items)
    except Exception:
        logger.error(f"completions error:\n{traceback.format_exc()}")
        return lsp.CompletionList(is_incomplete=False, items=[])


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
                                f"@{d}" if isinstance(d, str) else f"@{d}"
                                for d in node.decorators
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
                            if prev and prev.type == TokenType.IDENTIFIER:
                                func_name = prev.value
                            elif (
                                prev
                                and prev.type == TokenType.KEYWORD
                                and prev.value in _BUILTIN_FUNC_PARAMS
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
            analyzer = bundle[3]
            deps = set()
            for imp in getattr(analyzer, "_imports", None) or []:
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


@server.feature(lsp.TEXT_DOCUMENT_CODE_ACTION, lsp.CodeActionOptions())
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
