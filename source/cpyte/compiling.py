import ctypes
import ctypes.util
import os
import struct
import subprocess
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
import threading

from .generate_bc import _remove_probe_stack_ir
from .linker import format_cc_diag, LinkerNotFoundError, Linker
from .ui import print_err, print_ok
from .winjit_patch import link_jit_anchors, patch_windows_gnu_relocs

# Suppress ctypes callback cleanup warning during shutdown (harmless)
warnings.filterwarnings(
    "ignore", category=RuntimeWarning, message="memory leak in callback function"
)

if getattr(sys, "frozen", False):
    _RUNTIME_C = os.path.join(getattr(sys, "_MEIPASS", ""), "runtime.c")
else:
    _RUNTIME_C = os.path.join(os.path.dirname(__file__), "runtime.c")

if getattr(sys, "frozen", False):
    _GC_RUNTIME_C = os.path.join(getattr(sys, "_MEIPASS", ""), "gc_runtime.c")
else:
    _GC_RUNTIME_C = os.path.join(os.path.dirname(__file__), "gc_runtime.c")

if getattr(sys, "frozen", False):
    _BIGNUM_C = os.path.join(getattr(sys, "_MEIPASS", ""), "bignum.c")
else:
    _BIGNUM_C = os.path.join(os.path.dirname(__file__), "bignum.c")

if getattr(sys, "frozen", False):
    _WINJIT_STUB_C = os.path.join(getattr(sys, "_MEIPASS", ""), "winjit_stubs.c")
else:
    _WINJIT_STUB_C = os.path.join(os.path.dirname(__file__), "winjit_stubs.c")

_llvm_cc_cache = None

# llvmlite's binding is not thread-safe: parse_assembly / link_modules /
# create_pass_builder / optimize all touch the same global LLVM context.
# The 3-way parallel C-runtime compilation keeps the (expensive, independent)
# clang subprocesses on the worker threads, but every in-process binding call
# is serialized behind this lock so cold-cache warm-up cannot race the JIT's
# own parse/link while it links the user module.
_llvm_binding_lock = threading.RLock()


def _host_default_pic():
    """Return True when the host linker requires PIC (PIE) by default.

    Both macOS and Linux on AArch64 link position-independent executables by
    default, so non-PIC objects (with UABS relocations) are rejected at link
    time. x86-64 keeps the historical non-PIC default.
    """
    import platform

    return platform.machine().lower() in ("arm64", "aarch64")


def _use_windows_gnu():
    """True on Windows when the MSVC dev environment is not available.

    The prebuilt llvmlite wheels default to an ``x86_64-pc-windows-msvc``
    triple which requires the MSVC headers/libs. On machines that only have a
    Unix-flavoured toolchain (MinGW-w64, msys2, Cygwin, WSL-driverless clang)
    the compiler instead targets ``x86_64-w64-windows-gnu`` so the C runtime
    and libc resolve through MinGW without an MSVC install.
    """
    if os.name != "nt":
        return False
    if os.environ.get("WindowsSdkDir") or os.environ.get("VCToolsInstallDir"):
        return False
    return True


def host_target():
    """Return the :class:`llvmlite.binding.Target` for the host program.

    Uses llvmlite's default triple, except on Windows without a detected MSVC
    toolchain where the MinGW GNU triple is used so that (a) the C runtime can
    be compiled by clang against MinGW headers, and (b) AOT object emission is
    COFF (linkable by the MinGW linker) instead of an unlinkable msvc-ELF.
    """
    from llvmlite import binding

    binding.initialize_native_target()
    binding.initialize_native_asmprinter()
    if _use_windows_gnu():
        return binding.Target.from_triple("x86_64-w64-windows-gnu")
    return binding.Target.from_default_triple()


# clang (GNU/MinGW target) emits ``call __main`` at the entry of every compiled
# C function and ``call ___chkstk_ms`` when a frame needs more than 4 KiB of
# stack. Both are CRT-lib helpers (supplied by libmingw32/crtbegin in real AOT
# binaries) that no Windows DLL exports, so MCJIT resolves them to address 0
# and the JIT'd program segfaults on the first call. Define no-op / probing
# equivalents in IR so the JIT'd module is self-contained.
_JIT_WINDOWS_GNU_STUBS_IR = """\
define void @"__main"() {
  ret void
}

define void @"___chkstk_ms"() {
  ; Called with RAX = frame size N (entered as `call ___chkstk_ms; sub rsp, rax`).
  ; Touch every page down to (entry rsp - N) so the guard page is hit before
  ; any deep write lands past it, then return with RAX intact.
  call void asm sideeffect "
    push %rax
    lea 16(%rsp), %rcx
    sub %rax, %rcx
  chtkloop:
    cmp %rcx, %rsp
    jbe chtkdone
    sub $$4096, %rsp
    movl $$0, (%rsp)
    jmp chtkloop
  chtkdone:
    pop %rax
  ", "~{rcx},~{memory},~{cc},~{rsp},~{rax}"()
  ret void
}
"""


def _link_windows_gnu_jit_stubs(mod):
    """Link the MinGW-CRT JIT stubs into ``mod`` when the host needs them."""
    if not _use_windows_gnu():
        return
    from llvmlite import binding

    binding.initialize_native_asmparser()
    stub_mod = binding.parse_assembly(_JIT_WINDOWS_GNU_STUBS_IR)
    binding.link_modules(mod, stub_mod)


