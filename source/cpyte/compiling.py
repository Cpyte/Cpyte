import ctypes
import ctypes.util
import os
import subprocess
import sys
import warnings

from ._bignum_bc import load_bignum_bc
from ._gc_bc import load_gc_bc
from .generate_bc import _remove_probe_stack_ir
from .ui import print_err, print_ok

# Suppress ctypes callback cleanup warning during shutdown (harmless)
warnings.filterwarnings("ignore", category=RuntimeWarning,
                        message="memory leak in callback function")

if getattr(sys, 'frozen', False):
    _RUNTIME_C = os.path.join(getattr(sys, '_MEIPASS', ''), 'runtime.c')
else:
    _RUNTIME_C = os.path.join(os.path.dirname(__file__), 'runtime.c')

_callbacks: list = []


def _load_libc():
    """Open libc bypassing symbol interposition where possible.

    On macOS, tools like MallocStackLogging may interpose malloc/free with a
    private arena allocator whose pointers the interposed free() rejects
    ("pointer being freed was not allocated"). Using RTLD_FIRST forces dlsym
    to resolve to the real libSystem symbols so JIT'd malloc/free/realloc/
    calloc stay self-consistent.
    """
    if sys.platform == 'darwin':
        RTLD_FIRST = 0x100
        try:
            return ctypes.CDLL(
                '/usr/lib/libSystem.B.dylib',
                mode=os.RTLD_NOW | RTLD_FIRST,
            )
        except OSError:
            pass
    return ctypes.CDLL(ctypes.util.find_library('c'))


_libc = _load_libc()
_libc.strlen.argtypes = [ctypes.c_char_p]
_libc.strlen.restype = ctypes.c_int
_libc.memcpy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
_libc.memcpy.restype = ctypes.c_void_p
_libc.malloc.argtypes = [ctypes.c_size_t]
_libc.malloc.restype = ctypes.c_void_p


def _runtime_print(n: int):
    print(n)


def _runtime_print_int64(n: int):
    print(n)


def _runtime_print_uint64(n: int):
    if n < 0:
        # Handle unsigned interpretation
        n = n & ((1 << 64) - 1)
    print(n)


def _runtime_print_double(d: float):
    print(f"{d:.6f}")


def _runtime_print_str(s: bytes):
    if s is None:
        print("(null)")
    else:
        try:
            print(s.decode('utf-8'))
        except UnicodeDecodeError:
            print(repr(s))


def _runtime_input() -> int:
    return int(input())


def _runtime_input_str() -> bytes:
    return input().encode('utf-8')


