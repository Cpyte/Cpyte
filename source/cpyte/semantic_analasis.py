import os
import re
import subprocess
import sys
import tempfile


def _get_multiarch():
    for cc in ("cc", "gcc", "clang"):
        try:
            r = subprocess.run(
                [cc, "-print-multiarch"], capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def _get_sdk_paths():
    """Auto-discover system include/SDK roots across macOS, Linux and Windows."""
    paths = []

    if sys.platform == "darwin":
        try:
            sdk = subprocess.run(
                ["xcrun", "--show-sdk-path"], capture_output=True, text=True, timeout=5
            )
            if sdk.returncode == 0 and sdk.stdout.strip():
                paths.append(sdk.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
        for base in (
            "/Library/Developer/CommandLineTools/SDKs",
            "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs",
        ):
            if os.path.isdir(base):
                for entry in sorted(os.listdir(base), reverse=True):
                    full = os.path.join(base, entry)
                    if (
                        os.path.isdir(full)
                        and entry.startswith("MacOSX")
                        and full not in paths
                    ):
                        paths.append(full)
        for p in ("/usr/local/include", "/usr/include"):
            if os.path.isdir(p) and p not in paths:
                paths.append(p)

    elif sys.platform == "win32":
        roots = []
        windows_sdk = os.environ.get("WindowsSdkDir")
        if windows_sdk:
            roots.append(os.path.join(windows_sdk, "Include"))
        pf = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
        if pf:
            roots.append(os.path.join(pf, "Windows Kits", "10", "Include"))
            roots.append(os.path.join(pf, "Windows Kits", "8.1", "Include"))
        vctools = os.environ.get("VCToolsInstallDir")
        if vctools:
            roots.append(os.path.join(vctools, "include"))
        for root in roots:
            if not os.path.isdir(root):
                continue
            versioned = [
                d
                for d in sorted(os.listdir(root), reverse=True)
                if re.match(r"^\d+(\.\d+)+", d)
            ]
            candidates = (
                [os.path.join(root, d) for d in versioned] if versioned else [root]
            )
            for c in candidates:
                if os.path.isdir(c) and c not in paths:
                    paths.append(c)
        for p in ("C:/msys64/usr/include", "C:/msys2/usr/include"):
            if os.path.isdir(p) and p not in paths:
                paths.append(p)

    else:
        for p in ("/usr/local/include", "/usr/include"):
            if os.path.isdir(p) and p not in paths:
                paths.append(p)
        multiarch = _get_multiarch()
        if multiarch:
            cand = os.path.join("/usr/include", multiarch)
            if os.path.isdir(cand) and cand not in paths:
                paths.append(cand)

    return paths


from .astparse import (
    AddrOf,
    Assert,
    Assign,
    Attr,
    BinOp,
    Break,
    BorrowExpr,
    Call,
    CastExpr,
    CCode,
    ClassDef,
    Continue,
    DeferStmt,
    Deref,
    EnumDef,
    ExprStmt,
    FString,
    FuncDef,
    If,
    Import,
    Index,
    MoveExpr,
    InlineAsm,
    Input,
    InputBig,
    InputStr,
    ListLit,
    Llvm,
    NewExpr,
    Number,
    ParseError,
    Print,
    Raise,
    Return,
    Signed67,
    SizeOf,
    String,
    StructDef,
    Switch,
    Try,
    TypeAlias,
    UnaryOp,
    VarDecl,
    Variable,
    While,
    parse_file,
)
from .clib import (
    _framework_name_from_path,
    parse_c_source,
    parse_header_file,
    parse_llvm_ir_text,
    resolve_library,
)
from .extension_hooks import (
    CompilerContext,
    DiagnosticSeverity,
    HookLoader,
    HookStage,
    SemanticHook,
    get_global_hook_registry,
)
from .lexar import Lexer, LexerError, register_keywords
from .package_manifest import ManifestParser, get_global_registry, iter_cpm_version_dirs

_ARRAY_SUFFIX_RE = re.compile(r"^(.*)\[\d*\]$")
_FIXED_ARRAY_RE = re.compile(r"^(.*?)\[(\d+)\]$")


def _array_back(t):
    """Element type of an array type `T[]` / `T[N]`, or None if not an array."""
    if not isinstance(t, str):
        return None
    m = _ARRAY_SUFFIX_RE.match(t)
    return m.group(1) if m else None


def _array_family(t):
    """Normalize `T[N]` to `T[]`. Both are `T*` at the LLVM level, so type
    compatibility checks treat a fixed-size array as an array of its element."""
    b = _array_back(t)
    return b + "[]" if b is not None else t


def _c(text, *styles):
    """Colorize `text` with ANSI codes when stdout is a terminal (and color is
    not disabled). Falls back to plain text for piped/non-tty output."""
    import os
    import sys

    if os.environ.get("NO_COLOR"):
        return text
    if os.environ.get("CLICOLOR") == "0" and not (
        os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE")
    ):
        return text
    forced = bool(os.environ.get("FORCE_COLOR")) or bool(
        os.environ.get("CLICOLOR_FORCE")
    )
    stream = sys.stderr if sys.stderr else sys.stdout
    if not forced and not getattr(stream, "isatty", lambda: False)():
        return text
    codes = {
        "bold": 1,
        "dim": 2,
        "italic": 3,
        "underline": 4,
        "black": 30,
        "red": 31,
        "green": 32,
        "yellow": 33,
        "blue": 34,
        "magenta": 35,
        "cyan": 36,
        "white": 37,
        "bright_red": 91,
        "bright_green": 92,
        "bright_yellow": 93,
        "bright_blue": 94,
        "bright_magenta": 95,
        "bright_cyan": 96,
        "bright_white": 97,
        "bright_black": 90,
    }
    parts = [str(codes[s]) for s in styles if s in codes]
    if not parts:
        return text
    return "\x1b[%sm%s\x1b[0m" % (";".join(parts), text)


class Diagnostic:
    __slots__ = ("level", "message", "note", "span", "token", "code")

    def __init__(
        self,
        message: str,
        token=None,
        note: str | None = None,
        level: str = "error",
        span: int = 0,
        code: str | None = None,
    ):
        self.message = message
        self.token = token
        self.note = note
        self.level = level
        # Length of the underlined region in characters (0 => single caret).
        self.span = span
        self.code = code


_LEVEL_COLORS = {
    "error": "bright_red",
    "strict-error": "magenta",
    "warning": "bright_yellow",
    "strict-warning": "yellow",
}
_LEVEL_LABEL = {
    "error": "error",
    "strict-error": "error",
    "warning": "warning",
    "strict-warning": "warning",
}


class Reporter:
    def __init__(self, source: str):
        self.source = source
        self.lines = source.split("\n")
        self.diagnostics: list[Diagnostic] = []
        self._error_count = 0
        self._warning_count = 0

    def _code(self, prefix: str, explicit: str | None) -> str:
        """Return an error/warning code. Explicit codes are used verbatim;
        otherwise a unique `E####` / `W####` is generated."""
        if explicit:
            return explicit
        if prefix == "E":
            self._error_count += 1
            return f"E{self._error_count:04d}"
        self._warning_count += 1
        return f"W{self._warning_count:04d}"

    def error(
        self,
        message: str,
        token=None,
        note: str | None = None,
        span: int = 0,
        code: str | None = None,
    ):
        self.diagnostics.append(
            Diagnostic(
                message,
                token,
                note,
                level="error",
                span=span,
                code=self._code("E", code),
            )
        )

    def strict_error(
        self, message: str, token=None, note: str | None = None, code: str | None = None
    ):
        self.diagnostics.append(
            Diagnostic(
                message,
                token,
                note,
                level="strict-error",
                code=self._code("E", code),
            )
        )

    def strict_warning(
        self, message: str, token=None, note: str | None = None, code: str | None = None
    ):
        self.diagnostics.append(
            Diagnostic(
                message,
                token,
                note,
                level="strict-warning",
                code=self._code("W", code),
            )
        )

    def warning(
        self,
        message: str,
        token=None,
        note: str | None = None,
        span: int = 0,
        code: str | None = None,
    ):
        self.diagnostics.append(
            Diagnostic(
                message,
                token,
                note,
                level="warning",
                span=span,
                code=self._code("W", code),
            )
        )

    def has_errors(self) -> bool:
        return any(d.level in ("error", "strict-error") for d in self.diagnostics)

    def display(self) -> str:
        if not self.diagnostics:
            return ""

        diags = self._sorted_deduped(self.diagnostics)

        # Recolor the message with its level and render each diagnostic.
        parts = []
        for diag in diags:
            parts.append(self._format(diag))

        err_count = sum(1 for d in diags if d.level in ("error", "strict-error"))
        warn_count = sum(1 for d in diags if d.level in ("strict-warning", "warning"))
        if err_count or warn_count:
            bits = []
            if err_count:
                plural = "s" if err_count > 1 else ""
                bits.append(f"{err_count} semantic error{plural}")
            if warn_count:
                plural = "s" if warn_count > 1 else ""
                bits.append(f"{warn_count} warning{plural}")
            summary = "found: " + ", ".join(bits) + "."
            parts.append(_c(summary, "bold"))
        return "\n".join(parts)

    @staticmethod
    def _sorted_deduped(diags):
        """Return diagnostics ordered by (line, column) with exact duplicates
        removed, keeping only the first occurrence of a repeated diagnostic."""
        key = lambda d: (
            d.token.line if d.token is not None else -1,
            d.token.column if d.token is not None else -1,
            d.level,
            d.message,
        )
        seen = set()
        out = []
        for d in sorted(diags, key=key):
            sig = (d.level, d.message, d.note, key(d))
            if sig in seen:
                continue
            seen.add(sig)
            out.append(d)
        return out

    def _format(self, diag: Diagnostic) -> str:
        color = _LEVEL_COLORS.get(diag.level, "bright_red")
        label = _LEVEL_LABEL.get(diag.level, diag.level)
        bare = diag.token is None
        code = diag.code if diag.code else ""
        if bare:
            head = f"{label}: {diag.message}"
            if code:
                head = f"{label} {code}: {diag.message}"
            head = _c(head, color)
            lines = [head]
            if diag.note:
                lines.append(_c(f"  help: {diag.note}", "bright_black"))
            return "\n".join(lines)

        line = diag.token.line
        col = diag.token.column
        src = self.lines[line - 1] if 0 < line <= len(self.lines) else ""

        span = diag.span
        if span <= 0:
            # Default span: the length of the token value when available.
            val = getattr(diag.token, "value", None)
            span = len(val) if isinstance(val, str) and val else 1
        # Clamp the span to the source line so we don't underline past the end.
        span = max(1, min(span, max(1, len(src) - col + 1)))

        width = len(str(line))
        gutter = " " * width
        line_no = _c(str(line).rjust(width), "bright_cyan")

        loc = f"[{line}:{col}]"
        if code:
            loc = f"[{line}:{col}] {code}"
        head = _c(f"{label}{loc} {diag.message}", color)
        lines = [
            head,
            f"{gutter} |",
            f"{line_no} | {src}",
        ]

        underline = "^" * span
        marker_col = max(0, col - 1)
        lines.append(f"{gutter} | {_c(' ' * marker_col + underline, color)}")
        if diag.note:
            lines.append(_c(f"{gutter} |", "bright_black"))
            lines.append(_c(f"{gutter} = help: {diag.note}", "bright_black"))

        return "\n".join(lines)


class Symbol:
    __slots__ = ("const_value", "dynamic", "initialized", "kind", "node", "type")

    def __init__(
        self, kind: str, type_: str | None = None, node=None, initialized: bool = True
    ):
        self.kind = kind
        self.type = type_
        self.node = node
        self.const_value = None
        self.initialized = bool(initialized)
        self.dynamic = False


class Scope:
    def __init__(self, parent: "Scope | None" = None):
        self.parent = parent
        self.symbols: dict[str, Symbol] = {}

    def define(self, name: str, symbol: Symbol):
        self.symbols[name] = symbol

    def undefine(self, name: str):
        self.symbols.pop(name, None)

    def lookup(self, name: str) -> Symbol | None:
        if name in self.symbols:
            return self.symbols[name]
        if self.parent is not None:
            return self.parent.lookup(name)
        return None

    def lookup_local(self, name: str) -> Symbol | None:
        return self.symbols.get(name)


def _is_literal_zero(node) -> bool:
    if not isinstance(node, Number):
        return False
    if node.inferred_type == "float":
        return False
    try:
        return int(node.value, 0) == 0
    except (ValueError, TypeError):
        return False


def _is_compile_time_false(node) -> bool:
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


def _is_compile_time_true(node) -> bool:
    return isinstance(node, Number) and not _is_compile_time_false(node)


# When expression inference recurses deeper than this, the analyzer switches
# to an iterative post-order replay so that arbitrarily deep expressions can
# never blow the Python recursion limit. Each nesting level costs a small
# constant number of interpreter frames, so this stays far below the default
# recursion limit (1000) even combined with statement/visit frames.
_ANALYZE_DEPTH_LIMIT = 120


class SemanticAnalyzer:
    def __init__(
        self,
        source: str,
        filepath: str | None = None,
        workspace_root: str | None = None,
        strict: bool = False,
        enable_extensions: bool = True,
        no_gc: bool = False,
    ):
        self.reporter = Reporter(source)
        self.globals = Scope()
        self.current_func: FuncDef | None = None
        self.current_class: ClassDef | None = None
        self._generic_instantiations: dict[
            str, list[tuple]
        ] = {}  # name -> [(type_args, ...)]
        self.locals: Scope | None = None
        self.filepath = filepath
        self._filedir = os.path.dirname(filepath) if filepath else None
        self._workspace_root = workspace_root
        self._loop_depth = 0
        self._enum_context_name: str | None = None
        self.strict = strict
        self.enable_extensions = enable_extensions
        self.no_gc = no_gc
        self.globals.define(
            "free", Symbol("builtin_func", "void", None, initialized=True)
        )
        self._loaded_packages: set[str] = set()
        self._manifest_registry = get_global_registry()
        self._hook_registry = get_global_hook_registry()
        self._hook_ctx: CompilerContext | None = None
        self._analyze_depth = 0
        self._infer_memo: dict[int, str] = {}
        self._in_iterative = False
        self._in_decorator = False
        self._lazy_imports: list[dict] = []
        self._lazy_header_cache: dict[tuple, tuple] = {}
        self._lazy_load_error: str | None = None
        self._promoted_vars: set[str] = set()
        self._forward_names: set[str] = set()

    def _hook_context(self) -> CompilerContext:
        """Build a cached CompilerContext exposing this analyzer to hooks."""
        if self._hook_ctx is None:
            self._hook_ctx = CompilerContext(
                source_file=self.filepath,
                semantic_model=self,
                data={
                    "analyzer": self,
                    "workspace_root": self._workspace_root,
                },
            )
        return self._hook_ctx

    def _load_package_manifest(self, package_dir: str, package_name: str) -> bool:
        """
        Load and register a package manifest.

        Args:
            package_dir: Directory containing the package
            package_name: Name of the package

        Returns:
            True if manifest was loaded successfully, False otherwise
        """
        if not self.enable_extensions:
            return False

        if package_name in self._loaded_packages:
            return True  # Already loaded

        # In a long-lived process (the LSP server re-analyses the same file many
        # times) the global manifest/hook registries persist across analyses.
        # If this package's manifest is already registered globally, its hooks
        # were already registered by an earlier analysis — re-loading them here
        # would raise "Hook ... is already registered" and make the LSP emit
        # spurious "not installed" diagnostics on correct installed libraries.
        if self._manifest_registry.is_loaded(package_name):
            self._loaded_packages.add(package_name)
            return True

        manifest_path = os.path.join(package_dir, "package.json")
        if not os.path.exists(manifest_path):
            return False  # No manifest file

        try:
            manifest = ManifestParser.validate_and_parse(manifest_path)

            # Register keywords with lexer
            if manifest.capabilities.keywords:
                register_keywords(manifest.capabilities.keywords)

            # Register manifest in global registry
            self._manifest_registry.register(manifest)

            # Load hooks if present
            if self._workspace_root:
                context = self._hook_context()
                context.data["package_dir"] = package_dir
                context.data["package_name"] = package_name

                all_hook_files = (
                    manifest.extensions.parser_hooks
                    + manifest.extensions.semantic_hooks
                    + manifest.extensions.codegen_hooks
                    + manifest.extensions.runtime_hooks
                )

                if all_hook_files:
                    HookLoader.load_hooks_from_package(
                        package_name,
                        package_dir,
                        all_hook_files,
                        self._hook_registry,
                        context,
                    )

            self._loaded_packages.add(package_name)

            # Register empty symbols for extension-only packages
            # This allows the import to succeed even if there's no .cpy file
            self.globals.define(package_name, Symbol("package"))

            return True

        except Exception as e:
            self.error(f"Failed to load package manifest for '{package_name}': {e}")
            return False

    def _load_cpm_package_manifests(self) -> None:
        """Load manifests from all CPM packages in the workspace."""
        if not self.enable_extensions or not self._workspace_root:
            return

        cpm_root = os.path.join(self._workspace_root, ".cpm", "modules")
        if not os.path.isdir(cpm_root):
            return

        for package_name, version_dir in iter_cpm_version_dirs(cpm_root):
            # Match on the base package name so import lookups (which use the
            # final path segment, e.g. "json" for "@std/json") find it.
            base_name = package_name.rsplit("/", 1)[-1]

            if base_name in self._loaded_packages:
                continue

            self._load_package_manifest(version_dir, base_name)

    def _tok(self, node):
        if isinstance(node, dict):
            return node.get("_token")
        return getattr(node, "_token", None)

    @staticmethod
    def _levenshtein(a: str, b: str) -> int:
        """Edit distance between two strings (Levenshtein)."""
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(
                    min(
                        prev[j] + 1,
                        cur[j - 1] + 1,
                        prev[j - 1] + (ca != cb),
                    )
                )
            prev = cur
        return prev[-1]

    def _suggest(self, name: str, candidates: list[str]) -> str | None:
        """Return the best-matching candidate for `name`, or None if none is
        close enough. Used for 'did you mean ...?' hints."""
        best = None
        best_dist = None
        for cand in candidates:
            if cand == name:
                continue
            if cand.startswith("_") and not name.startswith("_"):
                continue
            d = self._levenshtein(name, cand)
            if d <= 1 or (len(name) >= 4 and d <= 2):
                if best_dist is None or d < best_dist:
                    best_dist = d
                    best = cand
        return best

    def _scope_names(self) -> list[str]:
        """All visible identifier names in the current scope chain + globals."""
        names: set[str] = set()
        for scope in self._scope_chain():
            names.update(scope.symbols.keys())
        return sorted(names)

    def _scope_chain(self):
        chain = []
        s = getattr(self, "current_scope", None) or getattr(self, "globals", None)
        while s is not None:
            chain.append(s)
            s = getattr(s, "parent", None)
        if not chain and getattr(self, "globals", None) is not None:
            chain.append(self.globals)
        return chain

    def error(
        self,
        message: str,
        node=None,
        note: str | None = None,
        span: int = 0,
        code: str | None = None,
    ):
        self.reporter.error(message, self._tok(node), note, span=span, code=code)

    def warning(
        self,
        message: str,
        node=None,
        note: str | None = None,
        span: int = 0,
        code: str | None = None,
    ):
        self.reporter.warning(message, self._tok(node), note, span=span, code=code)

    def _strict_error(self, message: str, node=None, note: str | None = None):
        if self.strict:
            self.reporter.strict_error(message, self._tok(node), note)

    def _strict_warning(self, message: str, node=None, note: str | None = None):
        if self.strict:
            self.reporter.strict_warning(message, self._tok(node), note)

    _NUMERIC_TYPES = (
        "int",
        "int64",
        "uint64",
        "float",
        "double",
        "big",
        "ubig",
        "char",
        "size_t",
    )
    _INT_TYPES = ("int", "int64", "uint64", "char", "size_t")
    _FLOAT_TYPES = ("float", "double")
    _WIDE_INT_TYPES = ("int64", "uint64")
    _CONV_BUILTINS = {"str": "str", "int": "int", "float": "float", "double": "float"}

    def _numeric_promote(self, t1: str | None, t2: str | None) -> str | None:
        """C-like usual arithmetic conversions for numeric types.

        Returns the type an arithmetic operation on ``t1`` and ``t2`` produces,
        or ``None`` if the operands are not mutually promotable.
        """
        if t1 is None or t2 is None:
            return None
        if t1 == t2:
            return t1 if t1 in self._NUMERIC_TYPES else None
        if t1 in self._FLOAT_TYPES or t2 in self._FLOAT_TYPES:
            if (t1 in self._FLOAT_TYPES or t1 in self._INT_TYPES) and (
                t2 in self._FLOAT_TYPES or t2 in self._INT_TYPES
            ):
                return "double" if "double" in (t1, t2) else "float"
            return None
        if "ubig" in (t1, t2):
            # ubig is unsigned big: it dominates all other integer/big types.
            if t1 in ("int", "int64", "uint64", "size_t", "big", "ubig") and t2 in (
                "int",
                "int64",
                "uint64",
                "size_t",
                "big",
                "ubig",
            ):
                return "ubig"
            return None
        if "big" in (t1, t2):
            if t1 in ("int", "int64", "uint64", "big") and t2 in (
                "int",
                "int64",
                "uint64",
                "big",
            ):
                return "big"
            return None
        if t1 in self._INT_TYPES and t2 in self._INT_TYPES:
            if t1 in self._WIDE_INT_TYPES or t2 in self._WIDE_INT_TYPES:
                return "int64"
            if t1 == "size_t" or t2 == "size_t":
                return "size_t"
            return "int"
        return None

    def analyze(self, nodes: list) -> bool:
        self._predeclare(nodes)
        for node in nodes:
            self._visit(node)
        if self._promoted_vars:
            self._stamp_dynamic(nodes)
        if self.reporter.has_errors():
            return False
        return True

    def _predeclare(self, nodes: list) -> None:
        """Pre-register top-level declarations before the main single-pass
        analysis so functions/structs/classes/enums/type-aliases may be
        referenced before their textual definition (forward references).

        Placeholder symbols carry enough metadata (return type / kind) for
        callers and type resolution; the real ``_visit_*`` handlers overwrite
        them in place, so ``_forward_names`` is used to suppress the normal
        redefinition error exactly once per forward-declared name.
        """
        stack = list(reversed(nodes))
        while stack:
            node = stack.pop()
            if isinstance(node, FuncDef):
                if node.name and node.name not in self._forward_names:
                    if self.globals.lookup_local(node.name) is None:
                        self.globals.define(
                            node.name, Symbol("function", node.rettype or "void", node)
                        )
                        self._forward_names.add(node.name)
            elif isinstance(node, StructDef):
                if node.name and node.name not in self._forward_names:
                    if self.globals.lookup_local(node.name) is None:
                        self.globals.define(node.name, Symbol("struct", None, node))
                        self._forward_names.add(node.name)
            elif isinstance(node, ClassDef):
                if node.name and node.name not in self._forward_names:
                    if self.globals.lookup_local(node.name) is None:
                        self.globals.define(node.name, Symbol("class", node.name, node))
                        self._forward_names.add(node.name)
            elif isinstance(node, EnumDef):
                if node.name and node.name not in self._forward_names:
                    if self.globals.lookup_local(node.name) is None:
                        self.globals.define(node.name, Symbol("enum", None, node))
                        self._forward_names.add(node.name)
            elif isinstance(node, TypeAlias):
                if node.name and node.name not in self._forward_names:
                    if self.globals.lookup_local(node.name) is None:
                        self.globals.define(
                            node.name, Symbol("type_alias", node.target_type, node)
                        )
                        self._forward_names.add(node.name)

    def _promoted_key(self, name: str) -> tuple:
        """Promotion identity is scoped to the enclosing function so that a
        variable promoted to ``dynamic`` in one function never contaminates an
        unrelated same-named variable in another function."""
        fn = self.current_func.name if self.current_func else ""
        return (fn, name)

    def _stamp_dynamic(self, nodes: list) -> None:
        """Mark every AST node referencing a promoted variable as dynamic.

        A variable may be promoted to ``dynamic`` (its type changes across
        assignments) after some read/write sites have already been analyzed.
        Code generation needs the *final* dynamic status at every site, so we
        walk the whole AST once analysis is complete and stamp the nodes.

        Promotions are scoped to their enclosing function, so a promotion of
        ``cb`` inside ``main`` never stamps an unrelated ``CountingBloom cb``
        declared in ``create_counting_bloom``.
        """
        visited: set[int] = set()
        stack: list = [(n, "") for n in nodes]
        while stack:
            item, fn = stack.pop()
            if id(item) in visited:
                continue
            visited.add(id(item))
            if isinstance(item, FuncDef):
                sub = (item.name, "")
                for child in item.body:
                    stack.append((child, item.name))
                continue
            if isinstance(item, Variable):
                if (fn, item.name) in self._promoted_vars:
                    item.dynamic = True
                continue
            if isinstance(item, VarDecl):
                if (fn, item.name) in self._promoted_vars:
                    item.dynamic = True
                if item.init is not None:
                    stack.append((item.init, fn))
                continue
            if isinstance(item, Assign):
                target = item.target
                if isinstance(target, Variable):
                    if (fn, target.name) in self._promoted_vars:
                        item.dynamic = True
                        target.dynamic = True
                    stack.append((target, fn))
                elif isinstance(target, str) and (fn, target) in self._promoted_vars:
                    item.dynamic = True
                else:
                    stack.append((target, fn))
                stack.append((item.value, fn))
                continue
            if isinstance(item, (list, tuple)):
                for child in item:
                    stack.append((child, fn))
                continue
            if isinstance(item, dict):
                for child in item.values():
                    stack.append((child, fn))
                continue
            slots = getattr(type(item), "__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                if slot.startswith("_"):
                    continue
                try:
                    child = getattr(item, slot)
                except AttributeError:
                    continue
                if child is None:
                    continue
                if isinstance(child, (list, tuple)):
                    for c in child:
                        stack.append((c, fn))
                elif isinstance(child, dict):
                    for c in child.values():
                        stack.append((c, fn))
                else:
                    stack.append((child, fn))

    def _infer_type(self, node):
        key = id(node)
        if key in self._infer_memo:
            return self._infer_memo[key]
        if not self._in_iterative:
            self._analyze_depth += 1
            try:
                if self._analyze_depth > _ANALYZE_DEPTH_LIMIT:
                    return self._infer_type_iterative(node)
                return self._infer_type_recursive(node)
            finally:
                self._analyze_depth -= 1
        return self._infer_type_recursive(node)

    def _infer_type_recursive(self, node):
        if isinstance(node, Number):
            # Check for hexadecimal literals (0x prefix) BEFORE float 'e' check,
            # since hex values legitimately contain the letter 'e' as a digit (0-9a-f)
            if node.value.startswith("0x") or node.value.startswith("0X"):
                try:
                    val = int(node.value, 16)
                    if val > 2**31 - 1 or val < -(2**31):
                        if val <= 2**63 - 1:
                            node.inferred_type = "int64"
                            return "int64"
                        if val <= 2**64 - 1:
                            node.inferred_type = "uint64"
                            return "uint64"
                        node.inferred_type = "big"
                        return "big"
                    node.inferred_type = "int"
                    return "int"
                except ValueError:
                    pass
                node.inferred_type = "int64"
                return "int64"
            if "." in node.value or "e" in node.value or "E" in node.value:
                node.inferred_type = "float"
                return "float"
            # Check for large decimal values that might need 64-bit
            try:
                val = int(node.value)
                if val > 2**31 - 1 or val < -(2**31):
                    if val <= 2**63 - 1 and val >= -(2**63):
                        node.inferred_type = "int64"
                        return "int64"
                    if val <= 2**64 - 1:
                        node.inferred_type = "uint64"
                        return "uint64"
                    node.inferred_type = "big"
                    return "big"
            except ValueError:
                pass
            # For small integers, return 'int' but allow implicit conversion to int64
            node.inferred_type = "int"
            return "int"

        if isinstance(node, String):
            node.inferred_type = "str"
            return "str"

        if isinstance(node, FString):
            for kind, payload in node.parts:
                if kind == "expr":
                    self._infer_type(payload)
            node.inferred_type = "str"
            return "str"

        if isinstance(node, Variable):
            if self._in_decorator and node.name == "result":
                node.inferred_type = "dynamic"
                node.dynamic = True
                return "dynamic"
            sym = self.current_scope.lookup(node.name)
            if sym is None and self._enum_context_name:
                sym = self.current_scope.lookup(
                    f"{self._enum_context_name}.{node.name}"
                )
            if sym is None:
                sym = self._lazy_resolve(node.name)
            if sym is None:
                if self._report_lazy_load_error(node):
                    return None
                note = "no definition found in this scope"
                suggestion = self._suggest(node.name, self._scope_names())
                if suggestion:
                    note = f"did you mean `{suggestion}`?"
                self.error(
                    f"use of undeclared identifier `{node.name}`",
                    node,
                    span=len(node.name),
                    code="E1001",
                    note=note,
                )
                return None
            if sym.const_value is not None:
                node.const_value = sym.const_value
            if sym.kind == "enum_member":
                node.inferred_type = "int"
                node.const_value = sym.const_value
                return "int"
            if sym.kind in ("enum", "struct"):
                node.inferred_type = node.name
                return node.name
            if sym.kind == "type_alias":
                node.inferred_type = sym.type
                return sym.type
            if sym.kind == "variable" and not sym.initialized:
                self.error(
                    f"use of uninitialized variable `{node.name}`",
                    node,
                    note="a variable declared without an initializer holds an "
                    "unspecified value; assign one before reading it",
                )
                return None
            node.dynamic = sym.dynamic
            node.inferred_type = sym.type
            return sym.type

        if isinstance(node, BinOp):
            left_t = self._infer_type(node.left)

            if node.op.name in ("AND", "OR"):
                if node.op.name == "AND" and _is_compile_time_false(node.left):
                    self._infer_type(node.right)
                    node.inferred_type = "bool"
                    return "bool"
                if node.op.name == "OR" and _is_compile_time_true(node.left):
                    self._infer_type(node.right)
                    node.inferred_type = "bool"
                    return "bool"
                right_t = self._infer_type(node.right)
                node.inferred_type = "bool"
                return "bool"

            right_t = self._infer_type(node.right)

            if left_t == "dynamic" or right_t == "dynamic":
                # One side is runtime-typed: defer the whole operation to the
                # runtime dispatcher (`dyn_op`), which inspects kinds at run time.
                node.inferred_type = "dynamic"
                return "dynamic"

            if node.op.name in (
                "EQ_EQ",
                "NOT_EQ",
                "LESS",
                "GREATER",
                "LESS_EQ",
                "GREATER_EQ",
            ):
                if left_t is not None and right_t is not None and left_t != right_t:
                    ok = self._numeric_promote(left_t, right_t) is not None
                    ok = ok or (left_t == "int" and right_t.endswith("*"))
                    ok = ok or (left_t.endswith("*") and right_t == "int")
                    ok = ok or (
                        left_t == "str" and (right_t.endswith("*") or right_t == "char")
                    )
                    ok = ok or (
                        right_t == "str" and (left_t.endswith("*") or left_t == "char")
                    )
                    ok = ok or (left_t, right_t) in (
                        ("float", "double"),
                        ("double", "float"),
                        ("int", "int64"),
                        ("int64", "int"),
                        ("int", "uint64"),
                        ("uint64", "int"),
                        ("int64", "uint64"),
                        ("uint64", "int64"),
                        ("str", "char"),
                        ("char", "str"),
                        ("char", "int"),
                        ("int", "char"),
                        ("char", "int64"),
                        ("int64", "char"),
                    )
                    ok = (
                        ok
                        or left_t == "big"
                        and right_t in ("int", "int64", "uint64", "ubig", "big")
                    )
                    ok = (
                        ok
                        or right_t == "big"
                        and left_t in ("int", "int64", "uint64", "ubig", "big")
                    )
                    ok = (
                        ok
                        or left_t == "ubig"
                        and right_t in ("int", "int64", "uint64", "ubig", "big")
                    )
                    ok = (
                        ok
                        or right_t == "ubig"
                        and left_t in ("int", "int64", "uint64", "ubig", "big")
                    )
                    if not ok:
                        self.error(
                            f"incompatible types in comparison: `{left_t}` vs `{right_t}`",
                            node,
                            note=f"both sides of `{node.op.name}` must be the same type",
                        )
                if left_t in ("big", "ubig") or right_t in ("big", "ubig"):
                    node.inferred_type = "bool"
                    return "bool"
                node.inferred_type = "bool"
                return "bool"

            if node.op.name in (
                "SHL",
                "SHR",
                "AMPERSAND",
                "PIPE",
                "CARET",
                "PERCENT",
            ):
                if node.op.name in ("SHL", "SHR", "AMPERSAND", "PIPE", "CARET"):
                    # Bitwise operations are unsupported for signed `big` but are
                    # provided for unsigned `ubig` (which has no sign bit), so
                    # AND/OR/XOR/shift operate over the full magnitude.
                    valid_int_types = ("int", "int64", "uint64", "size_t", "ubig")
                    if left_t == "big" or right_t == "big":
                        self.error(
                            f"bitwise operator `{node.op.name}` not supported for `big` operands",
                            node,
                            note=f"got `{left_t}` and `{right_t}`",
                        )
                    elif (left_t is not None and left_t not in valid_int_types) or (
                        right_t is not None and right_t not in valid_int_types
                    ):
                        self.error(
                            f"bitwise operator `{node.op.name}` requires integer operands",
                            node,
                            note=f"got `{left_t}` and `{right_t}`",
                        )
                    if left_t == "ubig" or right_t == "ubig":
                        node.inferred_type = "ubig"
                        return "ubig"
                    if left_t in ("int64", "uint64") or right_t in ("int64", "uint64"):
                        node.inferred_type = "int64"
                        return "int64"
                    if left_t == "size_t" or right_t == "size_t":
                        node.inferred_type = "size_t"
                        return "size_t"
                    node.inferred_type = "int"
                    return "int"
                # PERCENT
                valid_int_types = ("int", "int64", "uint64", "big", "ubig", "size_t")
                if (left_t is not None and left_t not in valid_int_types) or (
                    right_t is not None and right_t not in valid_int_types
                ):
                    self.error(
                        f"operator `{node.op.name}` requires integer operands",
                        node,
                        note=f"got `{left_t}` and `{right_t}`",
                    )
                if _is_literal_zero(node.right):
                    self.error(
                        f"division by zero in `{node.op.name}`",
                        node,
                        note="cannot divide or mod by zero",
                    )
                # Type promotion for mixed integer types
                if left_t == "ubig" or right_t == "ubig":
                    node.inferred_type = "ubig"
                    return "ubig"
                if left_t == "big" or right_t == "big":
                    node.inferred_type = "big"
                    return "big"
                if left_t in ("int64", "uint64") or right_t in ("int64", "uint64"):
                    node.inferred_type = "int64"
                    return "int64"  # Simplified: promote to int64 for mixed operations
                if left_t == "size_t" or right_t == "size_t":
                    node.inferred_type = "size_t"
                    return "size_t"
                node.inferred_type = "int"
                return "int"

            if node.op.name in ("PLUS", "MINUS", "STAR", "SLASH", "SLASH_SLASH", "POW"):
                if left_t == "str" and right_t == "str" and node.op.name == "PLUS":
                    node.inferred_type = "str"
                    return "str"
                if left_t == "str" or right_t == "str":
                    self.error(
                        f"operator `{node.op.name}` not supported for string operands",
                        node,
                        note="strings only support `+` (concatenation)",
                    )
                    result = left_t if left_t is not None else right_t
                    node.inferred_type = result
                    return result
                if node.op.name == "SLASH":
                    int_types = ("int", "int64", "uint64")
                    if (
                        left_t in int_types
                        and right_t in int_types
                        and _is_literal_zero(node.right)
                    ):
                        self.error(
                            "division by zero",
                            node,
                            note="cannot divide integer by zero",
                        )
                if left_t is not None and right_t is not None:
                    left_ptr = left_t.endswith("*")
                    right_ptr = right_t.endswith("*")
                    if node.op.name in ("PLUS", "MINUS") and (
                        (left_ptr and right_t == "int")
                        or (right_ptr and left_t == "int")
                    ):
                        node.inferred_type = left_t if left_ptr else right_t
                        return node.inferred_type
                    if node.op.name == "MINUS" and left_ptr and right_ptr:
                        node.inferred_type = "int"
                        return "int"
                if left_t is not None and right_t is not None and left_t != right_t:
                    # Usual arithmetic conversions: promote to the widest type
                    promoted = self._numeric_promote(left_t, right_t)
                    if promoted is not None:
                        node.inferred_type = promoted
                        return promoted
                    self.error(
                        f"mismatched types `{left_t}` and `{right_t}` in arithmetic expression",
                        node,
                        note=f"cannot apply `{node.op.name}` to different types",
                    )
                if left_t in ("float", "double") or right_t in ("float", "double"):
                    node.inferred_type = (
                        "double" if "double" in (left_t, right_t) else "float"
                    )
                    return node.inferred_type
                # Return the larger integer type
                if left_t == "ubig" or right_t == "ubig":
                    node.inferred_type = "ubig"
                    return "ubig"
                if left_t == "big" or right_t == "big":
                    node.inferred_type = "big"
                    return "big"
                if left_t in ("int64", "uint64") or right_t in ("int64", "uint64"):
                    node.inferred_type = "int64"
                    return "int64"
                if left_t == "size_t" or right_t == "size_t":
                    node.inferred_type = "size_t"
                    return "size_t"
                node.inferred_type = "int"
                return "int"

            node.inferred_type = left_t
            return left_t

        if isinstance(node, UnaryOp):
            operand_t = self._infer_type(node.operand)
            if operand_t == "dynamic":
                node.inferred_type = "dynamic"
                return "dynamic"
            if node.op.name == "NOT":
                node.inferred_type = "int"
                return "int"
            if node.op.name == "MINUS":
                if operand_t == "ubig":
                    self.error(
                        "cannot negate a `ubig` value",
                        node,
                        note="`ubig` is unsigned and can never be negative",
                    )
                    node.inferred_type = "ubig"
                    return "ubig"
                valid_types = (
                    "int",
                    "float",
                    "double",
                    "int64",
                    "uint64",
                    "big",
                    "size_t",
                )
                if operand_t is not None and operand_t not in valid_types:
                    self.error(
                        f"cannot apply unary minus to `{operand_t}`",
                        node,
                        note="unary minus expects numeric type",
                    )
            if node.op.name == "TILDE":
                valid_types = ("int", "int64", "uint64", "size_t")
                if operand_t is not None and operand_t not in valid_types:
                    self.error(
                        f"bitwise NOT (`~`) not supported for `{operand_t}`",
                        node,
                        note="bitwise NOT expects int, int64, or uint64 operand",
                    )
            if node.op.name == "MINUS_MINUS":
                if operand_t == "big":
                    self.error(
                        "decrement (`--`) not supported for `big`",
                        node,
                        note="big integers do not support decrement",
                    )
                if operand_t == "ubig":
                    self.error(
                        "decrement (`--`) not supported for `ubig`",
                        node,
                        note="`ubig` is unsigned and cannot be decremented below zero",
                    )
            node.inferred_type = operand_t
            return operand_t

        if isinstance(node, Call):
            if isinstance(node.callee, Variable):
                if node.callee.name in self._CONV_BUILTINS:
                    for arg in node.args:
                        self._infer_type(arg)
                    if len(node.args) != 1:
                        self.error(
                            f"{node.callee.name}() expects exactly 1 argument",
                            node,
                            note=f"got {len(node.args)}",
                        )
                        return None
                    target = self._CONV_BUILTINS[node.callee.name]
                    node.inferred_type = target
                    return target
                if node.callee.name == "range":
                    # Python-style builtin: range(stop) / range(start, stop) /
                    # range(start, stop, step) producing an int64 array.
                    # A user-defined `def range(...)` shadows the builtin.
                    user_sym = self.current_scope.lookup("range")
                    if user_sym is None or user_sym.kind != "function":
                        for arg in node.args:
                            self._infer_type(arg)
                        if not 1 <= len(node.args) <= 3:
                            self.error(
                                "range() expects 1 to 3 arguments",
                                node,
                                note=f"got {len(node.args)}",
                            )
                            return None
                        node.inferred_type = "int64[]"
                        return "int64[]"
                if node.callee.name == "str_split":
                    user_sym = self.current_scope.lookup("str_split")
                    if user_sym is None or user_sym.kind != "function":
                        for arg in node.args:
                            self._infer_type(arg)
                        if not 1 <= len(node.args) <= 2:
                            self.error(
                                "str_split() expects 1 or 2 arguments (string, separator)",
                                node,
                                note=f"got {len(node.args)}",
                            )
                            return None
                        node.inferred_type = "dynamic[]"
                        return "dynamic[]"
                if self._in_decorator and node.callee.name == "code":
                    for arg in node.args:
                        self._infer_type(arg)
                    if len(node.args) != 0:
                        self.error(
                            "code() expects no arguments",
                            node,
                            note=f"got {len(node.args)}",
                        )
                        return None
                    node.inferred_type = "dynamic"
                    return "dynamic"
            sym = self._resolve_callee(node.callee)
            if sym is not None:
                for arg in node.args:
                    self._infer_type(arg)
                self._check_call_args(node, sym)
                node.inferred_type = sym.type
                return sym.type
            return None

        if isinstance(node, Input):
            return "int"

        if isinstance(node, InputStr):
            return "str"

        if isinstance(node, InputBig):
            return "big"

        if isinstance(node, Signed67):
            return "str"

        if isinstance(node, ListLit):
            for item in node.items:
                self._infer_type(item)
            node.inferred_type = "dynamic[]"
            return "dynamic[]"

        if isinstance(node, Index):
            obj_t = self._infer_type(node.obj)
            self._infer_type(node.index)
            if obj_t == "dynamic":
                node.inferred_type = "dynamic"
                return "dynamic"
            if obj_t:
                elem_t = _array_back(obj_t)
                if elem_t is not None:
                    node.inferred_type = elem_t
                    return elem_t
            if obj_t == "str":
                node.inferred_type = "char"
                return "char"
            if obj_t and obj_t.endswith("*"):
                node.inferred_type = obj_t[:-1]
                return obj_t[:-1]
            if obj_t is not None:
                self.error(
                    f"cannot index value of type `{obj_t}`",
                    node,
                    note="indexing requires a string or array type",
                )
            return None

        if isinstance(node, Attr):
            obj_t = self._infer_type(node.obj)
            if obj_t:
                lookup_t = obj_t.removesuffix("*")
                sym = self.current_scope.lookup(lookup_t)
                if sym and sym.kind == "enum":
                    member_sym = self.current_scope.lookup(f"{lookup_t}.{node.name}")
                    if member_sym and member_sym.kind == "enum_member":
                        node._enum_member_value = member_sym.const_value
                        return "int"
                    self.error(f"enum `{lookup_t}` has no member `{node.name}`", node)
                    return None
                struct_sym = self.current_scope.lookup(lookup_t)
                if (
                    struct_sym
                    and struct_sym.kind in ("struct", "class")
                    and struct_sym.node
                ):
                    for field in struct_sym.node.fields:
                        if field.name == node.name:
                            node.inferred_type = field.type_expr
                            return field.type_expr
                    self.error(f"type `{obj_t}` has no field `{node.name}`", node)
                    return None
                self.error(
                    f"cannot access field `{node.name}` on non-struct type `{obj_t}`",
                    node,
                )
                return None
            return None

        if isinstance(node, Deref):
            operand_t = self._infer_type(node.operand)
            if operand_t is not None and operand_t.endswith("*"):
                node.inferred_type = operand_t[:-1]
                return operand_t[:-1]
            if operand_t is not None:
                self.error(f"cannot dereference non-pointer type `{operand_t}`", node)
            elif operand_t is None and hasattr(node.operand, "_token"):
                self.error("cannot dereference value of unknown type", node)
            return operand_t

        if isinstance(node, AddrOf):
            operand_t = self._infer_type(node.operand)
            if operand_t:
                return operand_t + "*"
            return None

        if isinstance(node, BorrowExpr):
            operand_t = self._infer_type(node.operand)
            resolved = self._resolve_type_alias(operand_t or "void")
            node.inferred_type = resolved + "*"
            return node.inferred_type

        if isinstance(node, MoveExpr):
            operand_t = self._infer_type(node.operand)
            node.inferred_type = operand_t
            return operand_t

        if isinstance(node, NewExpr):
            if node.size is not None:
                self._infer_type(node.size)
                # A literal size makes a fixed-size array type `T[N]` (e.g.
                # `new dynamic[1]` -> `dynamic[1]`, NOT `dynamic[]`). A runtime
                # size stays a dynamic array `T[]`.
                if isinstance(node.size, Number):
                    return f"{node.type_expr}[{node.size.value}]"
                return node.type_expr + "[]"
            return node.type_expr + "*"

        if isinstance(node, SizeOf):
            return "int"

        if isinstance(node, CastExpr):
            self._infer_type(node.expr)
            resolved = self._resolve_type_alias(node.type_expr)
            node.type_expr = resolved
            node.inferred_type = resolved
            return resolved

        if isinstance(node, InlineAsm):
            for _, arg_expr in node.inputs:
                self._infer_type(arg_expr)
            if node.outputs:
                return "i64"
            return "void"

        if isinstance(node, ExprStmt):
            return self._infer_type(node.expr)

        return None

    def _infer_children(self, node) -> list:
        """Child nodes that `_infer_type_recursive` descends into, in order."""
        if isinstance(node, BinOp):
            return [node.left, node.right]
        if isinstance(node, UnaryOp):
            return [node.operand]
        if isinstance(node, Call):
            return list(node.args)
        if isinstance(node, Index):
            return [node.obj, node.index]
        if isinstance(node, Attr):
            return [node.obj]
        if isinstance(node, Deref):
            return [node.operand]
        if isinstance(node, AddrOf):
            return [node.operand]
        if isinstance(node, NewExpr):
            return [node.size] if node.size is not None else []
        if isinstance(node, InlineAsm):
            return [arg_expr for _, arg_expr in node.inputs]
        if isinstance(node, ExprStmt):
            return [node.expr]
        if isinstance(node, CastExpr):
            return [node.expr]
        if isinstance(node, BorrowExpr):
            return [node.operand]
        if isinstance(node, MoveExpr):
            return [node.operand]
        if isinstance(node, DeferStmt):
            return [node.body]
        return []

    def _infer_combine(self, node):
        key = id(node)
        if key in self._infer_memo:
            return self._infer_memo[key]
        self._in_iterative = True
        try:
            t = self._infer_type_recursive(node)
        finally:
            self._in_iterative = False
        self._infer_memo[key] = t
        return t

    def _infer_type_iterative(self, node):
        memo = self._infer_memo
        if id(node) in memo:
            return memo[id(node)]
        stack = [("visit", node)]
        while stack:
            kind, n = stack.pop()
            key = id(n)
            if key in memo:
                continue
            if kind == "visit":
                children = self._infer_children(n)
                if children:
                    stack.append(("combine", n))
                    for c in reversed(children):
                        stack.append(("visit", c))
                else:
                    self._infer_combine(n)
            else:
                self._infer_combine(n)
        return memo[id(node)]

    @property
    def current_scope(self) -> Scope:
        return self.locals if self.locals is not None else self.globals

    def _resolve_callee(self, callee):
        if isinstance(callee, Variable):
            sym = self.current_scope.lookup(callee.name)
            if sym is None:
                sym = self._lazy_resolve(callee.name)
            if sym is None:
                if self._report_lazy_load_error(callee):
                    return None
                self.error(
                    f"use of undeclared identifier `{callee.name}`",
                    callee,
                    note="call target must be a function defined in scope",
                )
                return None
            if sym.kind not in ("function", "builtin_func"):
                self.error(
                    f"`{callee.name}` is not callable",
                    callee,
                    note=f"declared as `{sym.kind}`, not a function",
                )
                return None
            return sym
        return None

    def _check_call_args(self, call: Call, sym: Symbol):
        expected_count = 0
        if sym.kind == "builtin_func":
            return
        if sym.node and isinstance(sym.node, FuncDef):
            expected_count = len(sym.node.params)
        elif sym.node and isinstance(sym.node, (Import, CCode, Llvm)):
            for fname, (_, params, vararg) in sym.node.symbols:
                if fname == call.callee.name:
                    expected_count = len(params)
                    if vararg:
                        return
                    break
        actual_count = len(call.args)
        if expected_count != actual_count:
            name = call.callee.name if isinstance(call.callee, Variable) else "?"
            self.error(
                f"wrong number of arguments to `{name}`",
                call,
                note=f"expects {expected_count}, got {actual_count}",
            )

    def _visit(self, node, scope: Scope | None = None):
        for hook in self._hook_registry.get(HookStage.SEMANTIC):
            if not isinstance(hook, SemanticHook):
                continue
            try:
                if hook.should_visit_node(node):
                    ctx = self._hook_context()
                    ctx.data["scope"] = scope
                    ctx.data["node"] = node
                    for diag in hook.visit_node(node, ctx) or []:
                        self._report_hook_diag(diag)
            except Exception:
                pass
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
        elif isinstance(node, Assert):
            self._visit_assert(node)
        elif isinstance(node, While):
            self._visit_while(node, scope)
        elif isinstance(node, Switch):
            self._visit_switch(node, scope)
        elif isinstance(node, ExprStmt):
            self._infer_type(node.expr)
        elif isinstance(node, DeferStmt):
            self._visit(node.body, scope)
        elif isinstance(node, Import):
            self._visit_import(node)
        elif isinstance(node, StructDef):
            self._visit_struct(node, scope)
        elif isinstance(node, ClassDef):
            self._visit_class(node, scope)
        elif isinstance(node, EnumDef):
            self._visit_enum(node, scope)
        elif isinstance(node, TypeAlias):
            self._visit_type_alias(node, scope)
        elif isinstance(node, Try):
            self._visit_try(node, scope)
        elif isinstance(node, Raise):
            self._visit_raise(node)
        elif (
            isinstance(node, Deref)
            or isinstance(node, AddrOf)
            or isinstance(node, NewExpr)
        ):
            self._infer_type(node)
        elif isinstance(node, SizeOf):
            pass
        elif isinstance(node, InlineAsm):
            for _, var_expr in node.outputs:
                if isinstance(var_expr, Variable):
                    sym = self.current_scope.lookup(var_expr.name)
                    if sym is not None:
                        sym.initialized = True
            for _, var_expr in node.outputs:
                self._infer_type(var_expr)
            for _, arg_expr in node.inputs:
                self._infer_type(arg_expr)
        elif isinstance(node, CCode):
            self._visit_ccode(node)
        elif isinstance(node, Llvm):
            self._visit_llvm(node)
        elif (
            isinstance(node, Input)
            or isinstance(node, InputStr)
            or isinstance(node, InputBig)
            or isinstance(node, Signed67)
        ):
            pass
        elif isinstance(node, dict):
            self._visit_dict(node, scope)
        elif isinstance(node, (list, tuple)):
            for n in node:
                self._visit(n, scope)

    def _report_hook_diag(self, diag):
        """Surface a hook-returned Diagnostic through the local reporter."""
        if getattr(diag, "severity", None) is None:
            return
        sev = diag.severity
        note = None
        span = 0
        loc = getattr(diag, "location", None)
        tok = None
        if loc is not None and loc.line is not None:
            # Build a lightweight token so the reporter can point at the line.
            class _Tok:
                pass

            t = _Tok()
            t.line = loc.line
            t.column = loc.column or 1
            t.value = ""
            tok = t
        notes = getattr(diag, "notes", None) or []
        if notes:
            note = "; ".join(notes)
        code = getattr(diag, "code", None)
        if sev in (DiagnosticSeverity.ERROR, DiagnosticSeverity.FATAL):
            self.reporter.error(diag.message, tok, note, span=span, code=code)
        elif sev == DiagnosticSeverity.WARNING:
            self.reporter.warning(diag.message, tok, note, span=span, code=code)
        elif sev == DiagnosticSeverity.NOTE:
            self.reporter.warning(diag.message, tok, note, span=span, code=code)

    def _resolve_module_path(
        self, module: str, sdk_path: str | None = None
    ) -> str | None:
        candidates = [module]
        if self._filedir:
            candidates.append(os.path.normpath(os.path.join(self._filedir, module)))
            # Walk up from the source file's directory so an import that lives in a
            # parent directory of the file (e.g. a vendored sibling package or the
            # stdlib math library) resolves no matter where the LSP workspace root is.
            # This mirrors the CLI behavior, which falls back to cwd when inside the
            # stdlib dir. Plain first-match search path, same as C's quoted #include.
            d = os.path.normpath(self._filedir)
            seen = set()
            while True:
                parent = os.path.dirname(d)
                if parent == d or parent in seen:
                    break
                seen.add(parent)
                candidates.append(os.path.normpath(os.path.join(parent, module)))
                d = parent
        if self._workspace_root:
            candidates.append(
                os.path.normpath(os.path.join(self._workspace_root, module))
            )
        if module.endswith(".h") and "/" in module:
            parts = module.split("/")
            framework_name = parts[0]
            header_path = "/".join(parts[1:])
            sdk_paths = [sdk_path] if sdk_path else []
            sdk_paths.extend(_get_sdk_paths())
            for sdk in sdk_paths:
                for framework_dir in (
                    os.path.join(
                        sdk,
                        "System/Library/Frameworks",
                        f"{framework_name}.framework",
                        "Headers",
                    ),
                    os.path.join(
                        sdk,
                        "System/Library/Frameworks",
                        f"{framework_name}.framework",
                        "Versions/A/Headers",
                    ),
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

        is_file_import = (
            module.endswith(".c")
            or module.endswith(".cc")
            or module.endswith(".h")
            or module.endswith(".cpy")
            or module.startswith('"')
        )

        if not is_file_import and not module.startswith("@"):
            # Bare imports of built-in C libraries (e.g. `import "math"`) must
            # take precedence over local package names of the same identifier.
            # Users can still force a package import via the explicit `@scope/name`
            # syntax when a package intentionally shadows a C library.
            if resolve_library(module) is not None:
                result = resolve_library(module)
                if result is not None:
                    symbols, kind = result
                    self._register_import_symbols(symbols, node)
                    return

        if not is_file_import:
            if self._try_cpm_import(node, module):
                return

        if module.startswith("@"):
            self.error(f"package `{module}` not installed — run 'cpm install'", node)
            return

        resolved = self._resolve_module_path(module, node.sdk_path)

        if not is_file_import:
            result = resolve_library(module)
            if result is None:
                self.error(f"unknown library `{module}`", node)
            else:
                symbols, kind = result
                self._register_import_symbols(symbols, node)
            return

        if resolved is None:
            note = None
            if "/" in module and not node.sdk_path and not _get_sdk_paths():
                note = (
                    "no C SDK found on this system — install Xcode Command Line "
                    'Tools, or point cpy at one with sdk("...")'
                )
            self.error(f"file not found: `{module}`", node, note=note)
            return

        search_paths = [node.sdk_path] if node.sdk_path else []
        search_paths.extend(_get_sdk_paths())

        if module.endswith(".c") or module.endswith(".cc"):
            result = parse_c_source(resolved)
            if result:
                self._register_import_symbols(result[0], node)
            node.src_file = resolved
        elif module.endswith(".h"):
            framework = _framework_name_from_path(resolved)
            if framework:
                node.frameworks.append(framework)
            node.symbols = []
            node.var_names = set()
            self._lazy_imports.append(
                {
                    "node": node,
                    "path": resolved,
                    "search_paths": search_paths,
                    "loaded": False,
                }
            )
            return
        elif module.endswith(".cpy"):
            result = self._import_cpy(resolved, node)
            if result:
                self._register_import_symbols(result[0], node)
        elif "/" in module:
            ext = module.rsplit(".", 1)[-1] if "." in module else ""
            if ext in ("c", "cc"):
                result = parse_c_source(resolved)
                if result:
                    self._register_import_symbols(result[0], node)
                node.src_file = resolved
            elif ext == "cpy":
                result = self._import_cpy(resolved, node)
                if result:
                    self._register_import_symbols(result[0], node)
            else:
                framework = _framework_name_from_path(resolved)
                if framework:
                    node.frameworks.append(framework)
                node.symbols = []
                node.var_names = set()
                self._lazy_imports.append(
                    {
                        "node": node,
                        "path": resolved,
                        "search_paths": search_paths,
                        "loaded": False,
                    }
                )
                return
        else:
            result = None

        if result is None:
            self.error(f"unknown library `{module}`", node)

    def _register_import_symbols(self, symbols, node, var_names=None):
        s = self.globals
        var_names = var_names or set()
        node.var_names = var_names
        for fname, (ret_type, params, vararg) in symbols.items():
            existing = s.lookup_local(fname)
            if not existing:
                kind = "variable" if fname in var_names else "function"
                s.define(fname, Symbol(kind, ret_type, node))
        node.symbols = list(symbols.items())

    def _register_import_constants(self, constants, node):
        s = self.globals
        for name, val in constants.items():
            existing = s.lookup_local(name)
            if not existing:
                sym = Symbol("variable", "int", node)
                sym.const_value = val
                s.define(name, sym)

    def _visit_ccode(self, node):
        """Register functions defined by an embedded `ccode:` block.

        The C body is scanned the same way `import "file.c"` scans its source
        (via clib.parse_c_source) so every non-static, non-`main` function is
        callable from cpyte and its signature is known for arg checks. The
        emitted symbols are cached on the node for the bytecoder to declare.
        """
        if getattr(node, "symbols", None) is not None:
            return
        if not node.value:
            node.symbols = []
            node.var_names = set()
            return
        fd, path = tempfile.mkstemp(suffix=".c", prefix="ccode_sem_")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(node.value)
            result = parse_c_source(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if not result:
            node.symbols = []
            node.var_names = set()
            return
        node.symbols = list(result[0].items())
        node.var_names = set()
        self._register_import_symbols(result[0], node)

    def _visit_llvm(self, node):
        """Register functions defined by an embedded `llvm:` block.

        Safe `llvm:` blocks are scanned for their `define`d function
        signatures so calls from cpyte are type-checked like any other call.
        `unsafe llvm:` blocks are literal copy-paste: no signatures are
        registered, so their functions are not directly callable from cpyte.
        """
        if getattr(node, "symbols", None) is not None:
            return
        if node.unsafe or not node.value:
            node.symbols = []
            node.var_names = set()
            return
        result = parse_llvm_ir_text(node.value)
        if not result:
            node.symbols = []
            node.var_names = set()
            return
        node.symbols = list(result[0].items())
        node.var_names = set()
        self._register_import_symbols(result[0], node)

    def _lazy_resolve(self, name):
        """Resolve `name` from a lazily-imported header on first use.

        Headers are parsed on demand (once per analyzer, cached) and only the
        symbol actually referenced is registered, so `import "Framework.h"`
        pulls in just the bytecode the program uses.
        """
        if not self._lazy_imports:
            return None
        for entry in self._lazy_imports:
            if not entry.get("loaded"):
                cache = self._lazy_header_cache
                key = (entry["path"], tuple(entry["search_paths"] or ()))
                if key not in cache:
                    try:
                        result = parse_header_file(entry["path"], entry["search_paths"])
                    except Exception as e:
                        entry["loaded"] = True
                        if self._lazy_load_error is None:
                            self._lazy_load_error = (
                                f'could not load imported header `{entry["path"]}`: {e}'
                            )
                        continue
                    symbols, _kind, constants, frameworks, var_names = result
                    cache[key] = (symbols, constants, var_names, frameworks)
                (
                    entry["symbols"],
                    entry["constants"],
                    entry["var_names"],
                    entry["frameworks"],
                ) = cache[key]
                entry["loaded"] = True
                node = entry["node"]
                for fw in entry.get("frameworks", ()):
                    if fw and fw not in node.frameworks:
                        node.frameworks.append(fw)
            symbols = entry.get("symbols", {})
            if name in symbols:
                return self._lazy_register_used(
                    entry["node"], name, symbols[name], entry.get("var_names", set())
                )
            constants = entry.get("constants", {})
            if name in constants:
                return self._lazy_register_const(entry["node"], name, constants[name])
        return None

    def _report_lazy_load_error(self, node) -> bool:
        """Report a header load/parse failure once, at the first use site.

        Returns True if a load error was reported (caller should skip its own
        'undeclared identifier' diagnostic since the real root cause is the
        broken import).
        """
        if self._lazy_load_error is None:
            return False
        self.error(
            self._lazy_load_error,
            node,
            note="the imported header could not be loaded or parsed",
        )
        self._lazy_load_error = None
        return True

    def _lazy_register_used(self, node, name, entry, var_names=()):
        existing = self.globals.lookup_local(name)
        if existing is not None:
            return existing
        ret_type, params, vararg = entry
        kind = "variable" if name in var_names else "function"
        sym = Symbol(kind, ret_type, node)
        self.globals.define(name, sym)
        if node.symbols is None:
            node.symbols = []
        if not any(fname == name for fname, _ in node.symbols):
            node.symbols.append((name, entry))
        if kind == "variable":
            if node.var_names is None:
                node.var_names = set()
            node.var_names.add(name)
        return sym

    def _lazy_register_const(self, node, name, value):
        existing = self.globals.lookup_local(name)
        if existing is not None:
            return existing
        sym = Symbol("variable", "int", node)
        sym.const_value = value
        self.globals.define(name, sym)
        return sym

    def _find_package_entry(self, search_dir: str, pkg_name: str) -> str | None:
        for dir_candidate in (search_dir, os.path.join(search_dir, "src")):
            if not os.path.isdir(dir_candidate):
                continue
            for entry_name in (
                f"{pkg_name}.cpy",
                "package.cpy",
                "main.cpy",
            ):
                entry_path = os.path.join(dir_candidate, entry_name)
                if os.path.isfile(entry_path):
                    return entry_path
        return None

    def _check_llvm_version(self, pkg_dir: str) -> None:
        pkg_toml = os.path.join(pkg_dir, "package.toml")
        if not os.path.isfile(pkg_toml):
            return
        try:
            import tomllib

            with open(pkg_toml, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            return
        meta = data.get("package", {})
        if not meta.get("prebuilt", False):
            return
        expected = meta.get("llvm_version", "")
        if not expected:
            return
        import llvmlite.binding as llvm

        v = llvm.llvm_version_info
        actual = f"{v[0]}.{v[1]}.{v[2]}"
        if actual != expected:
            print(
                f"WARNING: package `{meta.get('name', '?')}` was prebuilt with LLVM {expected}, "
                f"current LLVM is {actual} — mismatch may cause errors",
                file=sys.stderr,
            )

    def _import_prebuilt(self, ll_dir: str, node: Import):
        self._check_llvm_version(ll_dir)
        ll_files = []
        symbols = {}
        for root, _dirs, files in os.walk(ll_dir):
            for f in sorted(files):
                if not f.endswith(".ll"):
                    continue
                path = os.path.join(root, f)
                with open(path) as fh:
                    content = fh.read()
                for m in re.finditer(
                    r'^\s*define\s+.*?@(?:"(\w+)"|(\w+))\s*\(([^)]*)\)',
                    content,
                    re.MULTILINE,
                ):
                    func_name = m.group(1) or m.group(2)
                    params_str = m.group(3).strip()
                    param_count = (
                        len([p for p in params_str.split(",") if p.strip()])
                        if params_str
                        else 0
                    )
                    if func_name not in symbols:
                        symbols[func_name] = (
                            "int",
                            [(f"p{i}", "int") for i in range(param_count)],
                            False,
                        )
                ll_files.append(path)
        if symbols:
            node.prebuilt_ll_files = ll_files
            self._register_import_symbols(symbols, node)
            return symbols, "prebuilt"
        return None

    def _try_cpm_import(self, node: Import, module: str) -> bool:
        # Support both @scope.name and @scope/name import forms.
        import_name = module[1:] if module.startswith("@") else module
        pkg_name = import_name.replace("/", ".").split(".")[-1]

        cpm_root = None
        if self._workspace_root:
            cpm_root = os.path.join(self._workspace_root, ".cpm", "modules")
        elif self._filedir:
            cpm_root = os.path.join(self._filedir, "..", ".cpm", "modules")

        # A package already loaded purely via its manifest (extension hooks only)
        # has no real symbols; short-circuit only when no CPM directory exists to
        # resolve a real entry from. A package that ships an entry file must still
        # re-export its actual symbols below.
        if pkg_name in self._loaded_packages and not (
            cpm_root and os.path.isdir(cpm_root)
        ):
            self._register_import_symbols({}, node)
            return True

        if cpm_root and os.path.isdir(cpm_root):
            if module.startswith("@"):
                scoped = module[1:].replace("/", ".").replace(".", os.sep)
                pkg_dir = os.path.join(
                    cpm_root, f"@{scoped.split(os.sep)[0]}", *scoped.split(os.sep)[1:]
                )
            else:
                pkg_dir = os.path.join(cpm_root, *module.replace("/", ".").split("."))

            if os.path.isdir(pkg_dir):
                versions = sorted(
                    [
                        d
                        for d in os.listdir(pkg_dir)
                        if os.path.isdir(os.path.join(pkg_dir, d))
                    ],
                    reverse=True,
                )
                if versions:
                    version_dir = os.path.join(pkg_dir, versions[0])

                    manifest_loaded = False
                    if self.enable_extensions:
                        manifest_loaded = self._load_package_manifest(
                            version_dir, pkg_name
                        )

                    cpy_file = self._find_package_entry(version_dir, pkg_name)
                    if cpy_file:
                        result = self._import_cpy(cpy_file, node)
                        if result:
                            self._register_import_symbols(result[0], node)
                        return True
                    result = self._import_prebuilt(version_dir, node)
                    if result:
                        return True

                    if manifest_loaded:
                        self._register_import_symbols({}, node)
                        return True
        if self.enable_extensions and pkg_name in self._loaded_packages:
            self._register_import_symbols({}, node)
            return True
        return False

    def _import_cpy(self, module: str, node: Import | None = None):
        try:
            with open(module) as f:
                source = f.read()
        except FileNotFoundError:
            self.error(f"file not found: `{module}`")
            return None

        lex = Lexer(source)
        try:
            tokens = lex.get_tokens()
        except LexerError as e:
            self.error(f"lex error in imported `{module}`: {e}")
            return None

        try:
            imported_ast, _ = parse_file(tokens)
        except ParseError as e:
            self.error(f"parse error in imported `{module}`: {e}")
            return None

        # Run semantic analysis on imported file
        sub = SemanticAnalyzer(
            source, filepath=module, workspace_root=self._workspace_root
        )
        if not sub.analyze(imported_ast):
            self.error(f"imported file `{module}` has semantic errors")
            return None

        # Extract public functions and structs
        symbols = {}
        sub_ast = []
        for ast_node in imported_ast:
            if isinstance(ast_node, (CCode, Llvm)):
                # Propagate embedded `ccode:`/`llvm:` blocks from the imported
                # module so (a) their functions become callable from the
                # importing program and (b) codegen emits and links the C/LLVM
                # body. The sub-analyzer already parsed the block and cached the
                # symbols on the node under its own scope; re-register them on
                # this analyzer's globals and keep the node in sub_ast so
                # `emit_ccode`/`emit_llvm` runs at codegen.
                existing_syms = getattr(ast_node, "symbols", None) or []
                if existing_syms:
                    self._register_import_symbols(
                        dict(existing_syms),
                        ast_node,
                        var_names=getattr(ast_node, "var_names", set()) or set(),
                    )
                else:
                    if isinstance(ast_node, CCode):
                        self._visit_ccode(ast_node)
                    else:
                        self._visit_llvm(ast_node)
                sub_ast.append(ast_node)
            elif isinstance(ast_node, FuncDef) and ast_node.visibility == "public":
                params = [(name, ptype) for name, ptype in ast_node.params.items()]
                ret_type = ast_node.rettype or "int"
                symbols[ast_node.name] = (ret_type, params, False)
                sub_ast.append(ast_node)
            elif isinstance(ast_node, StructDef):
                sub_ast.append(ast_node)
                existing = self.globals.lookup_local(ast_node.name)
                if not existing:
                    self.globals.define(ast_node.name, Symbol("struct", None, ast_node))
            elif isinstance(ast_node, Import):
                for fname, (ret_type, params, vararg) in ast_node.symbols:
                    if fname not in symbols:
                        symbols[fname] = (ret_type, params, vararg)

                # Preserve the sub-AST chain for re-exported imports so codegen
                # can still reach the aggregated module's function definitions
                # (emit_program recurses through Import sub_ast).
                if getattr(ast_node, "sub_ast", None):
                    sub_ast.append(ast_node)

        if node is not None:
            node.sub_ast = sub_ast
        return symbols, "cpy"

    def _visit_dict(self, node: dict, scope: Scope | None = None):
        t = node.get("type")
        if t == "while":
            self._visit_while(node, scope)
        elif t == "for":
            self._visit_for(node, scope)

    def _visit_funcdef(self, node: FuncDef, scope: Scope | None = None):
        s = scope or self.globals
        existing = s.lookup_local(node.name)
        if existing and node.name not in self._forward_names:
            self.error(
                f"redefinition of `{node.name}`",
                node,
                note="a function with this name already exists in this scope",
            )
            return
        if existing:
            self._forward_names.discard(node.name)

        if node.rettype == "decorated":
            has_code = any(
                isinstance(stmt, ExprStmt)
                and isinstance(stmt.expr, Call)
                and isinstance(stmt.expr.callee, Variable)
                and stmt.expr.callee.name == "code"
                for stmt in node.body
            )
            if not has_code:
                self.error(
                    "decorator function with return type `decorated` must call code()",
                    node,
                    note="code() invokes the original function; "
                    "without it the decorator has no effect",
                )

        sym = Symbol("function", node.rettype or "void", node)
        s.define(node.name, sym)

        old_func = self.current_func
        self.current_func = node
        old_in_decorator = self._in_decorator
        self._in_decorator = node.rettype == "decorated"
        old_locals = self.locals
        self.locals = Scope(s)

        const_params = set(getattr(node, "const_params", None) or ())
        for param_name, param_type in node.params.items():
            if param_name in const_params:
                # Constant-view parameter: read-only inside the function, but the
                # caller's value stays fully modifiable outside.
                self.locals.define(
                    param_name, Symbol("const_view", param_type or None, node)
                )
            else:
                psym = Symbol("variable", param_type or None, node)
                if param_type == "dynamic":
                    psym.dynamic = True
                self.locals.define(param_name, psym)

        for stmt in node.body:
            self._visit(stmt, self.locals)

        self._check_ownership(node.body, node)
        self._check_lints(node.body, node)

        self.current_func = old_func
        self._in_decorator = old_in_decorator
        self.locals = old_locals

    def _check_lints(self, body, fn):
        """Hygiene lints: unused-local warnings, unreachable-code warnings, and
        a missing-return error for non-void functions that can fall off the end.

        All diagnostics here are advisory (best-effort over simple control flow)
        and are gated so they never break a valid compile: unused/unreachable are
        warnings; a definitely-missing return is an error only when the function
        has no `return` statement anywhere in its body.
        """
        reads: set = set()
        declared: list = []  # (name, VarDecl-node)
        written: set = set()
        terminates: set = set()  # names of `return`-terminated statements
        has_return = [False]

        def walk(stmt, terminated):
            """Return True if the rest of the enclosing block is unreachable."""
            if isinstance(stmt, Return):
                has_return[0] = True
                names = set()
                _collect(stmt.value, names)
                reads.update(names)
                return True
            if isinstance(stmt, (Break, Continue, Raise)):
                return True
            if isinstance(stmt, VarDecl):
                if stmt.name and not stmt.name.startswith("_"):
                    declared.append((stmt.name, stmt, terminated))
                # reads capture the RHS
                names = set()
                _collect(stmt.init, names)
                reads.update(names)
                return False
            if isinstance(stmt, Assign):
                if isinstance(stmt.target, Variable):
                    written.add(stmt.target.name)
                else:
                    # e.g. `*p = v` or `arr[i] = v` — the base of the target is a
                    # read of the underlying variable.
                    names = set()
                    _collect(stmt.target, names)
                    reads.update(names)
                names = set()
                _collect(stmt.value, names)
                reads.update(names)
                return False
            if isinstance(stmt, Print):
                for arg in stmt.value:
                    names = set()
                    _collect(arg, names)
                    reads.update(names)
                return False
            if isinstance(stmt, ExprStmt):
                names = set()
                _collect(stmt.expr, names)
                reads.update(names)
                return False
            if isinstance(stmt, DeferStmt):
                names = set()
                _collect(stmt.body, names)
                reads.update(names)
                return False
            if isinstance(stmt, If):
                names = set()
                _collect(stmt.cond, names)
                reads.update(names)
                then_ret = False
                els_ret = False
                for s in stmt.body:
                    then_ret = walk(s, then_ret) or then_ret
                for s in getattr(stmt, "orelse", None) or []:
                    els_ret = walk(s, els_ret) or els_ret
                # If the condition is a constant true/false, the other branch is
                # unreachable; otherwise fall through only if both branches return.
                return then_ret and els_ret
            if isinstance(stmt, While):
                names = set()
                _collect(stmt.cond, names)
                reads.update(names)
                for s in stmt.body:
                    walk(s, False)
                # a `while True:` without a break is non-terminating; treat as
                # terminating only if the loop body is unreachable-agnostic.
                return False
            if isinstance(stmt, Switch):
                for branch in getattr(stmt, "cases", []):
                    names = set()
                    _collect(branch[0], names)
                    reads.update(names)
                    for s in branch[1]:
                        walk(s, False)
                return False
            # default container
            for child in getattr(stmt, "body", None) or []:
                walk(child, False)
            return False

        def _collect(node, acc):
            if node is None:
                return
            if isinstance(node, Variable):
                acc.add(node.name)
                return
            for child in self._infer_children(node):
                _collect(child, acc)
            # Some expression nodes aren't covered by _infer_children (e.g.
            # print args, move/borrow operands).
            for attr in ("operand", "expr"):
                child = getattr(node, attr, None)
                if child is not None and not isinstance(child, Variable):
                    _collect(child, acc)

        for stmt in body:
            terminated = walk(stmt, False)
            # If a statement sets terminated True, subsequent statements in the
            # same block are unreachable — handled via the walker's signal.
            if terminated:
                continue
        # Unreachable-code warning: statements in a block after a terminator.
        self._warn_unreachable(body)

        # Unused-local warnings (only for locals, skipping params/_ -prefixed).
        param_names = set(fn.params.keys())
        for name, node, _term in declared:
            if name in param_names or name.startswith("_"):
                continue
            if name in reads:
                continue
            self.warning(
                f"local `{name}` is assigned but never used",
                node,
                span=len(name),
                code="W1001",
                note="remove it, or prefix with `_` to silence this warning",
            )

        # Missing-return lint: a non-void function that has no `return` at all.
        # cpyte implicitly returns 0, so this is advisory rather than a fatal
        # error — it flags a likely-intended `return <value>` omission.
        rettype = fn.rettype
        if (
            rettype
            and rettype not in ("void", "auto", "dynamic", "decorated")
            and not has_return[0]
        ):
            self.warning(
                f"missing `return` in function `{fn.name}` returning `{rettype}`; "
                f"it will implicitly return 0",
                fn,
                span=0,
                code="W1002",
                note="add a `return <value>` before the end of the function body",
            )

    def _warn_unreachable(self, body):
        for stmt in body:
            if isinstance(stmt, (Return, Break, Continue, Raise)):
                idx = body.index(stmt) + 1
                for after in body[idx:]:
                    self.warning(
                        "unreachable code",
                        after,
                        span=1,
                        code="W1003",
                        note="this statement follows a return/break/continue and "
                        "will never run",
                    )
                break
            if isinstance(stmt, If):
                self._warn_unreachable(stmt.body)
                self._warn_unreachable(getattr(stmt, "orelse", None) or [])
            elif isinstance(stmt, While):
                self._warn_unreachable(stmt.body)
            elif isinstance(stmt, Switch):
                for branch in getattr(stmt, "cases", []):
                    self._warn_unreachable(branch[1])
            elif hasattr(stmt, "body") and isinstance(stmt.body, list):
                self._warn_unreachable(stmt.body)

    def _check_ownership(self, body, fn):
        """Intra-procedural ownership + memory analysis (supportive, not a
        full sound borrow checker).

        Tracks, per named variable, whether it holds a `new` allocation and
        whether it has been freed/moved. Emits:
          * warning when a `new`-allocated variable is never freed (unmanaged
            memory / leak),
          * warning when a `new`-allocated variable is overwritten without
            freeing it first,
          * error when a variable is used after `move`,
          * warning when a value is mutated while it has an outstanding
            immutable borrow.
        Because cpyte has no runtime ownership metadata, this is best-effort
        static analysis over the function's statement list, walking into
        branch/loop bodies sequentially.
        """
        # state: name -> 'owned' | 'freed' | 'moved' | 'plain'
        # borrowed: name -> set of "ro"/"mut" outstanding borrows
        state: dict = {}
        borrowed: dict = {}
        visited: set = set()
        escaped: set = set()  # names mentioned in any `return` value

        def is_new_expr(v):
            if isinstance(v, NewExpr):
                return True
            if isinstance(v, CastExpr):
                return is_new_expr(v.expr)
            if isinstance(v, BorrowExpr) or isinstance(v, MoveExpr):
                return False
            return False

        _HEAP_ALLOC_FNS = ("malloc", "calloc", "realloc")

        def is_heap_alloc_expr(v):
            """True for `new` allocations and raw C heap allocs (`malloc`,
            `calloc`, `realloc`), so ownership tracking matches how these are
            actually reclaimed (manual `free`)."""
            if isinstance(v, NewExpr):
                return True
            if isinstance(v, CastExpr):
                return is_heap_alloc_expr(v.expr)
            if (
                isinstance(v, Call)
                and isinstance(v.callee, Variable)
                and v.callee.name in _HEAP_ALLOC_FNS
                and len(v.args) >= 1
            ):
                return True
            return False

        def free_target(v):
            """If expression is a free()/deferring-free call over a variable,
            return the variable name, else None."""
            val = v
            if isinstance(val, DeferStmt):
                val = val.body
            if isinstance(val, ExprStmt):
                val = val.expr
            if (
                isinstance(val, Call)
                and isinstance(val.callee, Variable)
                and val.callee.name == "free"
                and len(val.args) == 1
                and isinstance(val.args[0], Variable)
            ):
                return val.args[0].name
            return None

        def used_names(v, acc):
            if isinstance(v, Variable):
                acc.add(v.name)
            for child in self._infer_children(v):
                used_names(child, acc)

        def mark_owned(name, node):
            if state.get(name) == "owned":
                self.warning(
                    f"unmanaged memory: `{name}` already holds a heap allocation "
                    f"that is never freed before being overwritten",
                    node,
                    note=f"use `free({name})` (or `defer free({name})`) before reassigning",
                )
            state[name] = "owned"

        def walk_stmt(stmt, frame):
            if id(stmt) in visited:
                return
            if isinstance(stmt, VarDecl):
                if is_heap_alloc_expr(stmt.init):
                    mark_owned(stmt.name, stmt)
                elif isinstance(stmt.init, MoveExpr) and isinstance(
                    stmt.init.operand, Variable
                ):
                    src = stmt.init.operand.name
                    if state.get(src) == "moved":
                        self.error(
                            f"`{src}` is already moved",
                            stmt,
                            note="cannot move a value that has already been moved",
                        )
                    if state.get(stmt.name) == "owned":
                        self.warning(
                            f"assigning `move {src}` to `{stmt.name}` drops its previous "
                            f"heap allocation without freeing it",
                            stmt,
                        )
                    state[stmt.name] = state.get(src) or "plain"
                    state[src] = "moved"
                elif isinstance(stmt.init, BorrowExpr) and isinstance(
                    stmt.init.operand, Variable
                ):
                    borrowed.setdefault(stmt.init.operand.name, set()).add(
                        "mut" if stmt.init.mutable else "ro"
                    )
                return
            if isinstance(stmt, Assign):
                if isinstance(stmt.target, Variable):
                    name = stmt.target.name
                    if "ro" in borrowed.get(name, set()):
                        self.warning(
                            f"mutating `{name}` while it is immutably borrowed",
                            stmt,
                            note=f"`borrow {name}` forbids mutation until the borrow ends",
                        )
                    if state.get(name) == "moved":
                        self.error(
                            f"cannot reinitialize `{name}`; it was already moved",
                            stmt,
                        )
                    if is_heap_alloc_expr(stmt.value):
                        mark_owned(name, stmt)
                    elif isinstance(stmt.value, MoveExpr) and isinstance(
                        stmt.value.operand, Variable
                    ):
                        src = stmt.value.operand.name
                        if state.get(src) == "moved":
                            self.error(
                                f"`{src}` is already moved",
                                stmt,
                                note="cannot move a value that has already been moved",
                            )
                        if state.get(name) == "owned":
                            self.warning(
                                f"moving `{src}` into `{name}` drops its previous "
                                f"heap allocation without freeing it",
                                stmt,
                            )
                        st = state.get(src) or "plain"
                        state[name] = st
                        state[src] = "moved"
                    else:
                        if state.get(name) == "owned":
                            # reassigning to a non-freed owned var leaks the old
                            # allocation unless it is an internal pointer update.
                            self.warning(
                                f"unmanaged memory: reassigning `{name}` discards its "
                                f"previous heap allocation without freeing it",
                                stmt,
                                note=f"call `free({name})` first (e.g. `defer free({name})`)",
                            )
                    if isinstance(stmt.value, BorrowExpr) and isinstance(
                        stmt.value.operand, Variable
                    ):
                        borrowed.setdefault(stmt.value.operand.name, set()).add(
                            "mut" if stmt.value.mutable else "ro"
                        )
                else:
                    # Store through a non-variable target (`p[0] = v`,
                    # `(*p).f = v`, `p.f = v`): reads of freed/moved pointers in
                    # the target and value are still use-after-free.
                    names = set()
                    used_names(stmt.target, names)
                    if stmt.value is not None:
                        used_names(stmt.value, names)
                    for n in names:
                        if state.get(n) == "freed":
                            self.warning(
                                f"use of `{n}` after it was freed",
                                stmt,
                                note="`free()` releases the memory; the value is dangling",
                            )
                return
            if isinstance(stmt, Return):
                names = set()
                if stmt.value is not None:
                    if isinstance(stmt.value, (list, tuple)):
                        for item in stmt.value:
                            used_names(item, names)
                    else:
                        used_names(stmt.value, names)
                for n in names:
                    if state.get(n) == "freed":
                        self.warning(
                            f"use of `{n}` after it was freed",
                            stmt,
                            note="`free()` releases the memory; the value is dangling",
                        )
                escaped.update(names)
                return
            if isinstance(stmt, Print):
                names = set()
                for arg in stmt.value:
                    if isinstance(arg, (list, tuple)):
                        for item in arg:
                            used_names(item, names)
                    else:
                        used_names(arg, names)
                for n in names:
                    if state.get(n) == "moved":
                        self.error(
                            f"use of `{n}` after it was moved",
                            stmt,
                            note="`move` transfers ownership; the value is no longer valid",
                        )
                    elif state.get(n) == "freed":
                        self.warning(
                            f"use of `{n}` after it was freed",
                            stmt,
                            note="`free()` releases the memory; the value is dangling",
                        )
                return
            if isinstance(stmt, (If, While)):
                for s in stmt.body:
                    walk_stmt(s, frame)
                for s in getattr(stmt, "orelse", None) or []:
                    walk_stmt(s, frame)
                return
            if isinstance(stmt, DeferStmt):
                ft = free_target(stmt)
                if ft is not None:
                    if not self.no_gc:
                        self.warning(
                            f"manual `free({ft})` while the GC is enabled",
                            stmt,
                            note="GC-managed `new` allocations are reclaimed by the GC; "
                            "manually freeing them risks a double-free. Mark this "
                            "module `#nogc` to manage memory manually.",
                        )
                    if state.get(ft) == "freed":
                        self.warning(
                            f"`{ft}` is already freed",
                            stmt,
                            note="double-free of a heap allocation",
                        )
                    elif state.get(ft) == "moved":
                        self.warning(
                            f"`free({ft})` called on a value that was already moved",
                            stmt,
                        )
                    elif state.get(ft) == "plain":
                        self.warning(
                            f"`free({ft})` called on a value that was not allocated "
                            f"with `new`/`malloc` in this function",
                            stmt,
                        )
                    state[ft] = "freed"
                    return
            if isinstance(stmt, ExprStmt):
                e = stmt.expr
                ft = free_target(stmt)
                if ft is not None:
                    if not self.no_gc:
                        self.warning(
                            f"manual `free({ft})` while the GC is enabled",
                            stmt,
                            note="GC-managed `new` allocations are reclaimed by the GC; "
                            "manually freeing them risks a double-free. Mark this "
                            "module `#nogc` to manage memory manually.",
                        )
                    if state.get(ft) == "freed":
                        self.warning(
                            f"`{ft}` is freed more than once",
                            stmt,
                            note="double-free of a heap allocation",
                        )
                    elif state.get(ft) == "moved":
                        self.warning(
                            f"`free({ft})` called on a value that was already moved",
                            stmt,
                        )
                    elif state.get(ft) == "plain":
                        self.warning(
                            f"`free({ft})` called on a value that was not allocated "
                            f"with `new`/`malloc` in this function",
                            stmt,
                        )
                    state[ft] = "freed"
                    return
                # Generic expression walking: detect moves, borrows, use-after-move.
                if isinstance(e, MoveExpr) and isinstance(e.operand, Variable):
                    name = e.operand.name
                    state[name] = "moved"
                    return
                if isinstance(e, BorrowExpr) and isinstance(e.operand, Variable):
                    b = borrowed.setdefault(e.operand.name, set())
                    b.add("mut" if e.mutable else "ro")
                    return
                # plain expression statement — detect use-after-move + mutation of
                # immutably-borrowed vars.
                names = set()
                used_names(e, names)
                for n in names:
                    if state.get(n) == "moved":
                        self.error(
                            f"use of `{n}` after it was moved",
                            stmt,
                            note="`move` transfers ownership; the value is no longer valid",
                        )
                    elif state.get(n) == "freed":
                        self.warning(
                            f"use of `{n}` after it was freed",
                            stmt,
                            note="`free()` releases the memory; the value is dangling",
                        )
                # detect writes to immutably-borrowed variables (best effort)
                tgt = None
                if isinstance(e, Assign):
                    if isinstance(e.target, Variable):
                        tgt = e.target.name
                elif isinstance(e, Call) and isinstance(e.callee, Variable):
                    pass
                if tgt is not None and "ro" in borrowed.get(tgt, set()):
                    self.warning(
                        f"mutating `{tgt}` while it is immutably borrowed",
                        stmt,
                        note="`borrow {tgt}` forbids mutation until the borrow ends",
                    )
                return
            # fallback: recurse into known container bodies
            for child in getattr(stmt, "body", None) or []:
                if isinstance(
                    child,
                    (ExprStmt, VarDecl, Assign, If, While, DeferStmt, Return, Print),
                ):
                    walk_stmt(child, frame)

        for stmt in body:
            if not isinstance(
                stmt, (ExprStmt, VarDecl, Assign, If, While, DeferStmt, Return, Print)
            ):
                continue
            walk_stmt(stmt, fn)

        # Final leak warning for allocations still owned at function end.
        # Only meaningful without the GC: with the GC enabled, `new` memory is
        # reclaimed automatically, so a manual "leak" is not an actual leak.
        if self.no_gc:
            for name, st in state.items():
                if st == "owned" and name not in escaped:
                    self.warning(
                        f"unmanaged memory: heap allocation held by `{name}` is never freed",
                        fn,
                        note=f"add `defer free({name})` to release it on scope exit",
                    )

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
                if (
                    expected
                    and val_type
                    and _array_family(expected) != _array_family(val_type)
                    and expected != "dynamic"
                    and val_type != "dynamic"
                ):
                    valid_conversions = [
                        ("int", "int64"),
                        ("int", "uint64"),
                        ("int64", "int"),
                        ("uint64", "int"),
                        ("int64", "uint64"),
                        ("uint64", "int64"),
                        ("float", "double"),
                        ("double", "float"),
                        ("str", "char*"),
                        ("char*", "str"),
                        ("str", "char"),
                        ("char", "str"),
                        ("int", "big"),
                        ("int64", "big"),
                        ("uint64", "big"),
                        ("big", "big"),
                        ("int", "ubig"),
                        ("int64", "ubig"),
                        ("uint64", "ubig"),
                        ("size_t", "ubig"),
                        ("big", "ubig"),
                        ("ubig", "ubig"),
                        ("ubig", "big"),
                    ]
                    ok = (val_type, expected) in valid_conversions
                    ok = ok or (val_type == "int" and expected.endswith("*"))
                    ok = ok or (
                        val_type == "str"
                        and (expected.endswith("*") or expected == "char")
                    )
                    ok = ok or (
                        expected == "str"
                        and (val_type.endswith("*") or val_type == "char")
                    )
                    if not ok:
                        self.error(
                            f"return type `{val_type}` does not match declared return type `{expected}`",
                            node,
                            note=f"in function `{self.current_func.name}`",
                        )
        else:
            if (
                self.current_func
                and self.current_func.rettype
                and self.current_func.rettype != "void"
            ):
                self.error(
                    f"missing return value in function returning `{self.current_func.rettype}`",
                    node,
                    note=f"function `{self.current_func.name}` expects a return value of type `{self.current_func.rettype}`",
                )

    def _is_negative_constant(self, expr):
        # True for compile-time-negative literals like `-5`. ubig is "its own
        # parse (no minus)", so a negative constant must never enter an unsigned
        # big value.
        if isinstance(expr, UnaryOp):
            if getattr(expr, "op", None) is not None and expr.op.name == "MINUS":
                return isinstance(expr.operand, Number)
        return False

    def _check_ubig_no_negative(self, expr, node, ctx):
        if self._is_negative_constant(expr):
            self.error(
                "cannot store a negative constant in a `ubig`",
                node,
                note="`ubig` is unsigned (its own parse has no minus); use a `big` "
                f"or non-negative value instead ({ctx})",
            )

    def _visit_assign(self, node: Assign, scope: Scope | None = None):
        val_type = self._infer_type(node.value)
        if isinstance(node.target, (Variable, str)):
            name = (
                node.target.name if isinstance(node.target, Variable) else node.target
            )
            if self._in_decorator and name == "result":
                if isinstance(node.target, Variable):
                    node.target.inferred_type = "dynamic"
                    node.target.dynamic = True
                return
            s = scope or self.current_scope
            existing = s.lookup_local(name)
            if existing is None:
                existing = s.lookup(name)
            if existing is None:
                existing = Symbol("variable", val_type, node)
                s.define(name, existing)
            elif existing.kind == "const":
                self.error(
                    f"cannot assign to constant `{name}`",
                    node,
                    note="constants are immutable once declared; "
                    "declare a variable with `{type} {name} = ...` if you need to reassign it",
                )
            elif existing.kind == "const_view":
                self.error(
                    f"cannot assign to constant-view parameter `{name}`",
                    node,
                    note="constant-view parameters are read-only inside the function; "
                    "copy to a local variable to modify the value",
                )
            elif existing.type == "dynamic":
                # Dynamically-typed variable: accepts values of any type.
                existing.dynamic = True
                existing.initialized = True
                node.dynamic = True
                if isinstance(node.target, Variable):
                    node.target.dynamic = True
            elif val_type == "dynamic":
                # Assigning a dynamically-typed value into a concrete variable:
                # the runtime kind is checked/coerced at run time.
                if existing.kind == "variable":
                    existing.initialized = True
            if existing is not None and existing.type == "ubig":
                self._check_ubig_no_negative(node.value, node, "assignment")
            elif (
                val_type is not None
                and existing.type is not None
                and _array_family(val_type) != _array_family(existing.type)
            ):
                # Allow implicit conversion from int to int64/uint64
                # Allow implicit conversion between int64 and uint64
                # Allow int literal 0 as null for any pointer type
                # Allow float/double interchange (same LLVM type)
                # Allow str/char* interchange (same LLVM type)
                if existing.kind == "variable":
                    existing.initialized = True
                narrowing = {
                    ("int64", "int"),
                    ("uint64", "int"),
                    ("double", "float"),
                }
                valid_conversions = [
                    ("int", "int64"),
                    ("int", "uint64"),
                    ("int", "size_t"),
                    ("int64", "int"),
                    ("uint64", "int"),
                    ("int64", "uint64"),
                    ("uint64", "int64"),
                    ("int64", "size_t"),
                    ("uint64", "size_t"),
                    ("size_t", "int"),
                    ("size_t", "int64"),
                    ("size_t", "uint64"),
                    ("float", "double"),
                    ("double", "float"),
                    ("str", "char"),
                    ("char", "str"),
                    ("int", "big"),
                    ("int64", "big"),
                    ("uint64", "big"),
                    ("size_t", "big"),
                    ("big", "big"),
                    ("int", "ubig"),
                    ("int64", "ubig"),
                    ("uint64", "ubig"),
                    ("size_t", "ubig"),
                    ("big", "ubig"),
                    ("ubig", "ubig"),
                    ("ubig", "big"),
                ]
                ok = (val_type, existing.type) in valid_conversions
                ok = ok or (val_type == "int" and existing.type.endswith("*"))
                ok = ok or (
                    val_type == "str"
                    and (existing.type.endswith("*") or existing.type == "char")
                )
                ok = ok or (
                    existing.type == "str"
                    and (val_type.endswith("*") or val_type == "char")
                )
                if ok and (val_type, existing.type) in narrowing:
                    self._strict_error(
                        f"narrowing conversion from `{val_type}` to `{existing.type}` in assignment",
                        node,
                    )
                if ok and (val_type == "int" and existing.type.endswith("*")):
                    self._strict_warning(
                        f"implicit int-to-pointer conversion in assignment to `{existing.type}`",
                        node,
                        note="use 0 literal for null pointer",
                    )
                if not ok:
                    if (
                        existing.kind == "variable"
                        and existing.node is not None
                        and isinstance(existing.node, (VarDecl, Assign))
                    ):
                        # Promote the variable to dynamic: once a variable holds
                        # a value of a different type it becomes a runtime-typed
                        # (dynamic) variable. Subsequent assignments of any type
                        # are then allowed.
                        existing.dynamic = True
                        existing.initialized = True
                        self._promoted_vars.add(self._promoted_key(name))
                        existing.node.dynamic = True
                    else:
                        self.error(
                            f"cannot assign `{val_type}` to variable `{name}` of type `{existing.type}`",
                            node,
                        )
            elif existing.kind == "variable":
                existing.initialized = True
        elif isinstance(node.target, Attr):
            obj = node.target.obj
            if isinstance(obj, Variable):
                s = scope or self.current_scope
                obj_sym = s.lookup_local(obj.name)
                if obj_sym is None:
                    obj_sym = s.lookup(obj.name)
                if obj_sym is not None and obj_sym.kind == "variable":
                    obj_sym.initialized = True
            obj_t = self._infer_type(node.target.obj)
            if obj_t:
                lookup_t = obj_t.removesuffix("*")
                struct_sym = (
                    scope.lookup(lookup_t)
                    if scope
                    else self.current_scope.lookup(lookup_t)
                )
                if struct_sym and struct_sym.kind == "struct" and struct_sym.node:
                    for field in struct_sym.node.fields:
                        if (
                            field.name == node.target.name
                            and val_type is not None
                            and field.type_expr != val_type
                        ):
                            ok = val_type == "int" and field.type_expr.endswith("*")
                            ok = ok or (
                                val_type == "void*" and field.type_expr.endswith("*")
                            )
                            ok = ok or (
                                field.type_expr == "void*" and val_type.endswith("*")
                            )
                            ok = ok or (
                                val_type == "str"
                                and (
                                    field.type_expr.endswith("*")
                                    or field.type_expr == "char"
                                )
                            )
                            ok = ok or (
                                field.type_expr == "str"
                                and (val_type.endswith("*") or val_type == "char")
                            )
                            ok = ok or (val_type, field.type_expr) in (
                                ("int", "int64"),
                                ("int", "uint64"),
                                ("int", "size_t"),
                                ("int64", "uint64"),
                                ("int64", "size_t"),
                                ("uint64", "int64"),
                                ("uint64", "size_t"),
                                ("size_t", "int"),
                                ("size_t", "int64"),
                                ("size_t", "uint64"),
                                ("float", "double"),
                                ("double", "float"),
                                ("str", "char"),
                                ("char", "str"),
                            )
                            if not ok:
                                self.error(
                                    f"cannot assign `{val_type}` to field `{node.target.name}` of type `{field.type_expr}`",
                                    node,
                                )
                            break
        else:
            self._infer_type(node.target)

    def _visit_vardecl(self, node: VarDecl, scope: Scope | None = None):
        val_type = self._resolve_type_alias(node.var_type)
        node.var_type = val_type
        if val_type:
            self._check_generic_type(val_type)
        s = scope or self.current_scope
        existing = s.lookup_local(node.name)
        if existing and existing.kind not in ("variable", "const"):
            self.error(
                f"redeclaration of `{node.name}`",
                node,
                note="another symbol with this name already exists in this scope",
            )
        if node.init is None and val_type:
            # A bare fixed-size array declaration `int[5] x` allocates the array
            # like `x = new int[5]`; the fixed size is a type property, not a
            # runtime shape (unlike `T[]` which gets a value via `new`).
            m = _FIXED_ARRAY_RE.match(val_type)
            if m is not None:
                node.init = NewExpr(m.group(1), Number(m.group(2)), token=node._token)
        if node.is_const and node.init is None:
            self.error(
                f"constant `{node.name}` requires an initializer",
                node,
                note=f"declare it as `{node.var_type} ({node.name}) = <value>`",
            )
        if node.init is not None:
            init_type = self._infer_type(node.init)
            if val_type == "ubig":
                self._check_ubig_no_negative(node.init, node, "declaration")
            if val_type == "dynamic":
                # Dynamically-typed variable: accepts any initializer type.
                node.dynamic = True
            elif init_type == "dynamic":
                # Dynamically-typed initializer coerced to the declared type at run time.
                pass
            elif (
                init_type is not None
                and val_type is not None
                and _array_family(init_type) != _array_family(val_type)
            ):
                # Allow implicit conversion from int to int64/uint64
                # Allow implicit conversion between int64 and uint64
                # Allow int literal 0 as null for any pointer type
                # Allow float/double interchange
                # Allow str/char* interchange
                # Allow char to int (widening)
                narrowing = {
                    ("int64", "int"),
                    ("uint64", "int"),
                    ("double", "float"),
                }
                valid_conversions = [
                    ("int", "int64"),
                    ("int", "uint64"),
                    ("int", "size_t"),
                    ("int64", "int"),
                    ("uint64", "int"),
                    ("int64", "uint64"),
                    ("uint64", "int64"),
                    ("int64", "size_t"),
                    ("uint64", "size_t"),
                    ("size_t", "int"),
                    ("size_t", "int64"),
                    ("size_t", "uint64"),
                    ("float", "double"),
                    ("double", "float"),
                    ("str", "char"),
                    ("char", "str"),
                    ("char", "int"),
                    ("int", "big"),
                    ("int64", "big"),
                    ("uint64", "big"),
                    ("size_t", "big"),
                    ("big", "big"),
                    ("int", "ubig"),
                    ("int64", "ubig"),
                    ("uint64", "ubig"),
                    ("size_t", "ubig"),
                    ("big", "ubig"),
                    ("ubig", "ubig"),
                    ("ubig", "big"),
                ]
                ok = (init_type, val_type) in valid_conversions
                ok = ok or (init_type == "int" and val_type.endswith("*"))
                ok = ok or (
                    init_type == "str"
                    and (val_type.endswith("*") or val_type == "char")
                )
                ok = ok or (
                    val_type == "str"
                    and (init_type.endswith("*") or init_type == "char")
                )
                if ok and (init_type, val_type) in narrowing:
                    self._strict_error(
                        f"narrowing conversion from `{init_type}` to `{val_type}` in variable declaration",
                        node,
                    )
                if ok and (init_type == "int" and val_type.endswith("*")):
                    self._strict_warning(
                        f"implicit int-to-pointer conversion in declaration of `{val_type}`",
                        node,
                        note="use 0 literal for null pointer",
                    )
                if not ok:
                    self.error(
                        f"cannot initialize `{val_type}` variable with value of type `{init_type}`",
                        node,
                    )
        kind = "const" if node.is_const else "variable"
        is_global = s is self.globals
        sym = Symbol(
            kind, val_type, node, initialized=(node.init is not None or is_global)
        )
        if val_type == "dynamic":
            sym.dynamic = True
        s.define(node.name, sym)

    def _visit_break(self, node: Break):
        if self._loop_depth == 0:
            self.error("break outside loop", node)

    def _visit_continue(self, node: Continue):
        if self._loop_depth == 0:
            self.error("continue outside loop", node)

    def _visit_assert(self, node: Assert):
        self._infer_type(node.cond)
        if node.message is not None:
            self._infer_type(node.message)

    def _visit_switch(self, node: Switch, scope: Scope | None = None):
        val_t = self._infer_type(node.value)
        for val, body in node.cases:
            if val is not None:
                case_t = self._infer_type(val)
                if val_t == "str" and case_t != "str":
                    self.error(
                        "case value must be a string when switching on a string",
                        val,
                        note="string switch cases are shell-style glob patterns "
                        '(e.g. `a-*` matches any string starting with "a-")',
                    )
            for stmt in body:
                self._visit(stmt, scope)

    def _visit_print(self, node: Print):
        for expr in node.value:
            self._infer_type(expr)

    def _visit_class(self, node: ClassDef, scope: Scope | None = None):
        s = scope or self.globals
        class_sym = Symbol("class", node.name, node)
        s.define(node.name, class_sym)
        class_scope = Scope(s)

        # Resolve base class
        base_sym = None
        if node.base:
            base_sym = s.lookup(node.base)
            if base_sym is None or base_sym.kind != "class":
                self.error(f"base class `{node.base}` not found", node)
            elif base_sym.node and isinstance(base_sym.node, ClassDef):
                # Copy base class fields (for memory layout)
                for f in base_sym.node.fields:
                    class_scope.define(f.name, Symbol("field", f.type_expr, f))
                # Copy base class methods
                for m in base_sym.node.methods:
                    existing = class_scope.lookup_local(m.name)
                    if not existing:
                        sym = Symbol(m.visibility or "public", m.rettype or "void", m)
                        class_scope.define(m.name, sym)

        # Register fields in class scope
        for f in node.fields:
            existing = class_scope.lookup_local(f.name)
            if existing:
                class_scope.undefine(f.name)
            class_scope.define(f.name, Symbol("field", f.type_expr, f))

        # Visit methods (second pass: analyze bodies, which registers signatures)
        old_class = self.current_class
        self.current_class = node
        for m in node.methods:
            # Inject 'this' parameter implicitly
            m.params = {"this": node.name + "*", **m.params}
            self._visit_funcdef(m, class_scope)
        self.current_class = old_class

    def _visit_try(self, node: Try, scope: Scope | None = None):
        for stmt in node.body:
            self._visit(stmt, scope)
        for handler in node.handlers:
            if handler.type_name:
                handler_sym = self.current_scope.lookup(handler.type_name)
                if handler_sym is None:
                    self.error(
                        f"undefined exception type `{handler.type_name}`", handler
                    )
            for stmt in handler.body:
                self._visit(stmt, scope)

    def _visit_raise(self, node: Raise):
        sym = self.current_scope.lookup(node.exc_type)
        if sym is None:
            self.error(f"undefined exception class `{node.exc_type}`", node)
        self._infer_type(node.message)

    def _visit_struct(self, node: StructDef, scope: Scope | None = None):
        s = scope or self.globals
        existing = s.lookup_local(node.name)
        if existing and node.name not in self._forward_names:
            self.error(f"redefinition of struct `{node.name}`", node)
            return
        if existing:
            self._forward_names.discard(node.name)
        s.define(node.name, Symbol("struct", None, node))
        struct_scope = Scope(s)
        for param in node.generic_params:
            struct_scope.define(param, Symbol("type_param", None, node))
        for field in node.fields:
            struct_scope.define(field.name, Symbol("field", field.type_expr, node))

    def _visit_enum(self, node: EnumDef, scope: Scope | None = None):
        s = scope or self.globals
        existing = s.lookup_local(node.name)
        if existing and node.name not in self._forward_names:
            self.error(f"redefinition of enum `{node.name}`", node)
            return
        if existing:
            self._forward_names.discard(node.name)
        s.define(node.name, Symbol("enum", None, node))
        prev_ctx = self._enum_context_name
        self._enum_context_name = node.name
        next_val = 0
        try:
            for member in node.members:
                if member["value"] is not None:
                    self._infer_type(member["value"])
                    val = self._eval_const_expr(member["value"], s)
                    member["_const_value"] = val
                    if isinstance(val, int):
                        next_val = val + 1
                else:
                    member["_const_value"] = next_val
                    member["_auto_index"] = next_val
                    next_val += 1
                sym = Symbol("enum_member", node.name, node)
                sym.const_value = member["_const_value"]
                s.define(f'{node.name}.{member["name"]}', sym)
        finally:
            self._enum_context_name = prev_ctx

    def _check_generic_type(self, type_str: str):
        """If type_str is a generic instantiation like 'Pair<int, string>',
        record that the base struct needs monomorphization."""
        if not type_str or "<" not in type_str:
            return
        idx = type_str.index("<")
        base_name = type_str[:idx]
        struct_sym = self.globals.lookup(base_name)
        if (
            struct_sym
            and struct_sym.kind == "struct"
            and isinstance(struct_sym.node, StructDef)
        ):
            if struct_sym.node.generic_params:
                args_str = type_str[idx + 1 : -1]  # strip < and >
                # Split on commas (respecting nested generics)
                args = []
                depth = 0
                current = ""
                for ch in args_str:
                    if ch == "<":
                        depth += 1
                        current += ch
                    elif ch == ">":
                        depth -= 1
                        current += ch
                    elif ch == "," and depth == 0:
                        args.append(current.strip())
                        current = ""
                    else:
                        current += ch
                if current.strip():
                    args.append(current.strip())
                args_tuple = tuple(args)
                if base_name not in self._generic_instantiations:
                    self._generic_instantiations[base_name] = []
                if args_tuple not in self._generic_instantiations[base_name]:
                    self._generic_instantiations[base_name].append(args_tuple)

    def _visit_type_alias(self, node: TypeAlias, scope: Scope | None = None):
        s = scope or self.globals
        existing = s.lookup_local(node.name)
        if existing and node.name not in self._forward_names:
            self.error(f"redefinition of type alias `{node.name}`", node)
            return
        if existing:
            self._forward_names.discard(node.name)
        s.define(node.name, Symbol("type_alias", node.target_type, node))

    def _eval_const_expr(self, node, scope: Scope | None = None):
        """Fold a constant expression to a Python int/float/str.

        Supports numeric literals (decimal/hex/float), unary -/+/~/not,
        arithmetic/bitwise/logical binary operators, references to previously
        defined enum members (bare or qualified) and const variables.
        """
        scope = scope or self.current_scope
        if isinstance(node, Number):
            if node.inferred_type == "float":
                return float(node.value)
            try:
                return int(node.value, 0)
            except (ValueError, TypeError):
                try:
                    return int(node.value)
                except (ValueError, TypeError):
                    return node.value
        if isinstance(node, UnaryOp):
            val = self._eval_const_expr(node.operand, scope)
            if val is None:
                return None
            try:
                if node.op.name == "MINUS":
                    return -val
                if node.op.name == "PLUS":
                    return +val
                if node.op.name == "TILDE":
                    return ~val
                if node.op.name == "NOT":
                    return not val
            except TypeError:
                return None
            return None
        if isinstance(node, BinOp):
            left = self._eval_const_expr(node.left, scope)
            right = self._eval_const_expr(node.right, scope)
            if left is None or right is None:
                return None
            try:
                match node.op.name:
                    case "PLUS":
                        return left + right
                    case "MINUS":
                        return left - right
                    case "STAR":
                        return left * right
                    case "SLASH" | "SLASH_SLASH":
                        return left // right
                    case "PERCENT":
                        return left % right
                    case "POW":
                        return left**right
                    case "SHL":
                        return left << right
                    case "SHR":
                        return left >> right
                    case "AMPERSAND":
                        return left & right
                    case "PIPE":
                        return left | right
                    case "CARET":
                        return left ^ right
                    case "AND":
                        return left and right
                    case "OR":
                        return left or right
            except (ZeroDivisionError, TypeError):
                return None
            return None
        if isinstance(node, Variable):
            if node.const_value is not None:
                return node.const_value
            sym = scope.lookup(node.name)
            if sym is None and self._enum_context_name:
                sym = scope.lookup(f"{self._enum_context_name}.{node.name}")
            if sym is not None and sym.kind == "enum_member":
                return sym.const_value
            if sym is not None and sym.kind == "const" and sym.node is not None:
                init = getattr(sym.node, "init", None)
                if init is not None:
                    return self._eval_const_expr(init, scope)
            if sym is not None and sym.const_value is not None:
                return sym.const_value
            return None
        if isinstance(node, Attr):
            if getattr(node, "_enum_member_value", None) is not None:
                return node._enum_member_value
            if isinstance(node.obj, Variable):
                member_sym = scope.lookup(f"{node.obj.name}.{node.name}")
                if member_sym is not None and member_sym.kind == "enum_member":
                    return member_sym.const_value
            return None
        if isinstance(node, String):
            return node.value
        return None

    def _visit_while(self, node, scope: Scope | None = None):
        if isinstance(node, While):
            self._infer_type(node.cond)
            old_locals = self.locals
            loop_scope = Scope(scope or self.current_scope)
            self.locals = loop_scope
            self._loop_depth += 1
            for stmt in node.body:
                self._visit(stmt, loop_scope)
            self._loop_depth -= 1
            self.locals = old_locals
        elif isinstance(node, dict):
            cond = node.get("cond")
            if cond is not None:
                self._infer_type(cond)
            old_locals = self.locals
            loop_scope = Scope(scope or self.current_scope)
            self.locals = loop_scope
            self._loop_depth += 1
            for stmt in node.get("body", []):
                self._visit(stmt, loop_scope)
            self._loop_depth -= 1
            self.locals = old_locals

    def _visit_for(self, node: dict, scope: Scope | None = None):
        s = scope or self.current_scope
        loop_scope = Scope(s)
        iterable = node.get("iter")
        iter_t = self._infer_type(iterable) if iterable is not None else None
        if iter_t is not None and _array_back(iter_t) is not None:
            # `T[]` and `T[N]` both iterate their element type; normalize the
            # stored iterable type to `T[]` so codegen dispatches uniformly.
            var_t = _array_back(iter_t)
            iter_t = var_t + "[]"
            if var_t == "void":
                self.error(
                    "cannot iterate a void array",
                    node,
                    note="array element type `void` has no values",
                )
                var_t = "char"
        elif iter_t == "dynamic":
            # Runtime-typed iterable: assume a dynamic list at run time.
            var_t = "dynamic"
        elif iter_t == "str":
            var_t = "char"
        elif iter_t is not None:
            self.error(
                f"cannot iterate value of type `{iter_t}`",
                iterable,
                note="for loops require a string or an array type (`T[]`)",
            )
            var_t = "char"
        else:
            var_t = "char"
        node["iter_type"] = iter_t or "str"
        node["var_type"] = var_t
        loop_sym = Symbol("variable", var_t, node)
        if var_t == "dynamic":
            loop_sym.dynamic = True
        loop_scope.define(node["var"], loop_sym)
        old_locals = self.locals
        self.locals = loop_scope
        self._loop_depth += 1
        for stmt in node.get("body", []):
            self._visit(stmt, loop_scope)
        self._loop_depth -= 1
        self.locals = old_locals

    def _resolve_type_alias(self, type_name):
        if type_name is None:
            return None
        sym = self.current_scope.lookup(type_name)
        if sym and sym.kind == "type_alias":
            return self._resolve_type_alias(sym.type)
        return type_name


def analyze(
    source: str,
    nodes: list,
    strict: bool = False,
    workspace_root: str | None = None,
    filepath: str | None = None,
    enable_extensions: bool = True,
    no_gc: bool = False,
):
    analyzer = SemanticAnalyzer(
        source,
        filepath=filepath,
        strict=strict,
        workspace_root=workspace_root,
        enable_extensions=enable_extensions,
        no_gc=no_gc,
    )
    # Pre-load CPM package manifests before analysis
    if enable_extensions:
        analyzer._load_cpm_package_manifests()
    if not analyzer.analyze(nodes):
        return analyzer.reporter.display(), None, analyzer._generic_instantiations
    # No errors, but there may be warnings to surface.
    warnings_txt = ""
    warn_texts = [
        d
        for d in analyzer.reporter.diagnostics
        if d.level in ("warning", "strict-warning")
    ]
    if warn_texts:
        warnings_txt = analyzer.reporter.display()
    return None, warnings_txt or None, analyzer._generic_instantiations