def _windows_imp_mappings():
    """Return ``{import_thunk_name: address}`` for every DLL export this
    process can see.

    MinGW objects call imported DLL functions through ``__imp_X`` *data*
    symbols (PE IAT thunks).  MCJIT cannot back those with addresses by
    itself, so the engine is told where each one lives.
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    kernel32.GetProcAddress.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("Psapi.dll")
    psapi.EnumProcessModulesEx.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_ulong,
    ]
    psapi.EnumProcessModulesEx.restype = ctypes.c_long
    psapi.GetModuleBaseNameA.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
    ]
    psapi.GetModuleBaseNameA.restype = ctypes.c_ulong
    proc = kernel32.GetCurrentProcess()

    imports = {}

    def add_module_imports(handle):
        b = ctypes.create_string_buffer(260)
        psapi.GetModuleBaseNameA(proc, handle, b, 260)
        mname = b.value.decode(errors="replace")
        dll = None
        if mname.lower() in ("kernel32.dll", "kernelbase.dll"):
            dll = ("kernel32", "kernelbase")
        elif mname.lower() in ("ucrtbase.dll", "msvcrt.dll", "ntdll.dll"):
            dll = (mname.lower(),)
        if dll is None:
            return
        for d in dll:
            try:
                h = ctypes.WinDLL(d, use_last_error=True)._handle
            except OSError:
                continue
            for sym in _IMPORT_CANDIDATES:
                if sym in imports:
                    continue
                addr = kernel32.GetProcAddress(ctypes.c_void_p(h), sym.encode())
                if addr:
                    imports[sym] = addr

    hmods = (ctypes.c_void_p * 2048)()
    cb = ctypes.c_ulong(0)
    psapi.EnumProcessModulesEx(proc, hmods, ctypes.sizeof(hmods), ctypes.byref(cb), 3)
    n = cb.value // ctypes.sizeof(ctypes.c_void_p)
    for i in range(n):
        add_module_imports(hmods[i])
    return imports


# The DLL entry points the MinGW-compiled cpyte C runtime reaches through PE
# IAT thunks.  Extend this list if a new runtime call starts faulting.
_IMPORT_CANDIDATES = (
    "__acrt_iob_func",
    "EnterCriticalSection",
    "LeaveCriticalSection",
    "InitializeCriticalSection",
    "GetCurrentThreadStackLimits",
    "_beginthreadex",
    "Sleep",
    "WaitForSingleObject",
    "CloseHandle",
)


def _map_windows_gnu_imp_globals(engine, mod):
    """Give the JIT engine real addresses for ``__imp_*`` IAT thunks."""
    if not _use_windows_gnu():
        return
    imports = _windows_imp_mappings()
    for gv in mod.global_variables:
        name = gv.name
        if not name.startswith("__imp_"):
            continue
        sym = name[len("__imp_") :]
        if sym not in imports:
            continue
        try:
            engine.add_global_mapping(gv, imports[sym])
        except Exception:
            pass


_WINDOWS_EXPORT_DLLS = (
    "ucrtbase",
    "msvcrt",
    "kernel32",
    "kernelbase",
    "ntdll",
)


def _windows_resolve_exports(names, cache=None):
    """Resolve an arbitrary set of symbol names to addresses in the standard
    Windows DLLs, in priority order ``ucrtbase > msvcrt > kernel32 >
    kernelbase > ntdll``.

    ``cache`` (``dict``) optionally persists resolved names across calls. The
    returned mapping contains only names that were found.
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    kernel32.GetProcAddress.restype = ctypes.c_void_p

    if cache is None:
        cache = {}
    handles = []
    for d in _WINDOWS_EXPORT_DLLS:
        h = cache["_handles"].get(d)
        if h is None:
            try:
                h = ctypes.WinDLL(d, use_last_error=True)._handle
            except OSError:
                h = 0
            cache["_handles"][d] = h
        if h:
            handles.append(h)

    resolved = {}
    todo = [n for n in names if n not in cache]
    for h in handles:
        if not todo:
            break
        nxt = []
        for sym in todo:
            try:
                addr = kernel32.GetProcAddress(ctypes.c_void_p(h), sym.encode())
            except Exception:
                addr = 0
            if addr:
                cache[sym] = addr
            else:
                nxt.append(sym)
        todo = nxt
    for n in names:
        if n in cache:
            resolved[n] = cache[n]
    # `snprintf` has no export in ucrtbase/msvcrt (only `_snprintf`), yet the
    # runtime and user code call it through clib.py; back it with `_snprintf`.
    if "snprintf" in names and "snprintf" not in cache:
        for alias in ("_snprintf", "vsnprintf"):
            for h in handles:
                try:
                    addr = kernel32.GetProcAddress(ctypes.c_void_p(h), alias.encode())
                except Exception:
                    addr = 0
                if addr:
                    cache["snprintf"] = addr
                    resolved["snprintf"] = addr
                    break
            if "snprintf" in cache:
                break
    return resolved


def _map_windows_gnu_externals(engine, mod):
    """Give the JIT engine explicit addresses for every external symbol the
    merged module references.

    On Windows the large-model ELF codegen emitted by the ``-elf`` JIT target
    reaches DLL functions through absolute ``movabs`` addresses.  MCJIT's
    on-demand process search for those symbols is unreliable (members have
    been observed resolving to 0), so every external function/global is mapped
    to its real ``GetProcAddress`` address up front — the same technique numba
    uses for its runtime symbols.
    """
    if not _use_windows_gnu():
        return
    cache = {}
    externs = set()
    try:
        for f in mod.functions:
            if len(f.blocks) == 0 and not f.name.startswith("llvm."):
                externs.add(f.name)
        for gv in mod.global_variables:
            if gv.name.startswith("__imp_"):
                externs.add(gv.name[len("__imp_") :])
    except Exception:
        return
    if not externs:
        return
    addrs = _windows_resolve_exports(sorted(externs), cache=cache)
    # Process-global symbol registration.  MCJIT's runtime symbol search is
    # unreliable on this platform, so make every external resolvable through
    # LLVM's symbol table before the object is finalized.
    try:
        from llvmlite import binding as _lb

        for name, addr in addrs.items():
            try:
                _lb.add_symbol(name, addr)
            except Exception:
                pass
    except Exception:
        pass
    if engine is None:
        return
    for f in mod.functions:
        if len(f.blocks) == 0 and not f.name.startswith("llvm.") and f.name in addrs:
            try:
                engine.add_global_mapping(f, addrs[f.name])
            except Exception:
                pass
    for gv in mod.global_variables:
        if gv.name.startswith("__imp_"):
            sym = gv.name[len("__imp_") :]
            if sym in addrs:
                try:
                    engine.add_global_mapping(gv, addrs[sym])
                except Exception:
                    pass


# Optional global CPU / target-features override, set via ``--cpu``/``--mattr``
# or programmatically. When both are None the host's native CPU and SIMD
# features are detected and used.
_GLOBAL_CPU = None
_GLOBAL_FEATURES = None


def set_target_cpu(cpu=None, features=None):
    """Override the target CPU / LLVM target-features for all JIT/AOT builds."""
    global _GLOBAL_CPU, _GLOBAL_FEATURES
    _GLOBAL_CPU = cpu
    _GLOBAL_FEATURES = features


def host_cpu_features():
    """Return a ``(cpu, features)`` tuple describing the native host.

    ``cpu`` is ``llvmlite.binding.get_host_cpu_name()`` (e.g. ``apple-m1``,
    ``skylake``); ``features`` is the comma-joined ``+feat,-feat`` string that
    enables the widest SIMD the host supports (SSE/AVX/AVX2/AVX-512 on x86,
    NEON/SVE on AArch64), or ``""`` when undetectable.
    """
    from llvmlite import binding

    cpu = ""
    features = ""
    if hasattr(binding, "get_host_cpu_name"):
        try:
            cpu = binding.get_host_cpu_name() or ""
        except Exception:
            cpu = ""
    if hasattr(binding, "get_host_cpu_features"):
        try:
            fm = binding.get_host_cpu_features()
            features = fm.flatten() if fm else ""
        except Exception:
            features = ""
    return cpu, features