def optimize(mod, opt_level=3):
    from llvmlite import binding
    binding.initialize_native_target()
    binding.initialize_native_asmprinter()
    if opt_level <= 0:
        return
    target = binding.Target.from_default_triple()
    target_machine = target.create_target_machine()
    pto = binding.create_pipeline_tuning_options(speed_level=min(opt_level, 3))

    heavy = opt_level >= 3
    extra_heavy = opt_level >= 4

    if opt_level >= 2:
        pto.slp_vectorization = True
    if heavy:
        pto.inlining_threshold = 450 if extra_heavy else 275
        pto.loop_unrolling = True
        pto.loop_vectorization = True
        pto.loop_interleaving = True

    pb = binding.create_pass_builder(target_machine, pto)

    # Phase 1: infer function attributes (readonly/noalias/nonnull) so every
    # later pass can exploit them (RPO function-attrs runs as a module pass).
    if heavy:
        mpm = pb.getModulePassManager()
        mpm.add_rpo_function_attrs_pass()
        mpm.run(mod, pb)

    # Phase 2: aggressive per-function simplification. Promote allocas to SSA,
    # fold constants, remove dead stores/blocks, and specialise loops.
    if opt_level >= 2:
        fpm = pb.getFunctionPassManager()
        fpm.add_simplify_cfg_pass()
        fpm.add_sroa_pass()
        fpm.add_instruction_combine_pass()
        if heavy:
            fpm.add_new_gvn_pass()              # global value numbering
            fpm.add_instruction_combine_pass()
            fpm.add_sccp_pass()                 # sparse conditional constant propagation
            fpm.add_reassociate_pass()
            fpm.add_jump_threading_pass()
            fpm.add_loop_rotate_pass()
            fpm.add_loop_unroll_pass()
            fpm.add_loop_strength_reduce_pass()
            fpm.add_sinking_pass()
            fpm.add_mem_copy_opt_pass()
            fpm.add_dead_store_elimination_pass()
            fpm.add_tail_call_elimination_pass()
            fpm.add_aggressive_dce_pass()
        fpm.add_simplify_cfg_pass()
        for fn in mod.functions:
            if not fn.is_declaration:
                fpm.run(fn, pb)

    # Phase 3: aggressive interprocedural module passes (by-ref promotion,
    # whole-module constant propagation, dead argument/function elimination,
    # function merging). These shrink and specialise the module before the
    # default pipeline runs the inliner and vectorizers.
    if heavy:
        mpm = pb.getModulePassManager()
        mpm.add_argument_promotion_pass()
        mpm.add_ipsccp_pass()
        mpm.add_global_opt_pass()
        mpm.add_constant_merge_pass()
        mpm.add_global_dead_code_eliminate_pass()
        mpm.add_dead_arg_elimination_pass()
        if extra_heavy:
            mpm.add_post_order_function_attributes_pass()
            mpm.add_aggressive_instcombine_pass()
            mpm.add_always_inliner_pass()
            mpm.add_partial_inliner_pass()
            mpm.add_merge_functions_pass()
        mpm.run(mod, pb)

    # Default module pipeline (includes inliner, GVN, DCE, loop and vectorization opts)
    npm = pb.getModulePassManager()

    # Let extension hooks add their own passes
    try:
        from .extension_hooks import get_global_hook_registry
        registry = get_global_hook_registry()
        for hook in registry.get_codegen_hooks():
            if hook.should_add_module_passes():
                hook.add_module_passes(npm, {})
    except Exception:
        pass

    npm.run(mod, pb)


# Bruh, dead code.


def _maybe_compile(module, use_native_eh=False):
    if isinstance(module, list):
        from .bytecoding import LLVM
        c = LLVM(use_native_eh=use_native_eh)
        prog, src_files = c.emit_program(module)
        return prog, src_files
    return module, None

