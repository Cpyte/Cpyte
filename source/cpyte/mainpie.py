import sys
import os

if __package__:
    from . import __version__
    from .lexar import Lexer, LexerError, register_keywords
    from .astparse import parse_file, ParseError, Import
    from .semantic_analasis import analyze
    from .bytecoding import LLVM
    from .compiling import run_jit, run_aot, run_scorpion, optimize, _RUNTIME_C
    from .linker import Linker, find_linker, LinkerNotFoundError
    from ._bignum_bc import load_bignum_bc
    from ._gc_bc import load_gc_bc
    from .package_manifest import ManifestParser, get_global_registry, iter_cpm_version_dirs
    from .extension_hooks import HookLoader, get_global_hook_registry
    from .sef import cmd_pack, cmd_check, cmd_dump, cmd_size
    from .update_check import start_check, report_update
    from . import ui
else:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from cpyte import __version__
    from cpyte.lexar import Lexer, LexerError, register_keywords
    from cpyte.astparse import parse_file, ParseError, Import
    from cpyte.semantic_analasis import analyze
    from cpyte.bytecoding import LLVM
    from cpyte.compiling import run_jit, run_aot, run_scorpion, optimize, _RUNTIME_C
    from cpyte.linker import Linker, find_linker, LinkerNotFoundError
    from cpyte._bignum_bc import load_bignum_bc
    from cpyte._gc_bc import load_gc_bc
    from cpyte.package_manifest import ManifestParser, get_global_registry, iter_cpm_version_dirs
    from cpyte.extension_hooks import HookLoader, get_global_hook_registry
    from cpyte.sef import cmd_pack, cmd_check, cmd_dump, cmd_size
    from cpyte.update_check import start_check, report_update
    from cpyte import ui


_USAGE = """Usage: cpy [options] <source.cpy>
       cpy build [--output O] [--debug] [--opt N] [--osize] [--no-userspace] [--pic] [--lto] <source.cpy>
       cpy format [--write] [--check] [--tab-size N] <source.cpy>
       cpy sef <subcommand> ...

Global options:
  --tab-size N        Set tab size (default 4)
  --strict            Enable strict semantic analysis
  --no-userspace      Compile without the userspace runtime
  --pic               Position-independent code
  --export NAME       Export NAME as a library symbol (dynamic SEF; repeatable)
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
  sef                 Scorpion SEF binary tools (pack/dump/check/size)
"""