def make_target_machine(pic=False, cpu=None, features=None, codemodel=None):
    """Build a :class:`llvmlite.binding.TargetMachine` for the host (or an
    explicit cpu/features override) with SIMD enabled.

    Passing the host's native CPU and feature set lets LLVM's auto-vectorizer
    (loop/SLP vectorization, run by :func:`optimize` at ``-O2``/``-O3``) emit the
    widest available SIMD — SSE/AVX/AVX2 on x86-64, NEON/SVE on AArch64 — instead
    of the conservative baseline the ``from_default_triple()`` target machine
    produces. ``pic`` selects the relocation model (PIE is required on modern
    AArch64 linkers).

    ``codemodel`` (``"small"``/``"large"``/``"default"``/``"jitdefault"``)
    overrides the LLVM code model. On Windows, ``jitdefault`` (the default)
    forces ELF objects for MCJIT; AOT output must use a non-jit code model so
    the emitted object is COFF and can be linked with the native toolchain.
    """
    from llvmlite import binding

    binding.initialize_native_target()
    binding.initialize_native_asmprinter()

    if cpu is None:
        cpu = _GLOBAL_CPU
    if features is None:
        features = _GLOBAL_FEATURES
    if cpu is None and features is None:
        host_cpu, host_feats = host_cpu_features()
        cpu = host_cpu
        features = host_feats

    target = host_target()
    kwargs = {}
    if pic:
        kwargs["reloc"] = "pic"
    if cpu:
        kwargs["cpu"] = cpu
    if features:
        kwargs["features"] = features
    if codemodel:
        kwargs["codemodel"] = codemodel
    return target.create_target_machine(**kwargs)


