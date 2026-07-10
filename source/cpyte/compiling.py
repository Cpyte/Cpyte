import atexit
import ctypes
import ctypes.util
import subprocess
import os
import sys
import tempfile
import warnings

# ctypes warns "memory leak in callback function" when a CFUNCTYPE wrapper
# is garbage collected, even if no C code will ever call it again.  We keep
# all runtime callbacks alive in _callbacks for the program's lifetime, but
# the warning still fires during interpreter shutdown when module globals are
# cleared.  This is harmless so we suppress it.
warnings.filterwarnings("ignore", category=RuntimeWarning,
                        message="memory leak in callback function")

if getattr(sys, 'frozen', False):
    _RUNTIME_C = os.path.join(sys._MEIPASS, 'runtime.c')
else:
    _RUNTIME_C = os.path.join(os.path.dirname(__file__), 'runtime.c')

_callbacks: list = []

_libc = ctypes.CDLL(ctypes.util.find_library('c'))
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
    print(s.decode('utf-8'))


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
    pto = binding.create_pipeline_tuning_options(speed_level=opt_level)
    pb = binding.create_pass_builder(target_machine, pto)
    npm = pb.getModulePassManager()
    npm.run(mod, pb)


def _compile_sources_object(src_files, target_triple=None):
    objs = []
    for src in src_files:
        obj = src.rsplit('.', 1)[0] + '.o'
        cmd = ['clang', '-c', '-O3', '-o', obj, src]
        if target_triple:
            cmd.extend(['-target', target_triple])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f'error compiling {src}: {r.stderr}', file=__import__('sys').stderr)
            raise SystemExit(1)
        objs.append(obj)
    return objs


def _remove_probe_stack_ir(llvm_ir):
    import re
    return re.sub(r'\s+"probe-stack"="[^"]*"', '', llvm_ir)


def run_jit(module, opt_level=3, src_files=None):
    global _print_fn, _input_fn
    from llvmlite import binding
    binding.initialize_native_target()
    binding.initialize_native_asmprinter()

    llvm_ir = str(module)
    mod = binding.parse_assembly(llvm_ir)
    if src_files:
        target = binding.Target.from_default_triple()
        for src in src_files:
            ll = src.rsplit('.', 1)[0] + '.ll'
            r = subprocess.run(
                ['clang', '-S', '-emit-llvm', '-O0', '-target', target.triple,
                 '-fno-stack-protector', '-o', ll, src],
                capture_output=True, text=True
            )
            if r.returncode != 0:
                print(f'error compiling {src}: {r.stderr}', file=__import__('sys').stderr)
                raise SystemExit(1)
            src_ir = open(ll).read()
            src_ir = _remove_probe_stack_ir(src_ir)
            src_mod = binding.parse_assembly(src_ir)
            binding.link_modules(mod, src_mod)

    mod.verify()
    optimize(mod, opt_level)
    mod.verify()

    target = binding.Target.from_default_triple()
    target_machine = target.create_target_machine()

    backing_mod = binding.parse_assembly("")
    engine = binding.create_mcjit_compiler(backing_mod, target_machine)
    engine.add_module(mod)

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
    _map_libc_fn(engine, mod, 'strlen', ctypes.c_char_p, ctypes.c_int)
    _map_libc_fn(engine, mod, 'memcpy', None, ctypes.c_void_p,
                 argtypes=[ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int])

    engine.finalize_object()
    engine.run_static_constructors()

    func_ptr = engine.get_function_address("main")
    _main_fn = ctypes.CFUNCTYPE(ctypes.c_int)(func_ptr)
    _callbacks.append(_main_fn)
    ret = _main_fn()
    return ret


def _map_libc_fn(engine, mod, name, argtype, restype, argtypes=None):
    try:
        fn = mod.get_function(name)
        if argtypes:
            cfunctype = ctypes.CFUNCTYPE(restype, *argtypes)
            cfn = cfunctype(getattr(_libc, name))
        elif argtype is None:
            return
        else:
            cfunctype = ctypes.CFUNCTYPE(restype, argtype)
            cfn = cfunctype(getattr(_libc, name))
        _callbacks.append(cfn)
        engine.add_global_mapping(
            fn,
            ctypes.cast(cfn, ctypes.c_void_p).value,
        )
    except NameError:
        pass


def run_aot(module, output="program.o", opt_level=3, src_files=None):
    llvm_ir = str(module)
    import llvmlite.binding as binding
    binding.initialize_native_target()
    binding.initialize_native_asmprinter()

    mod = binding.parse_assembly(llvm_ir)
    mod.verify()
    optimize(mod, opt_level)
    mod.verify()

    target = binding.Target.from_default_triple()
    target_machine = target.create_target_machine()

    obj = target_machine.emit_object(mod)

    with open(output, "wb") as f:
        f.write(obj)

    objs = [output]
    for src in (src_files or []):
        src_obj = src.rsplit('.', 1)[0] + '.o'
        r = subprocess.run(
            ['clang', '-c', '-O3', '-o', src_obj, src],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            print(f'error compiling {src}: {r.stderr}', file=__import__('sys').stderr)
            raise SystemExit(1)
        objs.append(src_obj)
    runtime_obj = output + '.runtime.o'
    r = subprocess.run(
        ['clang', '-c', '-O3', '-o', runtime_obj, _RUNTIME_C],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        objs.append(runtime_obj)

    out_name = output.rsplit('.', 1)[0] if '.' in output else output
    r = subprocess.run(
        ['clang', '-O3', '-o', out_name] + objs + ['-lm'],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        print(f'error linking: {r.stderr}', file=__import__('sys').stderr)
        raise SystemExit(1)