def pretty_ast(node, indent=0):
    pad = '  ' * indent
    if isinstance(node, list):
        if not node:
            return f'{pad}(empty)'
        lines = []
        for item in node:
            lines.append(pretty_ast(item, indent))
        return '\n'.join(lines)

    name = type(node).__name__

    if name == 'Number':
        return f'{pad}{node.value}'

    if name == 'String':
        return f"{pad}'{node.value}'"

    if name == 'Variable':
        return f'{pad}{node.name}'

    if name == 'VarDecl':
        init = f' = {pretty_ast(node.init, indent)}' if node.init else ''
        return f'{pad}{node.var_type} {node.name}{init}'

    if name == 'ExprStmt':
        return f'{pad}statement:\n{pretty_ast(node.expr, indent + 1)}'

    if name == 'Assign':
        if isinstance(node.target, str):
            return f'{pad}{node.target} =\n{pretty_ast(node.value, indent + 1)}'
        return f'{pad}{pretty_ast(node.target, 0)} =\n{pretty_ast(node.value, indent + 1)}'

    if name == 'Return':
        if node.value is None:
            return f'{pad}return'
        return f'{pad}return\n{pretty_ast(node.value, indent + 1)}'

    if name == 'Print':
        return f'{pad}print\n{pretty_ast(node.value, indent + 1)}'

    if name == 'Input':
        return f'{pad}input()'

    if name == 'Break':
        return f'{pad}break'

    if name == 'Continue':
        return f'{pad}continue'

    if name == 'If':
        result = f'{pad}if\n{pretty_ast(node.cond, indent + 1)}'
        result += f'\n{pad}then:\n{pretty_ast(node.body, indent + 1)}'
        if node.orelse:
            result += f'\n{pad}else:\n{pretty_ast(node.orelse, indent + 1)}'
        return result

    if name == 'While':
        result = f'{pad}while\n{pretty_ast(node.cond, indent + 1)}'
        result += f'\n{pad}body:\n{pretty_ast(node.body, indent + 1)}'
        return result

    if name == 'Import':
        extra = f' [{node.src_file}]' if node.src_file else ''
        return f'{pad}import {node.module}{extra}'

    if name == 'NewExpr':
        size = f'[{pretty_ast(node.size, 0)}]' if node.size else ''
        return f'{pad}new {node.type_expr}{size}'

    if name == 'Deref':
        return f'{pad}*\n{pretty_ast(node.operand, indent + 1)}'

    if name == 'AddrOf':
        return f'{pad}&\n{pretty_ast(node.operand, indent + 1)}'

    if name == 'SizeOf':
        return f'{pad}sizeof({node.type_expr})'

    if name == 'StructDef':
        gp = f'<{", ".join(node.generic_params)}>' if node.generic_params else ''
        result = f'{pad}struct {node.name}{gp}:'
        for f in node.fields:
            result += f'\n{pad}  {f.type_expr} {f.name}'
        return result

    if name == 'Field':
        return f'{pad}{node.type_expr} {node.name}'

    if name == 'Switch':
        result = f'{pad}switch\n{pretty_ast(node.value, indent + 1)}'
        for val, body in node.cases:
            label = 'default' if val is None else f'case {pretty_ast(val, 0)}'
            result += f'\n{pad}  {label}:\n{pretty_ast(body, indent + 2)}'
        return result

    if name == 'BinOp':
        return f'{pad}{node.op.name}\n{pretty_ast(node.left, indent + 1)}\n{pretty_ast(node.right, indent + 1)}'

    if name == 'UnaryOp':
        return f'{pad}{node.op.name}\n{pretty_ast(node.operand, indent + 1)}'

    if name == 'Call':
        result = f'{pad}call\n{pretty_ast(node.callee, indent + 1)}'
        if node.args:
            result += f'\n{pad}args:'
            for arg in node.args:
                result += f'\n{pretty_ast(arg, indent + 1)}'
        return result

    if name == 'Index':
        return f'{pad}index\n{pretty_ast(node.obj, indent + 1)}\n{pretty_ast(node.index, indent + 1)}'

    if name == 'Attr':
        return f'{pad}.{node.name}\n{pretty_ast(node.obj, indent + 1)}'

    if name == 'FuncDef':
        vis = f'{node.visibility} ' if node.visibility else ''
        ret = f' -> {node.rettype}' if node.rettype else ''
        const_params = set(getattr(node, 'const_params', None) or ())
        params = ', '.join(f'({k}): {v}' if k in const_params else f'{k}: {v}' for k, v in node.params.items())
        result = f'{pad}{vis}def {node.name}({params}){ret}:'
        for stmt in node.body:
            result += f'\n{pretty_ast(stmt, indent + 1)}'
        return result

    if isinstance(node, dict):
        t = node.get('type', '?')
        if t == 'class':
            result = f'{pad}class {node["name"]}:'
            for stmt in node.get('body', []):
                result += f'\n{pretty_ast(stmt, indent + 1)}'
            return result
        if t == 'for':
            result = f'{pad}for {node["var"]} in\n{pretty_ast(node["iter"], indent + 1)}'
            result += f'\n{pad}body:'
            for stmt in node.get('body', []):
                result += f'\n{pretty_ast(stmt, indent + 1)}'
            return result
        return f'{pad}{node}'

    return f'{pad}{node}'


def _load_package_manifests_from_source(workspace_root: str) -> None:
    """
    Load all package manifests from CPM packages in the workspace.
    
    This ensures that package extensions (keywords, operators, etc.) are
    available during lexing and parsing.
    """
    cpm_root = os.path.join(workspace_root, '.cpm', 'modules')
    if not os.path.isdir(cpm_root):
        return
    
    manifest_registry = get_global_registry()
    hook_registry = get_global_hook_registry()
    
    # Load all available packages in the workspace
    for package_name, version_dir in iter_cpm_version_dirs(cpm_root):
        # Check if already loaded
        if manifest_registry.is_loaded(package_name):
            continue
        
        manifest_path = os.path.join(version_dir, 'package.json')
        if not os.path.exists(manifest_path):
            continue
            
        try:
            manifest = ManifestParser.validate_and_parse(manifest_path)
            
            # Register keywords with lexer
            if manifest.capabilities.keywords:
                register_keywords(manifest.capabilities.keywords)
            
            # Register manifest
            manifest_registry.register(manifest)
            
            # Load hooks if present
            all_hook_files = (
                manifest.extensions.parser_hooks +
                manifest.extensions.semantic_hooks +
                manifest.extensions.codegen_hooks +
                manifest.extensions.runtime_hooks
            )
            
            if all_hook_files:
                context = {
                    'workspace_root': workspace_root,
                    'package_dir': version_dir,
                    'package_name': package_name,
                }
                
                HookLoader.load_hooks_from_package(
                    package_name, version_dir, all_hook_files,
                    hook_registry, context
                )
                
        except Exception as e:
            ui.print_warn(f"Failed to load package manifest for '{package_name}': {e}")


