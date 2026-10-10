import os
import subprocess
import sys

if __package__:
    from . import __version__, ui
    from .astparse import FuncDef, Import, ParseError, parse_file
    from .bytecoding import LLVM
    from .compiling import (
        _BIGNUM_C,
        _GC_RUNTIME_C,
        _RUNTIME_C,
        _find_llvm_cc,
        _gmp_library_paths,
        _host_default_pic,
        _remove_probe_stack_ir,
        _runtime_extra_flags,
        host_target,
        make_target_machine,
        optimize,
        run_aot,
        run_jit,
        run_scorpion,
        set_target_cpu,
    )
    from .extension_hooks import (
        CompilerContext,
        HookLoader,
        HookStage,
        get_global_hook_registry,
    )
    from .lexar import Lexer, LexerError, register_keywords
    from .linker import Linker, format_cc_diag
    from .package_manifest import (
        ManifestParser,
        get_global_registry,
        iter_cpm_version_dirs,
    )
    from . import scorpion_abi
    from .sef import cmd_check, cmd_digest, cmd_dump, cmd_pack, cmd_size
    from .semantic_analasis import analyze
    from .update_check import report_update, start_check
else:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from cpyte import __version__, ui
    from cpyte.astparse import FuncDef, Import, ParseError, parse_file
    from cpyte.bytecoding import LLVM
    from cpyte.compiling import (
        _BIGNUM_C,
        _GC_RUNTIME_C,
        _RUNTIME_C,
        _find_llvm_cc,
        _gmp_library_paths,
        _host_default_pic,
        _remove_probe_stack_ir,
        _runtime_extra_flags,
        host_target,
        make_target_machine,
        optimize,
        run_aot,
        run_jit,
        run_scorpion,
        set_target_cpu,
    )
    from cpyte.extension_hooks import (
        CompilerContext,
        HookLoader,
        HookStage,
        get_global_hook_registry,
    )
    from cpyte.lexar import Lexer, LexerError, register_keywords
    from cpyte.linker import Linker, format_cc_diag
    from cpyte.package_manifest import (
        ManifestParser,
        get_global_registry,
        iter_cpm_version_dirs,
    )
    from cpyte import scorpion_abi
    from cpyte.sef import cmd_check, cmd_digest, cmd_dump, cmd_pack, cmd_size
    from cpyte.semantic_analasis import analyze
    from cpyte.update_check import report_update, start_check


def _host_target_triple():
    """Target triple used when compiling C sources with clang."""
    return host_target().triple


_USAGE = """Usage: cpy [options] <source.cpy>
       cpy build [--output O] [--debug] [--opt N] [--osize] [--no-userspace] [--pic] [--lto] <source.cpy>
       cpy format [--write] [--check] [--tab-size N] <source.cpy>
       cpy sef <subcommand> ...

Global options:
  --tab-size N        Set tab size (default 4)
  --strict            Enable strict semantic analysis
  --no-userspace      Compile without the userspace runtime
  --nogc              Disable automatic GC and add the free() builtin
  --pic               Position-independent code
  --cpu CPU           Override the target CPU (e.g. skylake, apple-m1, native)
  --mattr FEATURES    Override LLVM target-features (e.g. +avx2,+fma)
  --export NAME       Export NAME as a library symbol, or
                      SYMBOL=WIRE_NAME to publish a symbol under a
                      different wire name (dynamic SEF; repeatable)
  --no-auto-export    Disable automatic export of `public` functions (scorpion)
  --require NAME=VER  Pin an import to a version (dynamic SEF; repeatable)
  --scope S           Prefix unscope'd exports with `S::` (dynamic SEF)
  --weak NAME         Mark an import weak: unresolved is not fatal
                      (dynamic SEF; repeatable)
  --lazy / --no-lazy  One PLT stub per call site, patched on first call
                      (dynamic SEF)
  --versym / --no-versym
                      Wide v2.1 import/export records (the default), or
                      v2.0 records for an older loader (dynamic SEF)
  --lto               Enable link-time optimization (requires clang)
  --ast               Print the parsed AST
  --emit-llvm         Print the generated LLVM IR
  --jit               JIT-compile and run (default)
  --aot               Compile to a native executable
  --scorpion          Cross-compile to RISC-V 32-bit (SEF)
  --version           Print the cpyte version
  -h, --help          Show this help

Commands:
  build               Compile source.cpy to a native executable
  format              Canonically reformat source.cpy (AST-based)
  sef                 Scorpion SEF binary tools (pack/dump/check/size/digest)
"""