def run_jit(module, opt_level=3, src_files=None, no_userspace=False, pic=False, use_native_eh=False):
    module, src_files_auto = _maybe_compile(module, use_native_eh=use_native_eh)
    if src_files_auto is not None:
        src_files = src_files_auto
    global _print_fn, _input_fn
    from llvmlite import binding
    binding.initialize_native_target()
    binding.initialize_native_asmprinter()

    llvm_ir = str(module)
    mod = binding.parse_assembly(llvm_ir)
    if src_files:
        target = binding.Target.from_default_triple()
        for src in src_files:
            if src.endswith('.ll'):
                with open(src) as f:
                    src_ir = f.read()
            else:
                r = subprocess.run(
                    ['clang', '-S', '-emit-llvm', '-O0', '-target', target.triple,
                     '-fno-stack-protector', '-o', '-', src],
                    capture_output=True, text=True)

                if r.returncode != 0:
                    print(f'error compiling {src}: {r.stderr}', file=__import__('sys').stderr)
                    raise SystemExit(1)
                src_ir = r.stdout
            src_ir = _remove_probe_stack_ir(src_ir)
            src_mod = binding.parse_assembly(src_ir)
            binding.link_modules(mod, src_mod)

    bignum_mod = load_bignum_bc()
    binding.link_modules(mod, bignum_mod)

    gc_mod = load_gc_bc()
    binding.link_modules(mod, gc_mod)

    mod.verify()
    optimize(mod, opt_level)
    mod.verify()

    target = binding.Target.from_default_triple()
    target_machine = target.create_target_machine()

    backing_mod = binding.parse_assembly("")
    engine = binding.create_mcjit_compiler(backing_mod, target_machine)
    engine.add_module(mod)

    if not no_userspace:
        cb = ctypes.CFUNCTYPE(None, ctypes.c_int)(_runtime_print)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("print_int"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(None, ctypes.c_longlong)(_runtime_print_int64)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("print_int64"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(None, ctypes.c_ulonglong)(_runtime_print_uint64)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("print_uint64"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(None, ctypes.c_double)(_runtime_print_double)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("print_double"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(ctypes.c_int)(_runtime_input)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("input_int"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(ctypes.c_char_p)(_runtime_input_str)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("input_str"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(None, ctypes.c_char_p)(_runtime_print_str)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("print_str"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

    _map_libc_fn(engine, mod, 'malloc', ctypes.c_size_t, ctypes.c_void_p)
    _map_libc_fn(engine, mod, 'free', None, None, argtypes=[ctypes.c_void_p])
    _map_libc_fn(engine, mod, 'realloc', ctypes.c_void_p, ctypes.c_void_p,
                 argtypes=[ctypes.c_void_p, ctypes.c_size_t])
    _map_libc_fn(engine, mod, 'calloc', ctypes.c_size_t, ctypes.c_void_p,
                 argtypes=[ctypes.c_size_t, ctypes.c_size_t])
    _map_libc_fn(engine, mod, 'strlen', ctypes.c_char_p, ctypes.c_int)
    _map_libc_fn(engine, mod, 'memcpy', None, ctypes.c_void_p,
                 argtypes=[ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int])

    try:
        fn = mod.get_function('strcmp')
        engine.add_global_mapping(fn, _libc_addr('strcmp'))
    except NameError:
        pass

    # Bruh, dead code.

    # GC functions come from the linked gc_runtime bitcode

    engine.finalize_object()
    engine.run_static_constructors()

    func_ptr = engine.get_function_address("main")
    _main_fn = ctypes.CFUNCTYPE(ctypes.c_int)(func_ptr)
    _callbacks.append(_main_fn)
    ret = _main_fn()
    return ret


def _libc_addr(name):
    """Return the raw address of a libc function.

    Returns the real libSystem implementation (RTLD_FIRST on macOS), avoiding
    allocator interposition that can break malloc/free round-trips in JIT'd
    code.
    """
    return ctypes.cast(getattr(_libc, name), ctypes.c_void_p).value


def _map_libc_fn(engine, mod, name, argtype, restype, argtypes=None):
    """Map an external libc symbol in the JIT module to its raw native address.

    Direct raw-address mapping (rather than an ffi closure) avoids the
    closure -> Python -> ctypes -> libffi round trip, which is fragile under
    malloc interposers and slower at runtime.
    """
    try:
        fn = mod.get_function(name)
    except NameError:
        return
    engine.add_global_mapping(fn, _libc_addr(name))


def run_aot(module, output="program.o", opt_level=3, src_files=None, no_userspace=False, pic=False, lto=False):
    llvm_ir = str(module)
    import llvmlite.binding as binding
    binding.initialize_native_target()
    binding.initialize_native_asmprinter()

    mod = binding.parse_assembly(llvm_ir)
    bignum_mod = load_bignum_bc()
    binding.link_modules(mod, bignum_mod)
    gc_mod = load_gc_bc()
    binding.link_modules(mod, gc_mod)
    mod.verify()
    optimize(mod, opt_level)
    mod.verify()

    target = binding.Target.from_default_triple()
    if pic:
        target_machine = target.create_target_machine(reloc='pic')
    else:
        target_machine = target.create_target_machine()

    obj = target_machine.emit_object(mod)

    with open(output, "wb") as f:
        f.write(obj)

    objs = [output]
    for src in (src_files or []):
        src_obj = src.rsplit('.', 1)[0] + '.o'
        cmd = ['clang', '-c', '-O3', '-o', src_obj, src]
        if lto:
            cmd.append('-flto')
        if pic:
            cmd.append('-fPIC')
        r = subprocess.run(
            cmd,
            capture_output=True, text=True
        )
        if r.returncode != 0:
            print(f'error compiling {src}: {r.stderr}', file=__import__('sys').stderr)
            raise SystemExit(1)
        objs.append(src_obj)
    
    if not no_userspace:
        runtime_obj = output + '.runtime.o'
        cmd = ['clang', '-c', '-O3', '-o', runtime_obj, _RUNTIME_C]
        cmd.append('-fexceptions')
        cmd.append('-funwind-tables')
        if lto:
            cmd.append('-flto')
        if pic:
            cmd.append('-fPIC')
        r = subprocess.run(
            cmd,
            capture_output=True, text=True
        )
        if r.returncode == 0:
            objs.append(runtime_obj)

    out_name = output.rsplit('.', 1)[0] if '.' in output else output
    link_cmd = ['clang', '-O3', '-o', out_name] + objs + ['-lm']
    if lto:
        link_cmd.insert(1, '-flto')
    r = subprocess.run(
        link_cmd,
        capture_output=True, text=True
    )
    if r.returncode != 0:
        print_err(f'error linking: {r.stderr}')
        raise SystemExit(1)


if getattr(sys, 'frozen', False):
    _RUNTIME_SCORPION_C = os.path.join(getattr(sys, '_MEIPASS', ''), 'runtime_scorpion.c')
else:
    _RUNTIME_SCORPION_C = os.path.join(os.path.dirname(__file__), 'runtime_scorpion.c')

_SCORPION_CC = 'riscv32-unknown-elf-gcc'
_SCORPION_AS = 'riscv64-elf-as'
_SCORPION_LD = 'riscv64-elf-ld'
_SCORPION_OBJCOPY = 'riscv64-elf-objcopy'
_SCORPION_ARCH = '-march=rv32imac_zicsr_zifencei_zba_zbb_zbs_zbkb'
_SCORPION_ABI = '-mabi=ilp32'


def _find_scorpion_tool(name, fallback):
    """Find a scorpion cross-compilation tool."""
    import shutil
    candidates = [
        f'riscv32-unknown-elf-{name}',
        f'riscv64-unknown-elf-{name}',
        f'riscv64-elf-{name}',
    ]
    for c in candidates:
        if shutil.which(c):
            return c
    return fallback


def run_scorpion(module, output='program.sef', opt_level=3, src_files=None, pic=False,
                 exports=None):
    """Compile a Cpyte module for Scorpion (RV32 bare-metal) producing a SEF file.
    Please use it good! :):)

    With pic=True the module is compiled with -fPIC and the final ELF is linked
    with --emit-relocs so it can be converted to a dynamic (SEF v2) image via
    WEW-scorpion/tools/elf2sef.py. `exports` names the symbols to mark as
    exported for dynamic linking (libraries).
    """
    import llvmlite.binding as binding
    binding.initialize_all_targets()
    binding.initialize_all_asmprinters()

    llvm_ir = str(module)
    mod = binding.parse_assembly(llvm_ir)

    mod.verify()
    optimize(mod, opt_level)
    mod.verify()

    target = binding.Target.from_triple('riscv32-unknown-elf')
    if pic:
        target_machine = target.create_target_machine(reloc='pic')
    else:
        target_machine = target.create_target_machine()

    # Emit RV32 object file
    obj = target_machine.emit_object(mod)
    obj_file = output.rsplit('.', 1)[0] + '.o'
    with open(obj_file, 'wb') as f:
        f.write(obj)

    objs = [obj_file]

    # Compile Scorpion runtime
    runtime_obj = output.rsplit('.', 1)[0] + '.runtime.o'
    cc = _find_scorpion_tool('gcc', _SCORPION_CC)
    cmd = [cc, '-c', '-O3', _SCORPION_ARCH, _SCORPION_ABI,
           '-nostdlib', '-ffreestanding',
           '-o', runtime_obj, _RUNTIME_SCORPION_C]
    if pic:
        cmd.append('-fPIC') # This must be done to be ran on microcontrollers.
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f'error compiling runtime_scorpion.c: {r.stderr}', file=sys.stderr)
        raise SystemExit(1)
    objs.append(runtime_obj)

    # Link into ELF using the GCC driver so libgcc (e.g. __fixdfsi) is resolved
    elf_base = output.rsplit('.', 1)[0]
    elf_file = elf_base + '.elf'
    cc = _find_scorpion_tool('gcc', _SCORPION_CC)
    cmd = [cc, _SCORPION_ARCH, _SCORPION_ABI,
           '-nostdlib', '-nostartfiles',
           '-Wl,-e,main', '-Wl,--no-relax', '-Wl,-Ttext=0',
           '-o', elf_file] + objs + ['-lgcc']
    if pic:
        cmd += ['-Wl,-q', '-Wl,--unresolved-symbols=ignore-all']
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print_err(f'error linking: {r.stderr}')
        raise SystemExit(1)

    if pic:
        # Dynamic SEF v2: relocation + import/export records
        elf2sef = os.path.join(os.path.dirname(os.path.dirname(_RUNTIME_SCORPION_C)),
                               '..', '..', 'WEW-scorpion', 'tools', 'elf2sef.py')
        cmd = [sys.executable, elf2sef, elf_file, output]
        if exports:
            for name in exports:
                cmd += ['--export', name]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print_err(f'error converting to SEF: {r.stderr}')
            raise SystemExit(1)
    else:
        # Static SEF v1 via mksef.py
        mksef = os.path.join(os.path.dirname(os.path.dirname(_RUNTIME_SCORPION_C)),
                             '..', '..', 'WEW-scorpion', 'user', 'mksef.py')
        if not os.path.isfile(mksef):
            # Fallback: inline SEF generation using objdump/objcopy
            _elf_to_sef(elf_file, output, 0)
        else:
            r = subprocess.run([sys.executable, mksef, elf_file, output],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print_err(f'error converting to SEF: {r.stderr}')
                raise SystemExit(1)

    print_ok(f'Wrote {os.path.getsize(output)} bytes to {output}')
    return elf_file


def _elf_to_sef(elf_path, sef_output, flags=0):
    """Convert ELF to SEF format without mksef.py."""
    import struct

    sections = {}
    result = subprocess.run(
        [_find_scorpion_tool('objdump', _SCORPION_LD.replace('ld', 'objdump')),
         '-h', elf_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"error: objdump failed on {elf_path}", file=sys.stderr)
        raise SystemExit(1)

    for line in result.stdout.split('\n'):
        parts = line.split()
        if len(parts) >= 4 and parts[0].isdigit():
            name = parts[1]
            if name in ('.text', '.rodata', '.data', '.bss'):
                sections[name] = int(parts[2], 16)

    result = subprocess.run(
        [_find_scorpion_tool('readelf', _SCORPION_LD.replace('ld', 'readelf')),
         '-h', elf_path],
        capture_output=True, text=True
    )
    entry = 0
    for line in result.stdout.split('\n'):
        if 'Entry point address' in line:
            entry = int(line.split(':')[1].strip(), 16)

    bin_path = elf_path + '.bin'
    objcopy = _find_scorpion_tool('objcopy', _SCORPION_OBJCOPY)
    subprocess.run([objcopy, '-O', 'binary', elf_path, bin_path],
                   capture_output=True)

    with open(bin_path, 'rb') as f:
        flat = f.read()
    os.unlink(bin_path)

    segments = []
    off = 0
    for sec in ('.text', '.rodata', '.data'):
        if sec in sections:
            segments.append((0 if sec == '.text' else 1, off, sections[sec]))
            off += sections[sec]
    if '.bss' in sections:
        segments.append((2, off, sections['.bss']))

    num = len(segments)
    hdr = 12 + num * 16

    out = bytearray()
    out += struct.pack('<IIHH', 0x00464553, entry, num, flags)
    dc = hdr
    for st, sv, ss in segments:
        out += struct.pack('<IIII', st, sv, ss, dc)
        dc += ss
    out += flat

    with open(sef_output, 'wb') as f:
        f.write(out)