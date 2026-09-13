__version__ = "4.2.1"

# ---------------------------------------------------------------------------
# Public library API.
#
# Consumers should import ONLY the ``cpyte`` package and use these entry
# points (``import cpyte; cpyte.parse_and_analyze(...)``) instead of reaching
# into ``cpyte.astparse`` / ``cpyte.lexar`` / ``cpyte.semantic_analasis`` /
# ``cpyte.bytecoding`` / ``cpyte.compiling`` internals.  Everything here is an
# additive re-export or thin wrapper: compiler behavior is unchanged.
# ---------------------------------------------------------------------------

from . import astparse, bytecoding, compiling, lexar, mainpie, semantic_analasis, ui


class CpyteCompileError(Exception):
    """Raised by the high-level library entry points on parse / analyze /
    codegen failure (library calls never ``sys.exit``)."""


def parse_and_analyze(
    source,
    *,
    tab_size=4,
    strict=False,
    enable_extensions=True,
    no_gc=False,
    filepath=None,
    workspace_root=None,
):
    """Lex + parse + semantically analyze ``source``.

    Returns ``(parsed, generic_instantiations, warnings_txt)`` or raises
    :class:`CpyteCompileError`.  Loads workspace package manifests when
    extensions are enabled (same behavior as ``cpy build``).
    """
    if workspace_root is None:
        workspace_root = mainpie._find_workspace_root(filepath)
    if enable_extensions:
        mainpie._load_package_manifests_from_source(workspace_root)
    lex = lexar.Lexer(source, tab_size=tab_size, enable_extensions=enable_extensions)
    tokens = lex.get_tokens()
    try:
        parsed, _ = astparse.parse_file(tokens, enable_extensions=enable_extensions)
    except (lexar.LexerError, astparse.ParseError) as e:
        raise CpyteCompileError(f"parse error: {e}") from e
    result, warnings_txt, generic_instantiations = semantic_analasis.analyze(
        source,
        parsed,
        strict=strict,
        workspace_root=workspace_root,
        filepath=filepath,
        enable_extensions=enable_extensions,
        no_gc=no_gc,
    )
    if result:
        raise CpyteCompileError(str(result).rstrip())
    return parsed, generic_instantiations, warnings_txt


def emit_program(
    parsed,
    *,
    generic_instantiations=None,
    no_userspace=False,
    enable_extensions=True,
    no_gc=False,
    use_native_eh=False,
    debug_instrument=False,
    debug_nids=None,
    debug_step_ids=None,
):
    """Emit LLVM IR for a parsed module.

    Returns ``(module, src_files, emitter)``; the emitter carries the
    ``_dbg_*`` debug metadata (tracer symbols, box slots/types) when
    ``debug_instrument`` is enabled.  Raises :class:`CpyteCompileError` on
    codegen failure.
    """
    c = bytecoding.LLVM(
        no_userspace=no_userspace,
        enable_extensions=enable_extensions,
        no_gc=no_gc,
        use_native_eh=use_native_eh,
        debug_instrument=debug_instrument,
        debug_nids=debug_nids,
        debug_step_ids=debug_step_ids,
    )
    c.generic_instantiations = generic_instantiations or {}
    try:
        prog, src_files = c.emit_program(parsed)
    except Exception as e:
        raise CpyteCompileError(f"codegen error: {type(e).__name__}: {e}") from e
    return prog, src_files, c


# -- low-level re-exports (the whole compiler usable from one package) ------
from .astparse import (  # noqa: E402, F401
    Assert,
    Assign,
    Break,
    Call,
    ClassDef,
    Continue,
    DeferStmt,
    EnumDef,
    ExprStmt,
    FuncDef,
    If,
    Import,
    ParseError,
    Print,
    Raise,
    Return,
    StructDef,
    Switch,
    Try,
    TypeAlias,
    While,
)
from .lexar import Lexer, LexerError  # noqa: E402, F401
from .semantic_analasis import analyze  # noqa: E402, F401
from .bytecoding import LLVM  # noqa: E402, F401
from .compiling import (  # noqa: E402, F401
    Linker,
    LinkerNotFoundError,
    host_target,
    run_aot,
    run_jit,
)
from .compiling import _host_default_pic as host_default_pic  # noqa: E402
from .mainpie import (  # noqa: E402, F401
    _collect_frameworks as collect_frameworks,
    _detect_nogc as detect_nogc,
    _find_workspace_root as find_workspace_root,
)