def pretty_ast(node, indent=0):
    pad = "  " * indent
    if isinstance(node, list):
        if not node:
            return f"{pad}(empty)"
        lines = []
        for item in node:
            lines.append(pretty_ast(item, indent))
        return "\n".join(lines)

    name = type(node).__name__

    if name == "Number":
        return f"{pad}{node.value}"

    if name == "String":
        return f"{pad}'{node.value}'"

    if name == "FString":
        return f"{pad}f-string: {node.parts}"

    if name == "Variable":
        return f"{pad}{node.name}"

    if name == "VarDecl":
        init = f" = {pretty_ast(node.init, indent)}" if node.init else ""
        return f"{pad}{node.var_type} {node.name}{init}"

    if name == "ExprStmt":
        return f"{pad}statement:\n{pretty_ast(node.expr, indent + 1)}"

    if name == "Assign":
        if isinstance(node.target, str):
            return f"{pad}{node.target} =\n{pretty_ast(node.value, indent + 1)}"
        return (
            f"{pad}{pretty_ast(node.target, 0)} =\n{pretty_ast(node.value, indent + 1)}"
        )

    if name == "Return":
        if node.value is None:
            return f"{pad}return"
        return f"{pad}return\n{pretty_ast(node.value, indent + 1)}"

    if name == "Print":
        return f"{pad}print\n{pretty_ast(node.value, indent + 1)}"

    if name == "Input":
        return f"{pad}input()"

    if name == "InputStr":
        return f"{pad}input_str()"

    if name == "InputBig":
        return f"{pad}input_big()"

    if name == "Break":
        return f"{pad}break"

    if name == "Continue":
        return f"{pad}continue"

    if name == "Assert":
        result = f"{pad}assert {pretty_ast(node.cond, indent)}"
        if node.message:
            result += f", {pretty_ast(node.message, indent)}"
        return result

    if name == "If":
        result = f"{pad}if\n{pretty_ast(node.cond, indent + 1)}"
        result += f"\n{pad}then:\n{pretty_ast(node.body, indent + 1)}"
        if node.orelse:
            result += f"\n{pad}else:\n{pretty_ast(node.orelse, indent + 1)}"
        return result

    if name == "While":
        result = f"{pad}while\n{pretty_ast(node.cond, indent + 1)}"
        result += f"\n{pad}body:\n{pretty_ast(node.body, indent + 1)}"
        return result

    if name == "Import":
        extra = f" [{node.src_file}]" if node.src_file else ""
        return f"{pad}import {node.module}{extra}"

    if name == "NewExpr":
        size = f"[{pretty_ast(node.size, 0)}]" if node.size else ""
        text = f"{pad}new {node.type_expr}{size}"
        if getattr(node, "args", None) is not None:
            text += "(" + ", ".join(pretty_ast(a, 0) for a in node.args) + ")"
        return text

    if name == "Deref":
        return f"{pad}*\n{pretty_ast(node.operand, indent + 1)}"

    if name == "AddrOf":
        return f"{pad}&\n{pretty_ast(node.operand, indent + 1)}"

    if name == "SizeOf":
        return f"{pad}sizeof({node.type_expr})"

    if name == "CastExpr":
        return f"{pad}({node.type_expr}){pretty_ast(node.expr, 0)}"

    if name == "StructDef":
        gp = f"<{', '.join(node.generic_params)}>" if node.generic_params else ""
        result = f"{pad}struct {node.name}{gp}:"
        for f in node.fields:
            result += f"\n{pad}  {f.type_expr} {f.name}"
        return result

    if name == "Field":
        return f"{pad}{node.type_expr} {node.name}"

    if name == "Switch":
        result = f"{pad}switch\n{pretty_ast(node.value, indent + 1)}"
        for val, body in node.cases:
            label = "default" if val is None else f"case {pretty_ast(val, 0)}"
            result += f"\n{pad}  {label}:\n{pretty_ast(body, indent + 2)}"
        return result

    if name == "BinOp":
        return f"{pad}{node.op.name}\n{pretty_ast(node.left, indent + 1)}\n{pretty_ast(node.right, indent + 1)}"

    if name == "UnaryOp":
        return f"{pad}{node.op.name}\n{pretty_ast(node.operand, indent + 1)}"

    if name == "Call":
        result = f"{pad}call\n{pretty_ast(node.callee, indent + 1)}"
        if node.args:
            result += f"\n{pad}args:"
            for arg in node.args:
                result += f"\n{pretty_ast(arg, indent + 1)}"
        return result

    if name == "Index":
        return f"{pad}index\n{pretty_ast(node.obj, indent + 1)}\n{pretty_ast(node.index, indent + 1)}"

    if name == "Attr":
        return f"{pad}.{node.name}\n{pretty_ast(node.obj, indent + 1)}"

    if name == "FuncDef":
        decorators = getattr(node, "decorators", None) or []
        lines = []
        for dec in decorators:
            lines.append(f"{pad}@{pretty_ast(dec, 0)}")
        vis = f"{node.visibility} " if node.visibility else ""
        ret = f" -> {node.rettype}" if node.rettype else ""
        const_params = set(getattr(node, "const_params", None) or ())
        params = ", ".join(
            f"({k}): {v}" if k in const_params else f"{k}: {v}"
            for k, v in node.params.items()
        )
        lines.append(f"{pad}{vis}def {node.name}({params}){ret}:")
        result = "\n".join(lines)
        for stmt in node.body:
            result += f"\n{pretty_ast(stmt, indent + 1)}"
        return result

    if isinstance(node, dict):
        t = node.get("type", "?")
        if t == "class":
            result = f"{pad}class {node['name']}:"
            for stmt in node.get("body", []):
                result += f"\n{pretty_ast(stmt, indent + 1)}"
            return result
        if t == "for":
            result = (
                f"{pad}for {node['var']} in\n{pretty_ast(node['iter'], indent + 1)}"
            )
            result += f"\n{pad}body:"
            for stmt in node.get("body", []):
                result += f"\n{pretty_ast(stmt, indent + 1)}"
            return result
        return f"{pad}{node}"

    return f"{pad}{node}"


