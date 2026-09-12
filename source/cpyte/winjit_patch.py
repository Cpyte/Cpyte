# Windows MinGW GNU JIT reloc fixer.

from __future__ import annotations

import ctypes
import os
import struct
from typing import Any, Dict, List, Optional, Tuple

# Undefined-symbol relocation types in the MinGW GNU large-model ELF objects
# emitted by the ``*windows-gnu-elf`` JIT triple on this machine.
_R_X86_64_64 = 1
_R_X86_64_PLT32 = 4
_R_X86_64_32 = 10
_R_X86_64_32S = 11
_R_X86_64_PC64 = 24
_R_X86_64_GOTOFF64 = 25
_R_X86_64_GOTPC64 = 29

_JIT_ANCHORS_IR = """\
target triple = "{triple}"
@_JITBASE_LDATA = global i8 0, section ".ldata"
@_JITBASE_LRODATA = constant i8 0, section ".lrodata"
@_JITBASE_LBSS = global i8 0, section ".lbss"
"""

_WINDOWS_EXPORT_DLLS = (
    "ucrtbase",
    "msvcrt",
    "kernel32",
    "kernelbase",
    "ntdll",
)

_export_cache: Dict[str, int] = {}
_extra_cache: Dict[str, int] = {}
_k32_gpa_cache: Any = None
k32: Any = None


def _init_k32() -> Any:
    global k32
    if k32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.VirtualProtect.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        k32.VirtualProtect.restype = ctypes.c_long
    return k32


def link_jit_anchors(mod: Any, triple: str) -> None:
    """Give the JIT module real members in `.ldata`/`.lrodata`/`.lbss` so the
    reloc patcher can recover each section's loaded address after finalize."""
    from llvmlite import binding

    binding.initialize_native_asmparser()
    anchor_mod = binding.parse_assembly(_JIT_ANCHORS_IR.format(triple=triple))
    binding.link_modules(mod, anchor_mod)


def _scavenge_export(name: str) -> int:
    """Lazily resolve ``name`` by scanning standard DLL exports.

    Catches symbols the module itself never declared (intrinsic-to-libcall
    lowering: ``memcpy``/``memset``/``floor`` from ``llvm.*``, ``__udivti3``)
    plus the ``snprintf -> vsnprintf`` family that lacks a plain export.
    """
    global _k32_gpa_cache, _export_cache
    if name in _export_cache:
        return _export_cache[name]

    if _k32_gpa_cache is None:
        _k32_gpa_cache = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32_gpa_cache.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        _k32_gpa_cache.GetProcAddress.restype = ctypes.c_void_p

    for d in _WINDOWS_EXPORT_DLLS:
        if d not in _extra_cache:
            try:
                _extra_cache[d] = ctypes.WinDLL(d, use_last_error=True)._handle
            except OSError:
                _extra_cache[d] = 0

    # Build unique candidate symbol names to test
    candidates = [name, f"_{name}"]
    if name == "snprintf":
        candidates.append("vsnprintf")

    for alias in candidates:
        alias_bytes = alias.encode("utf-8")
        for d in _WINDOWS_EXPORT_DLLS:
            h = _extra_cache.get(d)
            if not h:
                continue
            try:
                addr = _k32_gpa_cache.GetProcAddress(ctypes.c_void_p(h), alias_bytes)
            except Exception:
                addr = 0
            if addr:
                _export_cache[name] = addr
                return addr

    _export_cache[name] = 0
    return 0