def _find_llvm_cc():
    """Find a C compiler that supports -emit-llvm for JIT compilation.

    Tries clang first, then falls back to other compilers on PATH.
    Returns the compiler path or raises SystemExit with a clear message.
    """
    global _llvm_cc_cache
    if _llvm_cc_cache is not None:
        return _llvm_cc_cache
    import shutil

    for name in ("clang", "cc", "gcc"):
        exe = shutil.which(name)
        if exe:
            try:
                r = subprocess.run(
                    [exe, "-S", "-emit-llvm", "-O0", "-o", "-", "-xc", "-"],
                    input="int __x = 0;",
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if r.returncode == 0:
                    _llvm_cc_cache = exe
                    return exe
            except (OSError, subprocess.TimeoutExpired):
                continue
    print_err(
        "error: no C compiler found that supports -emit-llvm (needed for JIT).\n"
        "  Install clang, or use --aot mode which works with gcc.\n"
        "  On macOS: xcode-select --install\n"
        "  On Ubuntu/Debian: sudo apt install clang\n"
        "  On Fedora/RHEL: sudo dnf install clang"
    )
    raise SystemExit(1)


_callbacks: list = []


# Symbols the JIT/runtime resolves from libc. On POSIX they come from libc.so
# / libSystem; on Windows from the C runtime (UCRT/msvcrt). Verified on load so
# a half-loaded CRT (e.g. ucrtbase.dll lacking malloc) is skipped.
_LIBC_REQUIRED = ("malloc", "calloc", "realloc", "free", "strlen", "memcpy")


def _load_libc():
    """Open the platform C runtime for JIT symbol resolution.

    On macOS, tools like MallocStackLogging may interpose malloc/free with a
    private arena allocator whose pointers the interposed free() rejects
    ("pointer being freed was not allocated"). Using RTLD_FIRST forces dlsym
    to resolve to the real libSystem symbols so JIT'd malloc/free/realloc/
    calloc stay self-consistent.

    On Windows, ``ctypes.util.find_library("c")`` returns ``None`` (there is
    no ELF-style SONAME to discover), so the CRT must be opened explicitly
    instead of passing ``None`` to CDLL (which raises TypeError on LoadLibrary).
    """
    if sys.platform == "darwin":
        RTLD_FIRST = 0x100
        try:
            return ctypes.CDLL(
                "/usr/lib/libSystem.B.dylib",
                mode=os.RTLD_NOW | RTLD_FIRST,
            )
        except OSError:
            pass
    if os.name == "nt":
        for name in ("msvcrt", "ucrtbase", "api-ms-win-crt-runtime-l1-1-0"):
            try:
                lib = ctypes.CDLL(name)
                for sym in _LIBC_REQUIRED:
                    getattr(lib, sym)
                return lib
            except (OSError, AttributeError):
                continue
        raise OSError("cpyte: unable to load a Windows C runtime")
    name = ctypes.util.find_library("c")
    if not name:
        # find_library may return None on some libc/distros; fall back to the
        # conventional SONAMEs rather than passing None to CDLL.
        for cand in ("libc.so.6", "libc.so"):
            try:
                return ctypes.CDLL(cand)
            except OSError:
                continue
        raise OSError("cpyte: unable to locate the C standard library")
    return ctypes.CDLL(name)


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


def _runtime_print_hex(n: int):
    if n < 0:
        n = n & ((1 << 64) - 1)
    print(f"0x{n:x}")


def _runtime_print_double(d: float):
    print(f"{d:.6f}")


def _runtime_print_str(s: bytes):
    if s is None:
        print("(null)")
    else:
        try:
            print(s.decode("utf-8"))
        except UnicodeDecodeError:
            print(repr(s))


def _runtime_cstr(s: str) -> int:
    """Copy a Python string into a libc heap buffer, returning its address."""
    raw = s.encode("utf-8")
    size = len(raw) + 1
    buf = ctypes.create_string_buffer(raw + b"\0")
    ptr = _libc.malloc(size)
    if not ptr:
        return 0
    ctypes.memmove(ptr, buf, size)
    return ctypes.cast(ptr, ctypes.c_void_p).value or 0


def _runtime_str_of_int64(n: int) -> int:
    return _runtime_cstr(str(n))


def _runtime_str_of_uint64(n: int) -> int:
    if n < 0:
        n = n & ((1 << 64) - 1)
    return _runtime_cstr(str(n))


def _runtime_str_of_ptr(n: int) -> int:
    if n < 0:
        n = n & ((1 << 64) - 1)
    return _runtime_cstr(f"0x{n:x}")


def _runtime_str_of_double(d: float) -> int:
    return _runtime_cstr(f"{d:.6f}")


def _runtime_input() -> int:
    return int(input())


def _runtime_input_str() -> bytes:
    return input().encode("utf-8")


# Array length registry (mirrors runtime.c side table).
_arr_registry: dict[int, int] = {}


def _runtime_array_register(ptr: int, n: int):
    _arr_registry[ptr] = n


def _runtime_array_len(ptr: int) -> int:
    return _arr_registry.get(ptr, 0)


def _runtime_array_unregister(ptr: int):
    _arr_registry.pop(ptr, None)


# Dynamic variable runtime (name-keyed tagged values).
(
    _DYN_NONE,
    _DYN_INT,
    _DYN_INT64,
    _DYN_UINT64,
    _DYN_CHAR,
    _DYN_BOOL,
    _DYN_DOUBLE,
    _DYN_STR,
    _DYN_BIG,
    _DYN_PTR,
) = range(10)

_dyn_table: dict[bytes, tuple[int, int]] = {}


def _dyn_is_numeric(kind: int) -> bool:
    return kind in (
        _DYN_INT,
        _DYN_INT64,
        _DYN_UINT64,
        _DYN_CHAR,
        _DYN_BOOL,
        _DYN_DOUBLE,
    )


def _dyn_bits_to_double(bits: int) -> float:
    return struct.unpack("<d", struct.pack("<Q", bits))[0]


def _dyn_double_to_bits(v: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", v))[0]


def _runtime_assign(name: bytes, kind: int, data: int):
    _dyn_table[name] = (kind, data & ((1 << 64) - 1))


def _runtime_dyn_as(name: bytes, want_kind: int) -> int:
    entry = _dyn_table.get(name)
    if entry is None or entry[0] == _DYN_NONE:
        print(f"dynamic variable '{name}' has no value", file=sys.stderr)
        os.abort()
    k, data = entry
    if k == want_kind:
        return data
    if _dyn_is_numeric(k) and _dyn_is_numeric(want_kind):
        if k != _DYN_DOUBLE and want_kind != _DYN_DOUBLE:
            return data
        if k == _DYN_DOUBLE:
            v = _dyn_bits_to_double(data)
            return ctypes.c_int64(int(v)).value & ((1 << 64) - 1)
        if k == _DYN_UINT64:
            v = float(data)
        else:
            v = float(ctypes.c_int64(data).value)
        return _dyn_double_to_bits(v)
    if (k in (_DYN_STR, _DYN_PTR)) and (want_kind in (_DYN_STR, _DYN_PTR)):
        return data
    print(
        f"dynamic variable '{name}' type mismatch ({k} -> {want_kind})", file=sys.stderr
    )
    os.abort()


# Bigint helpers for the dynamic runtime, resolved lazily after JIT finalize.
_dyn_bigint_print = [None]
_dyn_bigint_to_str = [None]


def _runtime_dyn_truthy(name: bytes) -> int:
    entry = _dyn_table.get(name)
    if entry is None or entry[0] == _DYN_NONE:
        return 0
    k, data = entry
    if k == _DYN_DOUBLE:
        return 1 if _dyn_bits_to_double(data) != 0.0 else 0
    return 1 if data != 0 else 0


def _runtime_dyn_print(name: bytes):
    entry = _dyn_table.get(name)
    if entry is None or entry[0] == _DYN_NONE:
        print(0)
        return
    k, data = entry
    if k == _DYN_INT:
        print(ctypes.c_int32(data).value)
    elif k == _DYN_INT64:
        print(ctypes.c_int64(data).value)
    elif k == _DYN_UINT64:
        print(data)
    elif k == _DYN_CHAR:
        print(ctypes.c_int8(data & 0xFF).value)
    elif k == _DYN_BOOL:
        print(1 if data else 0)
    elif k == _DYN_DOUBLE:
        print(f"{_dyn_bits_to_double(data):.6f}")
    elif k == _DYN_STR:
        if data == 0:
            print("(null)")
        else:
            try:
                print(ctypes.string_at(data).decode("utf-8"))
            except UnicodeDecodeError:
                print(repr(ctypes.string_at(data)))
    elif k == _DYN_BIG:
        cb = _dyn_bigint_print[0]
        if cb is not None:
            cb(data)
        else:
            print(data)
    elif k == _DYN_PTR:
        print(f"0x{data:x}")
    else:
        print(0)


def _runtime_dyn_str(name: bytes) -> int:
    entry = _dyn_table.get(name)
    if entry is None or entry[0] == _DYN_NONE:
        return _runtime_cstr("0")
    k, data = entry
    if k == _DYN_INT:
        return _runtime_cstr(str(ctypes.c_int32(data).value))
    if k == _DYN_INT64:
        return _runtime_cstr(str(ctypes.c_int64(data).value))
    if k == _DYN_UINT64:
        return _runtime_cstr(str(data))
    if k == _DYN_CHAR:
        return _runtime_cstr(str(ctypes.c_int8(data & 0xFF).value))
    if k == _DYN_BOOL:
        return _runtime_cstr("1" if data else "0")
    if k == _DYN_DOUBLE:
        return _runtime_cstr(f"{_dyn_bits_to_double(data):.6f}")
    if k == _DYN_STR:
        if data == 0:
            return _runtime_cstr("")
        return _runtime_cstr(ctypes.string_at(data).decode("utf-8", "replace"))
    if k == _DYN_BIG:
        cb = _dyn_bigint_to_str[0]
        if cb is not None:
            return cb(data)
        return _runtime_cstr(str(data))
    if k == _DYN_PTR:
        return _runtime_cstr(f"0x{data:x}")
    return _runtime_cstr("0")


_bigint_from_str_cb = None


def _runtime_input_big():
    try:
        line = input()
    except EOFError:
        line = ""
    return _bigint_from_str_cb(line.strip().encode("utf-8"))


def optimize(mod, opt_level=3, opt_size=False):
    from llvmlite import binding

    binding.initialize_native_target()
    binding.initialize_native_asmprinter()
    if opt_level <= 0 and not opt_size:
        return
    target_machine = make_target_machine()

    if opt_size:
        # -OSize: optimize purely for code size, completely ignoring speed.
        # Cap at O2 so the heavy O3/O4 speed passes never run, and disable every
        # size-bloating transformation (loop unrolling/vectorization, high
        # inlining). The default module pipeline then shrinks code.
        effective = min(opt_level, 2)
        pto = binding.create_pipeline_tuning_options(speed_level=0)
        pto.inlining_threshold = 25
        pto.loop_unrolling = False
        pto.loop_vectorization = False
        pto.slp_vectorization = False
        pto.loop_interleaving = False
    else:
        effective = opt_level
        pto = binding.create_pipeline_tuning_options(speed_level=min(opt_level, 3))

    heavy = effective >= 3
    extra_heavy = effective >= 4

    if effective >= 2:
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
    if effective >= 2:
        fpm = pb.getFunctionPassManager()
        fpm.add_simplify_cfg_pass()
        fpm.add_sroa_pass()
        fpm.add_instruction_combine_pass()
        if heavy:
            fpm.add_new_gvn_pass()  # global value numbering
            fpm.add_instruction_combine_pass()
            fpm.add_sccp_pass()  # sparse conditional constant propagation
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
        elif opt_size:
            # Size-friendly subset: fold constant branches, prune dead code and
            # tail-call-eliminate, but never unroll or vectorize loops.
            fpm.add_instruction_combine_pass()
            fpm.add_sccp_pass()
            fpm.add_jump_threading_pass()
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
    elif opt_size:
        # Size-oriented module phase: coalesce constants, eliminate dead globals
        # and arguments, merge identical function bodies, drop unused prototypes.
        mpm = pb.getModulePassManager()
        mpm.add_constant_merge_pass()
        mpm.add_global_dead_code_eliminate_pass()
        mpm.add_dead_arg_elimination_pass()
        mpm.add_merge_functions_pass()
        mpm.add_strip_dead_prototype_pass()
        mpm.run(mod, pb)

    # Default module pipeline (includes inliner, GVN, DCE, loop and vectorization opts)
    npm = pb.getModulePassManager()

    # Let extension hooks add their own passes
    try:
        from .extension_hooks import (
            CompilerContext,
            HookStage,
            OptimizeHook,
            get_global_hook_registry,
        )

        registry = get_global_hook_registry()
        ctx = CompilerContext(optimization_level=opt_level, llvm_module=mod)
        for hook in registry.get(HookStage.OPTIMIZE):
            if not isinstance(hook, OptimizeHook):
                continue
            try:
                if hook.should_add_passes(ctx):
                    hook.add_module_passes(npm, ctx)
            except Exception as e:
                print_err(f"optimize hook {hook.__class__.__name__} failed: {e}")
    except Exception:
        pass

    npm.run(mod, pb)


def _maybe_compile(module, use_native_eh=False):
    if isinstance(module, list):
        from .bytecoding import LLVM

        c = LLVM(use_native_eh=use_native_eh)
        prog, src_files = c.emit_program(module)
        return prog, src_files
    return module, None


# Bignum symbols that must keep external linkage: the JIT resolves these by
# name after finalize (bigint_from_str/bigint_print/bigint_to_str) and the
# native AOT link of runtime.o needs bigint_from_int (runtime.c: bigint_input).
_BIGNUM_KEEP = frozenset(
    {"bigint_from_str", "bigint_from_int", "bigint_print", "bigint_to_str"}
)


def _mark_internal(mod, keep=frozenset()):
    """Switch every defined function's linkage to internal (except names in
    `keep`) before linking. The LLVM IR linker only carries internal globals
    into the combined module when something references them, so marking the
    runtime sources internal gives 'link only what's needed' semantics and lets
    a later GlobalDCE prune runtimes the current program never touches."""
    n = 0
    for fn in mod.functions:
        if fn.is_declaration or fn.name in keep:
            continue
        if str(fn.linkage) not in ("internal", "private", "linkonce_odr"):
            fn.linkage = "internal"
            n += 1
    return n


def _prune_module(mod):
    """Cheap pre-optimization cleanup: drop unreferenced internal functions and
    unused prototypes so the per-function pass phase and object emission never
    see dead runtime code."""
    from llvmlite import binding

    pto = binding.create_pipeline_tuning_options(speed_level=0)
    pb = binding.create_pass_builder(make_target_machine(), pto)
    mpm = pb.getModulePassManager()
    mpm.add_global_dead_code_eliminate_pass()
    mpm.add_strip_dead_prototype_pass()
    mpm.run(mod, pb)


def _cached_c_ir(
    llvm_cc,
    src_path,
    triple,
    opt="-O0",
    extra_flags=(),
    jit_opt_level=None,
    jit_opt_size=False,
):
    """Compile a C runtime source to LLVM IR text, caching by content.

    The three runtime files (runtime.c/bignum.c/gc_runtime.c and the Windows
    GNU JIT stubs) are recompiled by clang on every JIT run, which dominates
    startup cost; content-addressed caching makes repeat runs skip clang.

    When *jit_opt_level* is given (>0), a second cache entry is maintained
    containing the module already run through :func:`optimize` at that level.
    The JIT links these pre-optimized runtime modules so the expensive LLVM
    pass pipeline runs once per runtime *content* instead of once per program.
    """
    import hashlib
    import tempfile

    with open(src_path, "rb") as f:
        content = f.read()
    key = hashlib.sha1(
        content
        + triple.encode()
        + opt.encode()
        + b"".join(map(str.encode, extra_flags))
    ).hexdigest()

    cache_dir = os.path.join(tempfile.gettempdir(), "cpyte_rtcache")
    cache_path = os.path.join(cache_dir, key + ".ll")
    if jit_opt_level is not None and jit_opt_level > 0:
        opt_suffix = "opt%do%d" % (jit_opt_level, 1 if jit_opt_size else 0)
        cache_path = os.path.join(cache_dir, key + "." + opt_suffix + ".ll")
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "r", encoding="utf-8", errors="replace") as f:
            cached = f.read()
        if cached and cached.lstrip().startswith(";"):
            return cached, None
    except OSError:
        pass

    r = subprocess.run(
        [
            llvm_cc,
            "-S",
            "-emit-llvm",
            opt,
            *extra_flags,
            "-target",
            triple,
            "-fno-stack-protector",
            "-o",
            "-",
            src_path,
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None, r
    ir_text = r.stdout
    if ir_text and ir_text.lstrip().startswith(";"):
        if jit_opt_level is not None and jit_opt_level > 0:
            # Warm the optimized cache entry in a background-safe way: optimize
            # locally and atomically publish (tmp + os.replace). The llvmlite
            # binding section is serialized (not thread-safe), while the clang
            # subprocess above stays parallel across cores.
            try:
                from llvmlite import binding as _binding

                with _llvm_binding_lock:
                    _binding.initialize_native_target()
                    _binding.initialize_native_asmprinter()
                    opt_mod = _binding.parse_assembly(_remove_probe_stack_ir(ir_text))
                    optimize(opt_mod, jit_opt_level, opt_size=jit_opt_size)
                    opt_text = str(opt_mod)
                if opt_text.lstrip().startswith(";"):
                    tmp_path = cache_path + ".tmp"
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        f.write(opt_text)
                    os.replace(tmp_path, cache_path)
                return opt_text, None
            except Exception:
                pass
        try:
            tmp_path = cache_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(ir_text)
            os.replace(tmp_path, cache_path)
        except OSError:
            pass
    return ir_text, None


def run_jit(
    module,
    opt_level=3,
    src_files=None,
    no_userspace=False,
    pic=False,
    use_native_eh=False,
):
    module, src_files_auto = _maybe_compile(module, use_native_eh=use_native_eh)
    if src_files_auto is not None:
        src_files = src_files_auto
    global _print_fn, _input_fn
    from llvmlite import binding

    binding.initialize_native_target()
    binding.initialize_native_asmprinter()

    llvm_ir = str(module)
    with _llvm_binding_lock:
        mod = binding.parse_assembly(llvm_ir)
    llvm_cc = _find_llvm_cc()
    if src_files:
        # Compile every ccode module in parallel across all cores; the clang
        # subprocesses dominate wall time and are independent, while the
        # llvmlite parse_assembly/link_modules below stay serial on the main
        # thread (the binding is not thread-safe).
        target = host_target()

        def _compile_one(src):
            if src.endswith(".ll"):
                with open(src) as f:
                    return src, f.read(), None
            r = subprocess.run(
                [
                    llvm_cc,
                    "-S",
                    "-emit-llvm",
                    "-O0",
                    "-target",
                    target.triple,
                    "-fno-stack-protector",
                    "-o",
                    "-",
                    src,
                ],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                return src, None, r
            return src, r.stdout, None

        if len(src_files) > 1:
            with ThreadPoolExecutor(max_workers=max(2, os.cpu_count() or 2)) as ex:
                compiled = list(ex.map(_compile_one, src_files))
        else:
            compiled = [_compile_one(src_files[0])]
        for src, src_ir, r in compiled:
            if src_ir is None:
                ccmap = getattr(module, "_ccode_src_map", None) or {}
                where = ccmap.get(src)
                if where:
                    print_err(
                        f"error compiling ccode block from {where}: "
                        f"{format_cc_diag(r.stderr)}"
                    )
                else:
                    print_err(f"error compiling {src}: {format_cc_diag(r.stderr)}")
                raise SystemExit(1)
            src_ir = _remove_probe_stack_ir(src_ir)
            with _llvm_binding_lock:
                src_mod = binding.parse_assembly(src_ir)
                binding.link_modules(mod, src_mod)

    # Optimize the USER code now, before any runtime is linked. The C runtime
    # modules below are pulled from a content-addressed cache already run
    # through optimize() (see _cached_c_ir), so the heavy LLVM pass pipeline
    # executes once per runtime content instead of once per program. Doing the
    # user optimization here keeps every cpyte-specific pass (SROA, unrolling,
    # GVN, vectorization) applied to exactly the functions the user wrote; the
    # final whole-module re-optimization over the remaining runtime helpers is
    # then skipped.
    if opt_level > 0:
        optimize(mod, opt_level)

    # Link the C runtime (print/assign/dynamic dispatch/array registry) so the
    # JIT can resolve native helpers that have no Python callback mirror.
    target = host_target()

    # Compile runtime/bignum/gc IR from source; the round-trips are cached
    # per source-content/triple so repeat launches skip clang entirely, and the
    # (cold) compiles run in parallel. bignum/gc functions are marked internal
    # first so the IR linker only pulls in what this program references.
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(
                _cached_c_ir, llvm_cc, src, target.triple, jit_opt_level=opt_level
            ): src
            for src in (_RUNTIME_C, _BIGNUM_C, _GC_RUNTIME_C)
        }
        runtime_irs = {}
        for fut, src in futures.items():
            ir_text, err = fut.result()
            if ir_text is None:
                print_err(f"error compiling {src}: {format_cc_diag(err.stderr)}")
                raise SystemExit(1)
            runtime_irs[src] = ir_text

    internal_keep = {
        _RUNTIME_C: "external",
        _BIGNUM_C: _BIGNUM_KEEP,
        _GC_RUNTIME_C: frozenset(),
    }
    for src in (_RUNTIME_C, _BIGNUM_C, _GC_RUNTIME_C):
        with _llvm_binding_lock:
            m = binding.parse_assembly(_remove_probe_stack_ir(runtime_irs[src]))
            keep = internal_keep[src]
            if keep == "external":
                binding.link_modules(mod, m)
                continue
            src_names = {f.name for f in m.functions if not f.is_declaration}
            binding.link_modules(mod, m)
        # Mark internal AFTER linking: the LLVM IR linker treats an internal
        # source definition plus a same-named external declaration already in
        # the destination as a conflict and DROPS the definition (leaving the
        # user's call sites unresolved -> a NULL call in the JIT object).
        # Linking first, then internalizing, keeps every needed definition in
        # the combined module; the later _prune_module GlobalDCE removes the
        # unreferenced leftover runtime code instead.
        for fn in mod.functions:
            if (
                fn.name in src_names
                and fn.name not in keep
                and not fn.is_declaration
                and str(fn.linkage) not in ("internal", "private", "linkonce_odr")
            ):
                fn.linkage = "internal"

    # The GNU/MinGW lowering sprinkles `call __main` / `call ___chkstk_ms`
    # into every compiled runtime function; without prior CG libraries the
    # symbols do not exist in the RTDyld symbol table, so link IR stubs.
    _link_windows_gnu_jit_stubs(mod)

    if _use_windows_gnu():
        # ucrtbase/msvcrt leave memcpy/memset/memmove/floor/snprintf injected
        # via intrinsic libcalls, and i128 div/mod needs __udivti3/__umodti3,
        # none of which are reliably exported -- link our own definitions so
        # the emitted object has no undefined references to them.
        winjit_ir, err = _cached_c_ir(
            llvm_cc, _WINJIT_STUB_C, target.triple, extra_flags=("-fno-builtin",)
        )
        if winjit_ir is None:
            print_err(f"error compiling {_WINJIT_STUB_C}: {format_cc_diag(err.stderr)}")
            raise SystemExit(1)
        winjit_mod = binding.parse_assembly(_remove_probe_stack_ir(winjit_ir))
        binding.link_modules(mod, winjit_mod)

    mod.verify()
    _prune_module(mod)
    mod.verify()

    if _use_windows_gnu():
        link_jit_anchors(mod, host_target().triple)
        mod.verify()

    target_machine = make_target_machine()

    with _llvm_binding_lock:
        backing_mod = binding.parse_assembly("")
        engine = binding.create_mcjit_compiler(backing_mod, target_machine)
        engine.add_module(mod)

    # MinGW-compiled C runtime reaches DLL functions through absolute
    # large-model relocs (and __imp_* IAT thunks); hand the engine explicit
    # addresses so MCJIT never leaves an external symbol at NULL.
    _map_windows_gnu_externals(engine, mod)

    if not no_userspace and not _use_windows_gnu():
        # On the MinGW GNU JIT path these symbols are full native definitions
        # in runtime.c (print_*, dyn_*, assign, str_of_*, input_int) and
        # gc_runtime.c/runtime.c (cpyte_array_*); overriding them with libffi
        # closures crashes at runtime on Windows, so let the C implementations
        # (which now route through the winjit print/snprintf shims) serve.
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

        cb = ctypes.CFUNCTYPE(None, ctypes.c_ulonglong)(_runtime_print_hex)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("print_hex"),
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

        cb = ctypes.CFUNCTYPE(ctypes.c_void_p)(_runtime_input_big)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("bigint_input"),
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

        cb = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_longlong)(_runtime_str_of_int64)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("str_of_int64"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_ulonglong)(
            _runtime_str_of_uint64
        )
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("str_of_uint64"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_ulonglong)(_runtime_str_of_ptr)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("str_of_ptr"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_double)(_runtime_str_of_double)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("str_of_double"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint64)(
            _runtime_assign
        )
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("assign"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_char_p, ctypes.c_int)(
            _runtime_dyn_as
        )
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("dyn_as"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p)(_runtime_dyn_truthy)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("dyn_truthy"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(None, ctypes.c_char_p)(_runtime_dyn_print)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("dyn_print"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        cb = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p)(_runtime_dyn_str)
        _callbacks.append(cb)
        try:
            engine.add_global_mapping(
                mod.get_function("dyn_str"),
                ctypes.cast(cb, ctypes.c_void_p).value,
            )
        except NameError:
            pass

        # cpyte_array_alloc/len/unregister/reserve are provided by the C
        # runtime (length-prefixed header); no Python-side overrides needed.

    _map_libc_fn(engine, mod, "malloc", ctypes.c_size_t, ctypes.c_void_p)
    _map_libc_fn(engine, mod, "free", None, None, argtypes=[ctypes.c_void_p])
    _map_libc_fn(
        engine,
        mod,
        "realloc",
        ctypes.c_void_p,
        ctypes.c_void_p,
        argtypes=[ctypes.c_void_p, ctypes.c_size_t],
    )
    _map_libc_fn(
        engine,
        mod,
        "calloc",
        ctypes.c_size_t,
        ctypes.c_void_p,
        argtypes=[ctypes.c_size_t, ctypes.c_size_t],
    )
    _map_libc_fn(engine, mod, "strlen", ctypes.c_char_p, ctypes.c_int)
    _map_libc_fn(
        engine,
        mod,
        "memcpy",
        None,
        ctypes.c_void_p,
        argtypes=[ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int],
    )
    _map_libc_fn(engine, mod, "atoi", ctypes.c_char_p, ctypes.c_int)
    _map_libc_fn(engine, mod, "atof", ctypes.c_char_p, ctypes.c_double)

    try:
        fn = mod.get_function("strcmp")
        engine.add_global_mapping(fn, _libc_addr("strcmp"))
    except NameError:
        pass

    engine.finalize_object()
    if _use_windows_gnu():
        externs = sorted(
            {
                g.name
                for g in list(mod.functions) + list(mod.global_variables)
                if g.is_declaration
                and not g.name.startswith("llvm.")
                and g.name != "_GLOBAL_OFFSET_TABLE_"
            }
        )
        resolved = _windows_resolve_exports(externs, cache={"_handles": {}})
        if os.environ.get("CPYTE_JIT_DEBUG"):
            import sys as _sys

            missing = [n for n in externs if n not in resolved]
            print(
                f"[winjit] externs({len(externs)})={externs}",
                flush=True,
                file=_sys.stderr,
            )
            print(
                f"[winjit] missing={missing}",
                flush=True,
                file=_sys.stderr,
            )
        # Symbols only knowable through the engine (userland callbacks like
        # cpyte_array_unregister or _map_libc_fn natives) never appear in DLL
        # exports; pull their post-finalize addresses so the patcher can fix
        # their reloc immediates if MCJIT left them at 0.
        for name in externs:
            if name in resolved:
                continue
            try:
                addr = engine.get_function_address(name)
            except Exception:
                continue
            if addr:
                resolved[name] = addr
        patch_windows_gnu_relocs(engine, target_machine, mod, resolved)
    engine.run_static_constructors()

    global _bigint_from_str_cb
    _bigint_from_str_cb = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p)(
        engine.get_function_address("bigint_from_str")
    )

    try:
        _dyn_bigint_print[0] = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(
            engine.get_function_address("bigint_print")
        )
    except (NameError, RuntimeError):
        _dyn_bigint_print[0] = None
    try:
        _dyn_bigint_to_str[0] = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(
            engine.get_function_address("bigint_to_str")
        )
    except (NameError, RuntimeError):
        _dyn_bigint_to_str[0] = None

    func_ptr = engine.get_function_address("main")
    if not func_ptr:
        print_err("no `main` function defined")
        return 0
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


def run_aot(
    module,
    output="program.o",
    opt_level=3,
    src_files=None,
    no_userspace=False,
    pic=None,
    lto=False,
    frameworks=None,
):
    if pic is None:
        pic = _host_default_pic()
    llvm_ir = str(module)
    from llvmlite import binding

    binding.initialize_native_target()
    binding.initialize_native_asmprinter()

    mod = binding.parse_assembly(llvm_ir)
    # Compile the bignum runtime from source for the host platform (instead
    # of pre-built bitcode) so its libc symbol names match the target OS. The
    # clang round-trip is cached per source-content/triple, and the funcs are
    # marked internal (minus the symbols runtime.o needs) so the IR linker and
    # an early GlobalDCE only carry the bignum helpers this program uses.
    llvm_cc = _find_llvm_cc()
    bignum_ir, err = _cached_c_ir(llvm_cc, _BIGNUM_C, host_target().triple)
    if bignum_ir is None:
        print_err(f"error compiling {_BIGNUM_C}: {format_cc_diag(err.stderr)}")
        raise SystemExit(1)
    bignum_mod = binding.parse_assembly(_remove_probe_stack_ir(bignum_ir))
    _mark_internal(bignum_mod, keep=_BIGNUM_KEEP)
    binding.link_modules(mod, bignum_mod)
    mod.verify()
    _prune_module(mod)
    optimize(mod, opt_level)
    mod.verify()

    target_machine = make_target_machine(pic=pic, codemodel="small")

    obj = target_machine.emit_object(mod)

    with open(output, "wb") as f:
        f.write(obj)

    objs = [output]
    try:
        linker = Linker(lto=lto)
    except LinkerNotFoundError as e:
        print_err(f"error: {e}")
        raise SystemExit(1)

    compile_jobs = []
    for src in src_files or []:
        compile_jobs.append((src, src.rsplit(".", 1)[0] + ".o"))
    if not no_userspace:
        compile_jobs.append((_RUNTIME_C, output + ".runtime.o"))
    compile_jobs.append((_GC_RUNTIME_C, output + ".gc.o"))

    def _linker_compile(src, out):
        linker.compile_c(
            src,
            output=out,
            opt_level=3,
            pic=pic,
            eh=(not no_userspace and src == _RUNTIME_C),
        )
        return out

    if len(compile_jobs) > 1:
        with ThreadPoolExecutor(max_workers=max(2, os.cpu_count() or 2)) as ex:
            compiled_objs = list(ex.map(lambda p: _linker_compile(*p), compile_jobs))
    else:
        compiled_objs = [_linker_compile(*compile_jobs[0])]
    objs.extend(compiled_objs)

    out_name = output.rsplit(".", 1)[0] if "." in output else output
    linker.link(objs, out_name, opt_level=3, pic=pic, frameworks=frameworks)


if getattr(sys, "frozen", False):
    _RUNTIME_SCORPION_C = os.path.join(
        getattr(sys, "_MEIPASS", ""), "runtime_scorpion.c"
    )
else:
    _RUNTIME_SCORPION_C = os.path.join(os.path.dirname(__file__), "runtime_scorpion.c")

_SCORPION_CC = "riscv32-unknown-elf-gcc"
_SCORPION_AS = "riscv64-elf-as"
_SCORPION_LD = "riscv64-elf-ld"
_SCORPION_OBJCOPY = "riscv64-elf-objcopy"
_SCORPION_ARCH = "-march=rv32imac_zicsr_zifencei_zba_zbb_zbs_zbkb"
_SCORPION_ABI = "-mabi=ilp32"


def _find_scorpion_tool(name, fallback):
    """Find a scorpion cross-compilation tool."""
    import shutil

    candidates = [
        f"riscv32-unknown-elf-{name}",
        f"riscv64-unknown-elf-{name}",
        f"riscv64-elf-{name}",
    ]
    for c in candidates:
        if shutil.which(c):
            return c
    return fallback


def run_scorpion(
    module, output="program.sef", opt_level=3, src_files=None, pic=False, exports=None
):
    """Compile a Cpyte module for Scorpion (RV32 bare-metal) producing a SEF file.
    Please use it good! :):)

    With pic=True the module is compiled with -fPIC and the final ELF is linked
    with --emit-relocs so it can be converted to a dynamic (SEF v2) image via
    WEW-scorpion/tools/elf2sef.py. `exports` names the symbols to mark as
    exported for dynamic linking (libraries).
    """
    from llvmlite import binding

    binding.initialize_all_targets()
    binding.initialize_all_asmprinters()

    llvm_ir = str(module)
    mod = binding.parse_assembly(llvm_ir)

    mod.verify()
    optimize(mod, opt_level)
    mod.verify()

    target = binding.Target.from_triple("riscv32-unknown-elf")
    if pic:
        target_machine = target.create_target_machine(reloc="pic")
    else:
        target_machine = target.create_target_machine()

    # Emit RV32 object file
    obj = target_machine.emit_object(mod)
    obj_file = output.rsplit(".", 1)[0] + ".o"
    with open(obj_file, "wb") as f:
        f.write(obj)

    objs = [obj_file]

    # Compile Scorpion runtime
    runtime_obj = output.rsplit(".", 1)[0] + ".runtime.o"
    cc = _find_scorpion_tool("gcc", _SCORPION_CC)
    cmd = [
        cc,
        "-c",
        "-O3",
        _SCORPION_ARCH,
        _SCORPION_ABI,
        "-nostdlib",
        "-ffreestanding",
        "-o",
        runtime_obj,
        _RUNTIME_SCORPION_C,
    ]
    if pic:
        cmd.append("-fPIC")  # This must be done to be ran on microcontrollers.
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print_err(f"error compiling runtime_scorpion.c: {format_cc_diag(r.stderr)}")
        raise SystemExit(1)
    objs.append(runtime_obj)

    # Link into ELF using the GCC driver so libgcc (e.g. __fixdfsi) is resolved
    elf_base = output.rsplit(".", 1)[0]
    elf_file = elf_base + ".elf"
    cc = _find_scorpion_tool("gcc", _SCORPION_CC)
    cmd = (
        [
            cc,
            _SCORPION_ARCH,
            _SCORPION_ABI,
            "-nostdlib",
            "-nostartfiles",
            "-Wl,-e,main",
            "-Wl,--no-relax",
            "-Wl,-Ttext=0",
            "-o",
            elf_file,
        ]
        + objs
        + ["-lgcc"]
    )
    if pic:
        cmd += ["-Wl,-q", "-Wl,--unresolved-symbols=ignore-all"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print_err(f"error linking: {format_cc_diag(r.stderr)}")
        raise SystemExit(1)

    if pic:
        # Dynamic SEF v2: relocation + import/export records
        elf2sef = os.path.join(
            os.path.dirname(os.path.dirname(_RUNTIME_SCORPION_C)),
            "..",
            "..",
            "WEW-scorpion",
            "tools",
            "elf2sef.py",
        )
        cmd = [sys.executable, elf2sef, elf_file, output]
        if exports:
            for name in exports:
                cmd += ["--export", name]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print_err(f"error converting to SEF: {format_cc_diag(r.stderr)}")
            raise SystemExit(1)
    else:
        # Static SEF v1 via mksef.py
        mksef = os.path.join(
            os.path.dirname(os.path.dirname(_RUNTIME_SCORPION_C)),
            "..",
            "..",
            "WEW-scorpion",
            "user",
            "mksef.py",
        )
        if not os.path.isfile(mksef):
            # Fallback: inline SEF generation using objdump/objcopy
            _elf_to_sef(elf_file, output, 0)
        else:
            r = subprocess.run(
                [sys.executable, mksef, elf_file, output],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                print_err(f"error converting to SEF: {format_cc_diag(r.stderr)}")
                raise SystemExit(1)

    print_ok(f"Wrote {os.path.getsize(output)} bytes to {output}")
    return elf_file


def _elf_to_sef(elf_path, sef_output, flags=0):
    """Convert ELF to SEF format without mksef.py."""
    import struct

    sections = {}
    result = subprocess.run(
        [
            _find_scorpion_tool("objdump", _SCORPION_LD.replace("ld", "objdump")),
            "-h",
            elf_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"error: objdump failed on {elf_path}", file=sys.stderr)
        raise SystemExit(1)

    for line in result.stdout.split("\n"):
        parts = line.split()
        if len(parts) >= 4 and parts[0].isdigit():
            name = parts[1]
            if name in (".text", ".rodata", ".data", ".bss"):
                sections[name] = int(parts[2], 16)

    result = subprocess.run(
        [
            _find_scorpion_tool("readelf", _SCORPION_LD.replace("ld", "readelf")),
            "-h",
            elf_path,
        ],
        capture_output=True,
        text=True,
    )
    entry = 0
    for line in result.stdout.split("\n"):
        if "Entry point address" in line:
            entry = int(line.split(":")[1].strip(), 16)

    bin_path = elf_path + ".bin"
    objcopy = _find_scorpion_tool("objcopy", _SCORPION_OBJCOPY)
    subprocess.run([objcopy, "-O", "binary", elf_path, bin_path], capture_output=True)

    with open(bin_path, "rb") as f:
        flat = f.read()
    os.unlink(bin_path)

    segments = []
    off = 0
    for sec in (".text", ".rodata", ".data"):
        if sec in sections:
            segments.append((0 if sec == ".text" else 1, off, sections[sec]))
            off += sections[sec]
    if ".bss" in sections:
        segments.append((2, off, sections[".bss"]))

    num = len(segments)
    hdr = 12 + num * 16

    out = bytearray()
    out += struct.pack("<IIHH", 0x00464553, entry, num, flags)
    dc = hdr
    for st, sv, ss in segments:
        out += struct.pack("<IIII", st, sv, ss, dc)
        dc += ss
    out += flat

    with open(sef_output, "wb") as f:
        f.write(out)