def _load_package_manifests_from_source(workspace_root: str) -> None:
    """
    Load all package manifests from CPM packages in the workspace.

    This ensures that package extensions (keywords, operators, etc.) are
    available during lexing and parsing.
    """
    cpm_root = os.path.join(workspace_root, ".cpm", "modules")
    if not os.path.isdir(cpm_root):
        return

    manifest_registry = get_global_registry()

    # Load all available packages in the workspace
    for package_name, version_dir in iter_cpm_version_dirs(cpm_root):
        # Check if already loaded
        if manifest_registry.is_loaded(package_name):
            continue

        manifest_path = os.path.join(version_dir, "package.json")
        if not os.path.exists(manifest_path):
            continue

        try:
            manifest = ManifestParser.validate_and_parse(manifest_path)

            # Register keywords with lexer
            if manifest.capabilities.keywords:
                register_keywords(manifest.capabilities.keywords)

            # Register manifest
            manifest_registry.register(manifest)

            # Parser hooks must be loaded before parsing the source file.
            if manifest.extensions.parser_hooks:
                HookLoader.load_hooks_from_package(
                    package_name,
                    version_dir,
                    manifest.extensions.parser_hooks,
                    get_global_hook_registry(),
                    CompilerContext(data={"package_dir": version_dir}),
                )

            # Note: hooks are NOT loaded here. Per the "hooks only when
            # imported" rule, a package's extension hooks are introduced by the
            # semantic analyzer when the package is actually imported. This
            # function only registers the manifest (for import resolution) and
            # any lexer keywords so imports of that package resolve correctly.

        except Exception as e:
            ui.print_warn(f"Failed to load package manifest for '{package_name}': {e}")


_DEEP_INTEGRATION_STAGES = {
    HookStage.LEXER,
    HookStage.PARSER,
    HookStage.SEMANTIC,
    HookStage.TRANSFORM,
    HookStage.CODEGEN,
    HookStage.OPTIMIZE,
    HookStage.LINK,
    HookStage.RUNTIME,
}


def _notify_deep_hook_usage() -> None:
    """Emit a small notice when packages integrate into compiler internals."""
    registry = get_global_hook_registry()
    deep = [hook for hook in registry.all() if hook.stage in _DEEP_INTEGRATION_STAGES]
    if not deep:
        return
    names = sorted({hook.package_name for hook in deep})
    ui.print_status("deep extension integration active: " + ", ".join(names))


def _find_workspace_root(filepath: str | None) -> str:
    if not filepath:
        return os.getcwd()
    path = os.path.abspath(filepath)
    dirpath = os.path.dirname(path)
    while True:
        if os.path.isdir(os.path.join(dirpath, ".cpm")):
            return dirpath
        parent = os.path.dirname(dirpath)
        if parent == dirpath:
            return os.getcwd()
        dirpath = parent


def _compile(
    source,
    tab_size=4,
    strict=False,
    enable_extensions=True,
    no_gc=False,
    filepath=None,
    scorpion=False,
):
    workspace_root = _find_workspace_root(filepath)

    # Pre-load package manifests if extensions are enabled
    if enable_extensions:
        _load_package_manifests_from_source(workspace_root)

    lex = Lexer(source, tab_size=tab_size, enable_extensions=enable_extensions)
    tokens = lex.get_tokens()
    try:
        parsed, _ = parse_file(tokens, enable_extensions=enable_extensions)
    except (LexerError, ParseError) as e:
        ui.print_err(f"parse error: {e}")
        sys.exit(1)
    result, warnings_txt, generic_instantiations = analyze(
        source,
        parsed,
        strict=strict,
        workspace_root=workspace_root,
        filepath=filepath,
        enable_extensions=enable_extensions,
        no_gc=no_gc,
        scorpion=scorpion,
    )
    if result:
        sys.stderr.write(result + ("\n" if result else ""))
        sys.exit(1)
    if warnings_txt:
        sys.stderr.write(warnings_txt.strip() + "\n")
    if enable_extensions:
        _notify_deep_hook_usage()
    return parsed, generic_instantiations


def _detect_nogc(source: str) -> tuple[str, bool]:
    """Look for a `#nogc` directive on the first non-blank line.

    The directive disables automatic garbage collection (see --nogc). It is
    stripped from the source before lexing so it never reaches the parser.
    """
    lines = source.split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and lines[idx].strip() == "#nogc":
        lines[idx] = ""
        return "\n".join(lines), True
    return source, False


def _detect_scorpion(source: str) -> tuple[str, bool]:
    """Look for a `#scorpion` directive on the first non-blank line.

    The directive builds the Scorpion ABI (syscall numbers, `SCORPION_*`
    codes/flags, `ScorpionLibInfo`, every `scorpion_*` wrapper) into the
    language. It is stripped from the source before lexing so it never reaches
    the parser; the table itself lives in `scorpion_abi`.
    """
    return scorpion_abi.detect(source)


def _emit(
    parsed,
    generic_instantiations=None,
    no_userspace=False,
    enable_extensions=True,
    no_gc=False,
    target_triple=None,
    use_native_eh=False,
    scorpion=False,
):
    c = LLVM(
        no_userspace=no_userspace,
        enable_extensions=enable_extensions,
        no_gc=no_gc,
        target_triple=target_triple,
        use_native_eh=use_native_eh,
        scorpion=scorpion,
    )
    c.generic_instantiations = generic_instantiations or {}
    try:
        prog, src_files = c.emit_program(parsed)
    except SystemExit:
        raise
    except Exception as e:
        ui.print_err(f"codegen error: {type(e).__name__}: {e}")
        if isinstance(e, RuntimeError):
            sys.exit(1)
        if getattr(e, "user_facing", False):
            sys.exit(1)
        import traceback

        traceback.print_exc()
        sys.exit(1)
    return prog, src_files