def patch_windows_gnu_relocs(
    engine: Any, tm: Any, mod: Any, res: Dict[str, int]
) -> int:
    """Fix relocation immediates MCJIT left unresolved in the merged JIT module."""
    _init_k32()
    if not res:
        return 0

    _dbg = os.environ.get("CPYTE_JIT_DEBUG")
    if _dbg:
        print(f"[winjit] patcher: {len(res)} resolved symbols", flush=True)
        print(
            "[winjit] res keys: " + " ".join(f"{k}={v:#x}" for k, v in res.items()),
            flush=True,
        )

    obj = tm.emit_object(mod)
    if obj[:4] != b"\x7fELF":
        return 0

    d = obj
    e_shoff = struct.unpack_from("<Q", d, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", d, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", d, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", d, 0x3E)[0]

    shdrs: Dict[int, Dict[str, int]] = {}
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        sh = struct.unpack_from("<IIQQQQIIQQ", d, o)
        shdrs[i] = dict(
            name_idx=sh[0],
            sh_type=sh[1],
            offset=sh[4],
            size=sh[5],
            link=sh[6],
            info=sh[7],
        )

    shstr = shdrs[e_shstrndx]
    shstr_data = d[shstr["offset"] : shstr["offset"] + shstr["size"]]

    # Pre-decode section names once to optimize string processing inside loops
    secname_by_idx: Dict[int, str] = {}
    for i, sh_info in shdrs.items():
        off = sh_info["name_idx"]
        j = shstr_data.find(b"\x00", off)
        secname_by_idx[i] = shstr_data[off:j].decode("utf-8", errors="replace")

    symtab = symlink = None
    for sh in shdrs.values():
        if sh["sh_type"] == 2:  # SHT_SYMTAB
            symtab, symlink = sh, shdrs[sh["link"]]
            break

    if symtab is None or symlink is None:
        return 0

    strsyms = d[symlink["offset"] : symlink["offset"] + symlink["size"]]
    symbols: List[Tuple[str, int, int]] = []
    for i in range(symtab["size"] // 24):
        o = symtab["offset"] + i * 24
        st_name, st_info, st_other, st_shndx, st_value, st_size = struct.unpack_from(
            "<IBBHQQ", d, o
        )
        j = strsyms.find(b"\x00", st_name)
        symbols.append(
            (strsyms[st_name:j].decode("utf-8", errors="replace"), st_value, st_shndx)
        )

    bases: Dict[str, int] = {}
    try:
        main_addr = engine.get_function_address("main")
    except Exception:
        return 0

    main_val = 0
    for name, st_value, st_shndx in symbols:
        if name == "main" and st_shndx != 0:
            main_val = st_value

    bases[".ltext"] = main_addr - main_val
    for anchor in ("_JITBASE_LDATA", "_JITBASE_LRODATA", "_JITBASE_LBSS"):
        try:
            v = engine.get_global_value_address(anchor)
            bases["." + anchor.split("_JITBASE_", 1)[1].lower()] = v
        except Exception:
            pass

    ltext_base = bases.get(".ltext")
    if not ltext_base:
        if _dbg:
            print("[winjit] no .ltext base", flush=True)
        return 0

    sites2: List[str] = []

    symaddrs: Dict[str, Optional[int]] = {}
    defined_names = {n for n, v, sh in symbols if n and sh != 0}
    for name, st_value, st_shndx in symbols:
        if st_shndx == 0:
            sa = None
            if name in defined_names:
                try:
                    sa = engine.get_function_address(name)
                except Exception:
                    sa = None
            if sa is None:
                sa = res.get(name)
            if sa is None:
                sa = _scavenge_export(name)
            symaddrs[name] = sa
        else:
            secn = secname_by_idx.get(st_shndx)
            b = bases.get(secn) if secn else None
            if b is not None:
                symaddrs[name] = (b + st_value) & 0xFFFFFFFFFFFFFFFF

    all_relocs: List[Tuple[str, int, int, str, int, int]] = []
    for sh in shdrs.values():
        if sh["sh_type"] != 4:  # SHT_RELA
            continue
        tsec = secname_by_idx.get(sh["info"], "")
        if tsec not in bases or tsec == ".eh_frame":
            continue
        for i in range(sh["size"] // 24):
            o = sh["offset"] + i * 24
            r_off, r_info, r_addend = struct.unpack_from("<QQq", d, o)
            r_type = r_info & 0xFFFFFFFF
            symname, _, st_shndx = symbols[r_info >> 32]
            all_relocs.append((tsec, r_off, r_type, symname, r_addend, st_shndx))

    funcs: List[List[Any]] = sorted(
        [
            [s[0], s[1], 0]
            for s in symbols
            if s[0]
            and not s[0].startswith(".L")
            and secname_by_idx.get(s[2]) == ".ltext"
        ],
        key=lambda f: f[1],
    )
    for i in range(len(funcs) - 1):
        funcs[i][2] = funcs[i + 1][1]
    if funcs:
        funcs[-1][2] = 0x7FFFFFFF

    def owning(r_off: int) -> Optional[List[Any]]:
        lo, hi = 0, len(funcs)
        while lo < hi:
            mid = (lo + hi) // 2
            if funcs[mid][1] > r_off:
                hi = mid
            else:
                lo = mid + 1
        if lo:
            f = funcs[lo - 1]
            if f[1] <= r_off < f[2]:
                return f
        return None

    univ_got: List[int] = []
    for tsec, r_off, r_type, symname, add, shndx in all_relocs:
        if tsec == ".ltext" and r_type == _R_X86_64_GOTPC64:
            imm64 = struct.unpack_from(
                "<Q", ctypes.string_at(ltext_base + r_off, 8), 0
            )[0]
            gv = (ltext_base + r_off + 4 + imm64) & 0xFFFFFFFFFFFFFFFF
            univ_got.append(gv)
            if _dbg:
                start = max(0, r_off - 8)
                chunk = ctypes.string_at(ltext_base + start, 24)
                sites2.append(
                    f"GOTPC64 @{tsec}+0x{r_off:x} gb={gv:#x} bytes={chunk.hex()}"
                )

    first_univ = univ_got[0] if univ_got else None

    def decode_gotbase(faddr: int, func_size: int) -> Optional[int]:
        if not faddr:
            return None
        n = min(func_size, 160)
        try:
            raw = ctypes.string_at(faddr, n)
        except Exception:
            return None
        i = 0
        while i < n - 20:
            if raw[i] == 0x48 and raw[i + 1] == 0x8D and (raw[i + 2] & 0xC7) == 0x05:
                disp = struct.unpack_from("<i", raw, i + 3)[0]
                lea_eval = (faddr + i + 7 + disp) & 0xFFFFFFFFFFFFFFFF
                j = i + 7
                while j < i + 40 and j < n:
                    if raw[j] == 0x48 and 0xB8 <= raw[j + 1] <= 0xBF:
                        delta = struct.unpack_from("<Q", raw, j + 2)[0]
                        return (lea_eval + delta) & 0xFFFFFFFFFFFFFFFF
                    j += 1
            i += 1
        return None

    func_got: Dict[str, int] = {}
    for f in funcs:
        g = decode_gotbase(
            (ltext_base + f[1]) & 0xFFFFFFFFFFFFFFFF,
            min(f[2] - f[1], 0x10000),
        )
        if g is not None:
            func_got[f[0]] = g

    gotoff_fix: Dict[int, int] = {}
    for tsec, r_off, r_type, symname, add, shndx in all_relocs:
        if not (tsec == ".ltext" and r_type == _R_X86_64_GOTOFF64 and shndx == 0):
            continue
        f = owning(r_off)
        gb = func_got.get(f[0]) if f is not None else None
        if gb is None and first_univ:
            gb = first_univ
        if gb is not None:
            gotoff_fix[r_off] = gb

    def wr(tgt: int, b: bytes) -> None:
        """Write memory securely across page boundaries and restore page protections."""
        size = len(b)
        start_page = tgt & ~0xFFF
        end_page = (tgt + size - 1) & ~0xFFF
        prot_len = (end_page - start_page) + 0x1000

        old_prot = ctypes.c_ulong()
        # 0x40 = PAGE_EXECUTE_READWRITE
        k32.VirtualProtect(
            ctypes.c_void_p(start_page), prot_len, 0x40, ctypes.byref(old_prot)
        )
        ctypes.memmove(ctypes.c_void_p(tgt), b, size)
        # Restore original protections
        k32.VirtualProtect(
            ctypes.c_void_p(start_page),
            prot_len,
            old_prot.value,
            ctypes.byref(old_prot),
        )

    patched = 0
    for tsec, r_off, r_type, symname, add, shndx in all_relocs:
        if symname == "_GLOBAL_OFFSET_TABLE_":
            continue

        tgt = bases[tsec] + r_off
        if r_type == _R_X86_64_GOTOFF64:
            if shndx != 0:
                continue
            gb = gotoff_fix.get(r_off)
            sa = symaddrs.get(symname)
            if gb is None or sa is None:
                continue
            val = (sa - gb) & 0xFFFFFFFFFFFFFFFF
            wr(tgt, struct.pack("<Q", val))
            patched += 1
            continue

        if shndx != 0:
            continue

        sa = symaddrs.get(symname)
        if sa is None:
            if _dbg:
                print(
                    f"[winjit] UNRESOLVED {symname} @{tsec}+0x{r_off:x} type={r_type}",
                    flush=True,
                )
            continue

        if r_type == _R_X86_64_64:
            wr(tgt, struct.pack("<Q", (sa + add) & 0xFFFFFFFFFFFFFFFF))
        elif r_type == _R_X86_64_PC64:
            wr(tgt, struct.pack("<Q", (sa + add - tgt) & 0xFFFFFFFFFFFFFFFF))
        elif r_type == _R_X86_64_PLT32:
            v = sa + add - tgt - 4
            if not (-0x80000000 <= v <= 0x7FFFFFFF):
                if _dbg:
                    print(
                        f"[winjit] OVERFLOW: PLT32 target too far ({v:#x})", flush=True
                    )
                continue
            wr(tgt, struct.pack("<i", v))
        elif r_type in (_R_X86_64_32, _R_X86_64_32S):
            v = sa + add
            if not (0 <= v <= 0xFFFFFFFF or -0x80000000 <= v <= 0x7FFFFFFF):
                continue
            wr(tgt, struct.pack("<i", v if v < 0x80000000 else v - 0x100000000))
        else:
            continue

        patched += 1

    if _dbg:
        print(f"[winjit] patched undefined relocs: {patched}", flush=True)

    return patched