def _compile(source, tab_size=4, strict=False, enable_extensions=True):
    # Pre-load package manifests if extensions are enabled
    if enable_extensions:
        _load_package_manifests_from_source(os.getcwd())
    
    lex = Lexer(source, tab_size=tab_size, enable_extensions=enable_extensions)
    tokens = lex.get_tokens()
    try:
        parsed, _ = parse_file(tokens, enable_extensions=enable_extensions)
    except (LexerError, ParseError) as e:
        ui.print_err(f'parse error: {e}')
        sys.exit(1)
    result, generic_instantiations = analyze(source, parsed, strict=strict, workspace_root=os.getcwd(), enable_extensions=enable_extensions)
    if result:
        ui.print_err(result)
        sys.exit(1)
    return parsed, generic_instantiations


def _emit(parsed, generic_instantiations=None, no_userspace=False, enable_extensions=True, no_gc=False, target_triple=None, use_native_eh=False):
    c = LLVM(no_userspace=no_userspace, enable_extensions=enable_extensions, no_gc=no_gc, target_triple=target_triple, use_native_eh=use_native_eh)
    c.generic_instantiations = generic_instantiations or {}
    try:
        prog, src_files = c.emit_program(parsed)
    except SystemExit:
        raise
    except Exception as e:
        ui.print_err(f'codegen error: {type(e).__name__}: {e}')
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
            for attr in ('body', 'orelse', 'items', 'handlers', 'args'):
                val = getattr(node, attr, None)
                if isinstance(val, list):
                    stack.extend(val)
    return list(set(frameworks))


def cmd_build(args, tab_size=4, strict=False, no_userspace=False, pic=False, lto=False):
    if not args:
        ui.print_usage('Usage: cpy build [--output O] [--debug] [--opt N] [--osize] [--no-userspace] [--pic] [--lto] <source.cpy>')
        sys.exit(1)

    output = None
    debug = False
    opt = 3
    opt_size = False
    src_file = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == '-o' or a == '--output':
            if i + 1 < len(args):
                output = args[i + 1]
                i += 2
            else:
                ui.print_err(f'{a} requires an argument')
                sys.exit(1)
        elif a == '-g' or a == '--debug':
            debug = True
            i += 1
        elif a == '--no-userspace':
            no_userspace = True
            i += 1
        elif a == '--pic':
            pic = True
            i += 1
        elif a == '--lto':
            lto = True
            i += 1
        elif a == '--osize' or a == '-OSize':
            opt_size = True
            i += 1
        elif a == '--opt' and i + 1 < len(args):
            opt = int(args[i + 1])
            i += 2
        elif not a.startswith('-'):
            src_file = a
            i += 1
        else:
            ui.print_warn(f'Unknown flag: {a}')
            sys.exit(1)

    if not src_file:
        ui.print_usage('Usage: cpy build [--output O] [--debug] [--opt N] [--osize] [--pic] [--lto] <source.cpy>')
        sys.exit(1)

    if opt_size:
        # -OSize ignores speed entirely; cap at O2 so the O3/O4 pipelines never run.
        if opt > 2:
            ui.print_warn('-OSize ignores speed; capping --opt to 2')
            opt = 2

    with open(src_file) as f:
        source = f.read()

    parsed, generic_instantiations = _compile(source, tab_size=tab_size, strict=strict, enable_extensions=not no_userspace)

    frameworks = _collect_frameworks(parsed)

    ui.print_status(f'Compiling {src_file} ...')

    prog, src_files = _emit(parsed, generic_instantiations=generic_instantiations, no_userspace=no_userspace, enable_extensions=not no_userspace, use_native_eh=True)

    out_base = src_file.rsplit('.', 1)[0] if '.' in src_file else 'a'
    obj_file = out_base + '.o'

    import llvmlite.binding as binding

    binding.initialize_native_target()
    binding.initialize_native_asmprinter()

    mod = binding.parse_assembly(str(prog))
    bignum_mod = load_bignum_bc()
    binding.link_modules(mod, bignum_mod)
    gc_mod = load_gc_bc()
    binding.link_modules(mod, gc_mod)
    mod.verify()

    if lto or opt_size:
        optimize(mod, opt, opt_size=opt_size)
        mod.verify()

    target = binding.Target.from_default_triple()
    if pic:
        target_machine = target.create_target_machine(reloc='pic')
    else:
        target_machine = target.create_target_machine()
    obj = target_machine.emit_object(mod)
    with open(obj_file, 'wb') as f:
        f.write(obj)
    l = Linker(lto=lto)
    objs = [obj_file]

    for src in (src_files or []):
        src_obj = src.rsplit('.', 1)[0] + '.o'
        l.compile_c(src, output=src_obj, opt_level=opt, opt_size=opt_size, debug=debug, pic=pic)
        objs.append(src_obj)

    if not no_userspace:
        runtime_obj = out_base + '.runtime.o'
        l.compile_c(_RUNTIME_C, output=runtime_obj, opt_level=opt, opt_size=opt_size, debug=debug, pic=pic, eh=True)
        objs.append(runtime_obj)

    executable = output or out_base
    l.link(objs, executable, libraries=['m'], opt_level=opt, opt_size=opt_size, debug=debug, frameworks=frameworks, pic=pic)
    ui.print_ok(f'Wrote {executable}')