def _collect_frameworks(nodes):
    frameworks = []
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if isinstance(node, Import):
            frameworks.extend(node.frameworks)
        if isinstance(node, list):
            stack.extend(node)
        else:
            for attr in ("body", "orelse", "items", "handlers", "args"):
                val = getattr(node, attr, None)
                if isinstance(val, list):
                    stack.extend(val)
    return list(set(frameworks))


def _collect_public_exports(parsed):
    """Collect the automatic export list for a Scorpion dynamic build.

    Every top-level `public def` in the module itself, plus the re-exported
    public functions of modules it imports (recursed through Import.sub_ast,
    mirroring emit_program's node walk), is a candidate library symbol. Names
    unique; order preserved; no main.
    """

    def walk(nodes):
        for node in nodes:
            if isinstance(node, Import):
                sub = getattr(node, "sub_ast", None)
                if sub:
                    yield from walk(sub)
            elif isinstance(node, FuncDef):
                if (
                    node.name != "main"
                    and getattr(node, "visibility", None) == "public"
                ):
                    yield node.name

    seen = set()
    out = []
    for name in walk(parsed):
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


_SCORPION_C_TYPES = {
    "int": "int32_t",
    "int64": "int64_t",
    "uint64": "uint64_t",
    "size_t": "uintptr_t",
    "bool": "bool",
    "char": "char",
    "float": "float",
    "double": "double",
    "str": "const char *",
    "void": "void",
    "big": "void *",
    "ubig": "void *",
    "dynamic": "void *",
}


def _scorpion_c_type(t, seen=None):
    """Best-effort cpy type name -> C declaration (RV32 ABI).

    Class/unknown terms lower to opaque struct tags (forward-declared by the
    header writer via `seen`), so `Vec*` becomes `struct cpyte_Vec *` and a
    class VALUE `Vec` becomes `struct cpyte_Vec` (by-value struct, matching
    the class-VALUE param convention).
    """
    t = (t or "int").strip()
    if t.endswith("[]"):
        return _scorpion_c_type(t[:-2], seen) + " *"
    if t.endswith("*"):
        return _scorpion_c_type(t[:-1], seen) + " *"
    if t.endswith("&"):
        return _scorpion_c_type(t[:-1], seen) + " *"
    if t in _SCORPION_C_TYPES:
        return _SCORPION_C_TYPES[t]
    if "<" in t:
        base = t.split("<", 1)[0].strip()
        if base in _SCORPION_C_TYPES:
            return _SCORPION_C_TYPES[base]
        t = base
    tag = "cpyte_%s" % t
    if seen is not None:
        seen.add(tag)
    return "struct %s" % tag


def _scorpion_wire_name(entry):
    """The name a consumer sees for one `--export` argument.

    elf2sef accepts `NAME` or `SYMBOL=WIRE_NAME`; only the wire name goes into
    the loader's export table, so only that name belongs in the generated
    header/stub. Any scope prefix applied by `--scope` is loader-side too and
    is deliberately not reproduced here: the stub names the symbols this
    image publishes, and the scope is chosen by whoever builds the library.
    """
    return entry.split("=", 1)[1] if "=" in entry else entry


def _scorpion_prototypes(export_names, parsed):
    """Synthesize C prototypes for the exported Scorpion library symbols.

    For each exported name a prototype is built from the defining FuncDef
    found by walking the module (and its imports). Names that do not resolve
    to a FuncDef (e.g. hand-written `public` C helpers) are skipped with a
    comment. Returns ``(protos, seen_tags)`` where ``protos`` is a list of C
    lines (or comment lines for unresolvable names) and ``seen_tags`` collects
    the opaque struct tags that need forward declarations.
    """
    funcs = {}

    def walk(nodes):
        for node in nodes:
            if isinstance(node, Import):
                sub = getattr(node, "sub_ast", None)
                if sub:
                    walk(sub)
            elif isinstance(node, FuncDef):
                funcs.setdefault(node.name, node)

    walk(parsed)

    seen_tags = set()
    protos = []
    seen_wire = set()
    for entry in export_names:
        wire = _scorpion_wire_name(entry)
        # `NAME` and `SYMBOL=WIRE_NAME` can both publish the same wire name.
        if wire in seen_wire:
            continue
        seen_wire.add(wire)
        if not wire.isidentifier():
            # A scoped (`ns::f`) or versioned (`f@2.0`) wire name is a real
            # loader symbol but is not spellable as a cpyte identifier, so no
            # ccode prototype can name it and a generated one would be a lie.
            # Say so instead of emitting something that cannot link.
            protos.append(
                '/* exported as "%s": scoped/versioned wire name, not'
                " importable from cpyte (declare it by hand if you must) */" % wire
            )
            continue
        fd = funcs.get(wire)
        if fd is None:
            protos.append(
                '/* symbol "%s" exported (no cpyte signature available) */' % wire
            )
            continue
        ret = _scorpion_c_type(getattr(fd, "rettype", None), seen_tags)
        params = []
        for pname, ptype in (fd.params or {}).items():
            if pname == "this":
                continue
            params.append("%s %s" % (_scorpion_c_type(ptype, seen_tags), pname))
        if params:
            proto = "%s %s(%s);" % (ret, wire, ", ".join(params))
        else:
            proto = "%s %s(void);" % (ret, wire)
        protos.append(proto)
    return protos, seen_tags