def cmd_format(args, tab_size=4):
    if not args or args[0] in ('-h', '--help'):
        ui.print_usage('Usage: cpy format [--write] [--check] [--tab-size N] [--no-extensions] <source.cpy>')
        sys.exit(0 if args else 1)

    write = False
    check = False
    enable_extensions = True
    path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ('-w', '--write'):
            write = True
            i += 1
        elif a in ('-c', '--check'):
            check = True
            i += 1
        elif a == '--tab-size' and i + 1 < len(args):
            tab_size = int(args[i + 1])
            i += 2
        elif a == '--no-extensions':
            enable_extensions = False
            i += 1
        elif not a.startswith('-'):
            path = a
            i += 1
        else:
            ui.print_warn(f'Unknown flag: {a}')
            sys.exit(1)

    if not path:
        ui.print_usage('Usage: cpy format [--write] [--check] [--tab-size N] [--no-extensions] <source.cpy>')
        sys.exit(1)

    from cpyte.formatter import format_file
    result, code = format_file(path, write=write, check=check, tab_size=tab_size, enable_extensions=enable_extensions)

    if code != 0:
        for err in result.errors:
            ui.print_err(f'error: {err}')
        if check and not result.errors:
            ui.print_err(f'{path}: file is not formatted')
        elif not result.errors:
            ui.print_err(f'{path}: formatting failed')
        sys.exit(1)

    if write:
        ui.print_ok(f'Formatted {path}')
    elif check:
        ui.print_ok(f'{path}: ok')
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
"""


def cmd_sef(args):
    if not args or args[0] in ('-h', '--help'):
        print(ui.paint_usage(_SEF_USAGE, stream=sys.stderr), file=sys.stderr)
        sys.exit(0 if args else 1)

    cmd = args[0]
    rest = args[1:]

    if cmd == 'pack':
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
            if a == '--entry' and i + 1 < len(rest):
                entry = int(rest[i + 1], 0)
                i += 2
            elif a == '--flags' and i + 1 < len(rest):
                flags = int(rest[i + 1], 0)
                i += 2
            elif a == '--text' and i + 1 < len(rest):
                text.append(rest[i + 1])
                i += 2
            elif a == '--data' and i + 1 < len(rest):
                data.append(rest[i + 1])
                i += 2
            elif a == '--bss' and i + 1 < len(rest):
                bss.append(int(rest[i + 1], 0))
                i += 2
            elif a == '--spec' and i + 1 < len(rest):
                spec = rest[i + 1]
                i += 2
            elif not a.startswith('-'):
                output = a
                i += 1
            else:
                ui.print_warn(f'Unknown flag: {a}')
                sys.exit(1)
        if not output:
            ui.print_usage('Usage: cpy sef pack <output.sef> [--entry N] [--flags N] '
                           '[--text FILE ...] [--data FILE ...] [--bss SIZE ...] [--spec JSON]')
            sys.exit(1)
        sys.exit(cmd_pack(output, entry=entry, flags=flags, text=text, data=data,
                          bss=bss, spec=spec))

    if cmd in ('dump', 'check', 'size'):
        if len(rest) != 1:
            ui.print_usage(f'Usage: cpy sef {cmd} <input.sef>')
            sys.exit(1)
        fn = {'dump': cmd_dump, 'check': cmd_check, 'size': cmd_size}[cmd]
        sys.exit(fn(rest[0]))

    ui.print_warn(f'Unknown sef subcommand: {cmd}')
    print(ui.paint_usage(_SEF_USAGE, stream=sys.stderr), file=sys.stderr)
    sys.exit(1)


def main():
    start_check(__version__)
    try:
        result = _main()
    except KeyboardInterrupt:
        ui.print_warn('interrupted')
        return 130
    except Exception:
        ui.print_err('cpy error:')
        ui.print_traceback()
        return 1
    sys.stdout.flush()
    report_update()
    return result


def _main():
    tab_size = 4
    mode = 'jit'
    args = sys.argv[1:]

    strict = False
    no_userspace = False
    pic = False
    lto = False
    exports = []
    while args and args[0].startswith('--'):
        flag = args.pop(0)
        if flag == '--tab-size':
            tab_size = int(args.pop(0))
        elif flag == '--strict':
            strict = True
        elif flag == '--no-userspace':
            no_userspace = True
        elif flag == '--pic':
            pic = True
        elif flag == '--export':
            exports.append(args.pop(0))
        elif flag == '--lto':
            lto = True
        elif flag == '--ast':
            mode = 'ast'
        elif flag == '--emit-llvm':
            mode = 'emit-llvm'
        elif flag == '--jit':
            mode = 'jit'
        elif flag == '--aot':
            mode = 'aot'
        elif flag == '--scorpion':
            mode = 'scorpion'
        elif flag == '--version':
            print(ui.info(f'cpyte {__version__}'))
            sys.exit(0)
        elif flag == '--help':
            print(ui.paint_usage(_USAGE, stream=sys.stderr), file=sys.stderr)
            sys.exit(0)
        else:
            ui.print_warn(f'Unknown flag: {flag}')
            sys.exit(1)

    if args and args[0] in ('-h', '--help'):
        print(ui.paint_usage(_USAGE, stream=sys.stderr), file=sys.stderr)
        sys.exit(0)

    if not args:
        print(ui.paint_usage(_USAGE, stream=sys.stderr), file=sys.stderr)
        sys.exit(1)

    if args[0] == 'build':
        cmd_build(args[1:], tab_size=tab_size, strict=strict, no_userspace=no_userspace, pic=pic, lto=lto)
        return

    if args[0] == 'format':
        cmd_format(args[1:], tab_size=tab_size)
        return

    if args[0] == 'sef':
        cmd_sef(args[1:])
        return

    with open(args[0]) as f:
        source = f.read()

    parsed, generic_instantiations = _compile(source, tab_size=tab_size, strict=strict, enable_extensions=not no_userspace)

    if mode == 'ast':
        print(pretty_ast(parsed))
        sys.exit(0)

    if mode == 'scorpion':
        prog, src_files = _emit(parsed, generic_instantiations=generic_instantiations, no_userspace=no_userspace, enable_extensions=not no_userspace, no_gc=True, target_triple='riscv32-unknown-elf')
    elif mode == 'aot':
        prog, src_files = _emit(parsed, generic_instantiations=generic_instantiations, no_userspace=no_userspace, enable_extensions=not no_userspace, use_native_eh=True)
    else:
        prog, src_files = _emit(parsed, generic_instantiations=generic_instantiations, no_userspace=no_userspace, enable_extensions=not no_userspace)

    if mode == 'emit-llvm':
        print(prog)
    elif mode == 'aot':
        out_base = args[0].rsplit('.', 1)[0] if '.' in args[0] else 'program'
        obj_file = 'program.o'
        frameworks = _collect_frameworks(parsed)
        run_aot(prog, output=obj_file, src_files=src_files, no_userspace=no_userspace, pic=pic, lto=lto, frameworks=frameworks)
        ui.print_ok(f'Wrote {out_base}')
    elif mode == 'scorpion':
        out_base = args[0].rsplit('.', 1)[0] if '.' in args[0] else 'program'
        sef_file = out_base + '.sef'
        run_scorpion(prog, output=sef_file, src_files=src_files, pic=pic, exports=exports)
        ui.print_ok(f'Wrote {sef_file}')
    else:
        run_jit(prog, src_files=src_files, no_userspace=no_userspace, pic=pic)


if __name__ == '__main__':
    main()