def _write_scorpion_header(path, protos, seen_tags, export_names):
    """Generate a C ABI header for the exported Scorpion library symbols.

    The header can be `#include`d by C code that links against the .sef; the
    names are resolved at load time by the Scorpion loader from the SEF's
    SEG_EXPORT table (see also `scorpion_sym()` for the runtime-dlopen form).
    """
    out = []
    out.append("/* Generated by cpyte for the Scorpion (RISC-V 32) SEF library:")
    out.append(" *   %s" % path)
    out.append(" *")
    out.append(
        " * Exports %d symbol(s). Link this library with the Scorpion"
        % len(export_names)
    )
    out.append(" * loader; it binds these names from the SEF's export table at")
    out.append(" * load time, or look them up at runtime with scorpion_sym().")
    out.append(" */")
    out.append("#ifndef CPYTE_SCORPION_ABI")
    out.append("#define CPYTE_SCORPION_ABI")
    out.append("")
    out.append("#include <stdint.h>")
    out.append("#include <stdbool.h>")
    out.append("")
    if seen_tags:
        out.append("/* opaque cpyte class/struct types referenced by the ABI */")
        for tag in sorted(seen_tags):
            out.append("typedef struct %s %s;" % (tag, tag))
        out.append("")
    out.extend(protos)
    out.append("")
    out.append("#endif")

    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def _write_scorpion_stub(path, protos, seen_tags, export_names):
    """Generate a cpyte-consumable stub for the exported library symbols.

    A SEF export table carries names and addresses but no type information, so
    the consumer cannot type-check a call against a bare `import "lib.sef"`.
    This stub closes that gap: it is imported like any other cpy module and
    declares each exported function as a prototype, which cpyte lowers to an
    LLVM `declare` and `elf2sef` turns into a SEG_IMPORT record.

    The declarations live in a `ccode:` block because that is the one cpyte
    construct that describes an external function with a C signature; the text
    is byte-identical to the generated `.h`, so both consumers agree.
    """
    out = []
    out.append("# Generated by cpyte for the Scorpion (RISC-V 32) SEF library:")
    out.append("#   %s" % path)
    out.append("#")
    out.append(
        "# Imports %d symbol(s) from the library built alongside it."
        % len(export_names)
    )
    out.append("# Build the consumer with `--scorpion --pic` so these become")
    out.append("# SEG_IMPORT records bound by the loader at load time.")
    out.append("")
    out.append("ccode:")
    out.append("")
    # The prototypes use the fixed-width stdint spellings (int32_t, uint32_t,
    # ...). Those come from a header, and a ccode block is compiled verbatim, so
    # it has to pull them in itself: the cross clang provides stdint implicitly
    # but a host JIT/AOT build of the same stub does not.
    out.append("    #include <stdint.h>")
    out.append("    #include <stdbool.h>")
    out.append("")
    if seen_tags:
        for tag in sorted(seen_tags):
            out.append("    typedef struct %s %s;" % (tag, tag))
        out.append("")
    for line in protos:
        # Every line of a ccode: block must be indented; a column-0 line ends
        # the block and the next `;` then fails to lex.
        out.append("    %s" % line)
    out.append("")

    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def cmd_build(
    args,
    tab_size=4,
    strict=False,
    no_userspace=False,
    pic=None,
    lto=False,
    no_gc=False,
):
    if not args:
        ui.print_usage(
            "Usage: cpy build [--output O] [--debug] [--opt N] [--osize] [--no-userspace] [--nogc] [--pic] [--lto] <source.cpy>"
        )
        sys.exit(1)

    output = None
    debug = False
    opt = 3
    opt_size = False
    src_file = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-o" or a == "--output":
            if i + 1 < len(args):
                output = args[i + 1]
                i += 2
            else:
                ui.print_err(f"{a} requires an argument")
                sys.exit(1)
        elif a == "-g" or a == "--debug":
            debug = True
            i += 1
        elif a == "--no-userspace":
            no_userspace = True
            i += 1
        elif a == "--nogc":
            no_gc = True
            i += 1
        elif a == "--pic":
            pic = True
            i += 1
        elif a == "--lto":
            lto = True
            i += 1
        elif a == "--osize" or a == "-OSize":
            opt_size = True
            i += 1
        elif a == "--opt" and i + 1 < len(args):
            opt = int(args[i + 1])
            i += 2
        elif a in ("--cpu", "--march") and i + 1 < len(args):
            set_target_cpu(cpu=args[i + 1])
            i += 2
        elif a == "--mattr" and i + 1 < len(args):
            set_target_cpu(features=args[i + 1])
            i += 2
        elif not a.startswith("-"):
            src_file = a
            i += 1
        else:
            ui.print_warn(f"Unknown flag: {a}")
            sys.exit(1)

    if not src_file:
        ui.print_usage(
            "Usage: cpy build [--output O] [--debug] [--opt N] [--osize] [--pic]"
            " [--cpu CPU] [--mattr FEATURES] [--lto] <source.cpy>"
        )
        sys.exit(1)

    if pic is None:
        pic = _host_default_pic()

    if opt_size:
        # -OSize ignores speed entirely; cap at O2 so the O3/O4 pipelines never run.
        if opt > 2:
            ui.print_warn("-OSize ignores speed; capping --opt to 2")
            opt = 2

    with open(src_file) as f:
        source = f.read()

    source, no_gc = _detect_nogc(source) if not no_gc else (source, True)
    source, scorpion = _detect_scorpion(source)

    parsed, generic_instantiations = _compile(
        source,
        tab_size=tab_size,
        strict=strict,
        enable_extensions=not no_userspace,
        no_gc=no_gc,
        filepath=os.path.abspath(src_file),
        scorpion=scorpion,
    )

    frameworks = _collect_frameworks(parsed)

    ui.print_status(f"Compiling {src_file} ...")

    prog, src_files = _emit(
        parsed,
        generic_instantiations=generic_instantiations,
        no_userspace=no_userspace,
        enable_extensions=not no_userspace,
        no_gc=no_gc,
        use_native_eh=True,
        scorpion=scorpion,
    )

    out_base = src_file.rsplit(".", 1)[0] if "." in src_file else "a"
    obj_file = out_base + ".o"

    from llvmlite import binding

    binding.initialize_native_target()
    binding.initialize_native_asmprinter()

    mod = binding.parse_assembly(str(prog))
    # The bignum runtime is compiled from source for the host platform (instead
    # of pre-built bitcode) so its libc symbol names match the target OS. Where
    # a clang-compatible compiler with `-emit-llvm` is available we link its IR
    # directly into the module; otherwise (AOT on systems with only gcc) it is
    # compiled to a native object and linked by the system linker below.
    bignum_mod = None
    try:
        llvm_cc = _find_llvm_cc()
        r = subprocess.run(
            [
                llvm_cc,
                "-S",
                "-emit-llvm",
                "-O0",
                "-target",
                _host_target_triple(),
                "-fno-stack-protector",
                *(_runtime_extra_flags(_BIGNUM_C)),
                "-o",
                "-",
                _BIGNUM_C,
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            ui.print_err(f"error compiling {_BIGNUM_C}: {format_cc_diag(r.stderr)}")
            sys.exit(1)
        bignum_mod = binding.parse_assembly(_remove_probe_stack_ir(r.stdout))
    except SystemExit:
        # No clang/-emit-llvm compiler; fall back to native-object linking below.
        bignum_mod = None
    if bignum_mod is not None:
        binding.link_modules(mod, bignum_mod)
    mod.verify()

    # Always run module optimization so the LLVM passes (SROA, inlining and the
    # loop/SLP auto-vectorizer) run at the chosen -O level — this is what emits
    # SIMD/NEON/AVX. `optimize` opts out internally for -O0 and honors opt_size.
    optimize(mod, opt, opt_size=opt_size)
    mod.verify()

    target_machine = make_target_machine(pic=pic, codemodel="small")
    obj = target_machine.emit_object(mod)
    with open(obj_file, "wb") as f:
        f.write(obj)
    linker = Linker(lto=lto)
    objs = [obj_file]

    if bignum_mod is None:
        # No clang/-emit-llvm compiler (AOT with plain gcc): the bignum runtime
        # is compiled to a native object and linked by the system linker.
        bignum_obj = out_base + ".bignum.o"
        linker.compile_c(
            _BIGNUM_C,
            output=bignum_obj,
            opt_level=opt,
            opt_size=opt_size,
            debug=debug,
            pic=pic,
            extra_flags=_runtime_extra_flags(_BIGNUM_C),
        )
        objs.append(bignum_obj)

    for src in src_files or []:
        src_obj = src.rsplit(".", 1)[0] + ".o"
        linker.compile_c(
            src, output=src_obj, opt_level=opt, opt_size=opt_size, debug=debug, pic=pic
        )
        objs.append(src_obj)

    if not no_userspace:
        runtime_obj = out_base + ".runtime.o"
        linker.compile_c(
            _RUNTIME_C,
            output=runtime_obj,
            opt_level=opt,
            opt_size=opt_size,
            debug=debug,
            pic=pic,
            eh=True,
        )
        objs.append(runtime_obj)

    if not no_gc:
        gc_obj = out_base + ".gc.o"
        linker.compile_c(
            _GC_RUNTIME_C,
            output=gc_obj,
            opt_level=opt,
            opt_size=opt_size,
            debug=debug,
            pic=pic,
        )
        objs.append(gc_obj)

    executable = output or out_base
    linker.link(
        objs,
        executable,
        libraries=["m", "gmp"],
        library_paths=_gmp_library_paths(),
        opt_level=opt,
        opt_size=opt_size,
        debug=debug,
        frameworks=frameworks,
        pic=pic,
    )
    ui.print_ok(f"Wrote {executable}")


def cmd_format(args, tab_size=4):
    if not args or args[0] in ("-h", "--help"):
        ui.print_usage(
            "Usage: cpy format [--write] [--check] [--tab-size N] [--no-extensions] <source.cpy>"
        )
        sys.exit(0 if args else 1)

    write = False
    check = False
    enable_extensions = True
    path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-w", "--write"):
            write = True
            i += 1
        elif a in ("-c", "--check"):
            check = True
            i += 1
        elif a == "--tab-size" and i + 1 < len(args):
            tab_size = int(args[i + 1])
            i += 2
        elif a == "--no-extensions":
            enable_extensions = False
            i += 1
        elif not a.startswith("-"):
            path = a
            i += 1
        else:
            ui.print_warn(f"Unknown flag: {a}")
            sys.exit(1)

    if not path:
        ui.print_usage(
            "Usage: cpy format [--write] [--check] [--tab-size N] [--no-extensions] <source.cpy>"
        )
        sys.exit(1)

    from cpyte.formatter import format_file

    result, code = format_file(
        path,
        write=write,
        check=check,
        tab_size=tab_size,
        enable_extensions=enable_extensions,
    )

    if code != 0:
        for err in result.errors:
            ui.print_err(f"error: {err}")
        if check and not result.errors:
            ui.print_err(f"{path}: file is not formatted")
        elif not result.errors:
            ui.print_err(f"{path}: formatting failed")
        sys.exit(1)

    if write:
        ui.print_ok(f"Formatted {path}")
    elif check:
        ui.print_ok(f"{path}: ok")
    else:
        sys.stdout.write(result.formatted)


_SEF_USAGE = """Usage: cpy sef <subcommand> ...

Subcommands:
  pack    assemble a SEF binary: cpy sef pack <output.sef>
            [--entry N] [--flags N] [--text FILE ...] [--data FILE ...]
            [--bss SIZE ...] [--spec JSON]
  dump    decode and pretty-print: cpy sef dump <input.sef>
  check   validate a SEF binary:  cpy sef check <input.sef>
  size    report footprint/layout: cpy sef size <input.sef>
  digest  show dynamic-link exports/imports/relocs:
            cpy sef digest <input.sef> [--json]
"""


def cmd_sef(args):
    if not args or args[0] in ("-h", "--help"):
        print(ui.paint_usage(_SEF_USAGE, stream=sys.stderr), file=sys.stderr)
        sys.exit(0 if args else 1)

    cmd = args[0]
    rest = args[1:]

    if cmd == "pack":
        output = None
        entry = 0
        flags = 0
        text = []
        data = []
        bss = []
        spec = None
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--entry" and i + 1 < len(rest):
                entry = int(rest[i + 1], 0)
                i += 2
            elif a == "--flags" and i + 1 < len(rest):
                flags = int(rest[i + 1], 0)
                i += 2
            elif a == "--text" and i + 1 < len(rest):
                text.append(rest[i + 1])
                i += 2
            elif a == "--data" and i + 1 < len(rest):
                data.append(rest[i + 1])
                i += 2
            elif a == "--bss" and i + 1 < len(rest):
                bss.append(int(rest[i + 1], 0))
                i += 2
            elif a == "--spec" and i + 1 < len(rest):
                spec = rest[i + 1]
                i += 2
            elif not a.startswith("-"):
                output = a
                i += 1
            else:
                ui.print_warn(f"Unknown flag: {a}")
                sys.exit(1)
        if not output:
            ui.print_usage(
                "Usage: cpy sef pack <output.sef> [--entry N] [--flags N] "
                "[--text FILE ...] [--data FILE ...] [--bss SIZE ...] [--spec JSON]"
            )
            sys.exit(1)
        sys.exit(
            cmd_pack(
                output,
                entry=entry,
                flags=flags,
                text=text,
                data=data,
                bss=bss,
                spec=spec,
            )
        )

    if cmd in ("dump", "check", "size", "digest"):
        json_out = False
        positional = []
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--json":
                json_out = True
                i += 1
            elif not a.startswith("-"):
                positional.append(a)
                i += 1
            else:
                ui.print_warn(f"Unknown flag: {a}")
                sys.exit(1)
        if len(positional) != 1:
            usage = f"Usage: cpy sef {cmd} <input.sef>"
            if cmd == "digest":
                usage += " [--json]"
            ui.print_usage(usage)
            sys.exit(1)
        if cmd == "dump":
            sys.exit(cmd_dump(positional[0]))
        if cmd == "check":
            sys.exit(cmd_check(positional[0]))
        if cmd == "size":
            sys.exit(cmd_size(positional[0]))
        sys.exit(cmd_digest(positional[0], json_out=json_out))

    ui.print_warn(f"Unknown sef subcommand: {cmd}")
    print(ui.paint_usage(_SEF_USAGE, stream=sys.stderr), file=sys.stderr)
    sys.exit(1)


def main():
    start_check(__version__)
    try:
        result = _main()
    except KeyboardInterrupt:
        ui.print_warn("interrupted")
        return 130
    except Exception:
        ui.print_err("Cpyte got an error :(. The following tracebacks:")
        ui.print_traceback()
        return 1
    sys.stdout.flush()
    report_update()
    return result


def _main():
    tab_size = 4
    mode = "jit"
    args = sys.argv[1:]

    strict = False
    no_userspace = False
    pic: bool = False
    lto = False
    no_gc = False
    exports = []
    auto_export = True
    # Dynamic-linking options forwarded verbatim to elf2sef (SEF v2 only).
    requires = []
    scopes = []
    weaks = []
    lazy = None
    versym = None
    opt = None
    while args and args[0].startswith("--"):
        flag = args.pop(0)
        if flag == "--tab-size":
            tab_size = int(args.pop(0))
        elif flag == "--strict":
            strict = True
        elif flag == "--no-userspace":
            no_userspace = True
        elif flag == "--nogc":
            no_gc = True
        elif flag == "--pic":
            pic = True
        elif flag == "--export":
            exports.append(args.pop(0))
        elif flag == "--no-auto-export":
            auto_export = False
        elif flag == "--require":
            requires.append(args.pop(0))
        elif flag == "--scope":
            scopes.append(args.pop(0))
        elif flag == "--weak":
            weaks.append(args.pop(0))
        elif flag == "--lazy":
            lazy = True
        elif flag == "--no-lazy":
            lazy = False
        elif flag == "--versym":
            versym = True
        elif flag == "--no-versym":
            versym = False
        elif flag in ("--cpu", "--march") and args:
            set_target_cpu(cpu=args.pop(0))
        elif flag == "--mattr" and args:
            set_target_cpu(features=args.pop(0))
        elif flag == "--lto":
            lto = True
        elif flag == "--opt" and args:
            opt = int(args.pop(0))
        elif flag == "--ast":
            mode = "ast"
        elif flag == "--emit-llvm":
            mode = "emit-llvm"
        elif flag == "--jit":
            mode = "jit"
        elif flag == "--aot":
            mode = "aot"
        elif flag == "--scorpion":
            mode = "scorpion"
        elif flag == "--version":
            print(ui.info(f"cpyte {__version__}"))
            sys.exit(0)
        elif flag == "--help":
            print(ui.paint_usage(_USAGE, stream=sys.stderr), file=sys.stderr)
            sys.exit(0)
        else:
            ui.print_warn(f"Unknown flag: {flag}")
            sys.exit(1)

    if args and args[0] in ("-h", "--help"):
        print(ui.paint_usage(_USAGE, stream=sys.stderr), file=sys.stderr)
        sys.exit(0)

    if not args:
        print(ui.paint_usage(_USAGE, stream=sys.stderr), file=sys.stderr)
        sys.exit(1)

    if args[0] == "build":
        cmd_build(
            args[1:],
            tab_size=tab_size,
            strict=strict,
            no_userspace=no_userspace,
            pic=pic,
            lto=lto,
            no_gc=no_gc,
        )
        return

    if args[0] == "format":
        cmd_format(args[1:], tab_size=tab_size)
        return

    if args[0] == "sef":
        cmd_sef(args[1:])
        return

    with open(args[0]) as f:
        source = f.read()

    source, no_gc = _detect_nogc(source) if not no_gc else (source, True)
    source, scorpion = _detect_scorpion(source)

    parsed, generic_instantiations = _compile(
        source,
        tab_size=tab_size,
        strict=strict,
        enable_extensions=not no_userspace,
        no_gc=no_gc,
        filepath=os.path.abspath(args[0]),
        scorpion=scorpion,
    )

    if mode == "ast":
        print(pretty_ast(parsed))
        sys.exit(0)

    if mode == "scorpion":
        prog, src_files = _emit(
            parsed,
            generic_instantiations=generic_instantiations,
            no_userspace=no_userspace,
            enable_extensions=not no_userspace,
            no_gc=True,
            target_triple="riscv32-unknown-elf",
            scorpion=scorpion,
        )
    elif mode == "aot":
        prog, src_files = _emit(
            parsed,
            generic_instantiations=generic_instantiations,
            no_userspace=no_userspace,
            enable_extensions=not no_userspace,
            no_gc=no_gc,
            use_native_eh=True,
            scorpion=scorpion,
        )
    else:
        prog, src_files = _emit(
            parsed,
            generic_instantiations=generic_instantiations,
            no_userspace=no_userspace,
            enable_extensions=not no_userspace,
            no_gc=no_gc,
            scorpion=scorpion,
        )

    if mode == "emit-llvm":
        print(prog)
    elif mode == "aot":
        out_base = args[0].rsplit(".", 1)[0] if "." in args[0] else "program"
        obj_file = "program.o"
        frameworks = _collect_frameworks(parsed)
        run_aot(
            prog,
            output=obj_file,
            src_files=src_files,
            no_userspace=no_userspace,
            pic=pic,
            lto=lto,
            frameworks=frameworks,
        )
        ui.print_ok(f"Wrote {out_base}")
    elif mode == "scorpion":
        out_base = args[0].rsplit(".", 1)[0] if "." in args[0] else "program"
        sef_file = out_base + ".sef"
        dl_only = [
            n
            for n, v in (
                ("--require", requires),
                ("--scope", scopes),
                ("--weak", weaks),
                ("--lazy", lazy),
                ("--versym", versym),
            )
            if v
        ]
        if dl_only and not pic:
            ui.print_warn(
                "%s %s only for dynamic SEF builds; add --pic"
                % (", ".join(dl_only), "apply" if len(dl_only) > 1 else "applies")
            )
        auto_exports = _collect_public_exports(parsed) if auto_export else []
        elf_file, final_exports = run_scorpion(
            prog,
            output=sef_file,
            src_files=src_files,
            pic=pic,
            exports=exports,
            auto_exports=auto_exports,
            requires=requires,
            scopes=scopes,
            weaks=weaks,
            lazy=lazy,
            versym=versym,
        )
        if pic and final_exports:
            protos, seen_tags = _scorpion_prototypes(final_exports, parsed)
            header_file = out_base + ".scorpion.h"
            _write_scorpion_header(header_file, protos, seen_tags, final_exports)
            ui.print_ok(f"Wrote {header_file} ({len(final_exports)} exports)")
            stub_file = out_base + ".scorpion.cpy"
            _write_scorpion_stub(stub_file, protos, seen_tags, final_exports)
            ui.print_ok(f"Wrote {stub_file} ({len(final_exports)} exports)")
    else:
        run_jit(
            prog,
            src_files=src_files,
            no_userspace=no_userspace,
            pic=pic,
            opt_level=2 if opt is None else opt,
        )


if __name__ == "__main__":
    main()
