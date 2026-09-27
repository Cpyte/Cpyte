"""
elf2sef.py — ELF (RV32, PIC, --emit-relocs) to Scorpion SEF v2.1 converter.

Produces a relocatable SEF image (docs/dynamic-linking.md):

  * SEG_TEXT  / SEG_DATA / SEG_BSS  — flattened load image (linked at 0)
  * SEG_RELOC — load-time relocations for absolute references that were
                resolved at link time (R_RISCV_32 data words, lui-based
                HI20/LO12 absolute pairs, .got/.got.plt slots)
  * SEG_IMPORT — unresolved external symbols the loader must bind
  * SEG_EXPORT — symbols other images may import (--export NAME)
  * SEG_PLT    — lazy-binding `ebreak` stubs (--lazy)
  * SEG_VERDEF — version strings this image defines (--verdef VER)

An import record is emitted **per relocation site**, not per symbol name.
One symbol referenced from N places needs N records, because each site holds
its own copy of the address (a call site and a data word are separate words
in the image). Keying records by name binds only the first site and leaves
the rest pointing at whatever the linker left behind.

Link with:  -fPIC ... -Wl,-q --unresolved-symbols=ignore-all --no-relax

Usage:
  elf2sef.py [--lazy] [--weak NAME]... [--require NAME=VER]...
             [--verdef VER]... [--no-versym]
             [--export NAME]... [--export-file FILE] [--scope NAME]
             <input.elf> <output.sef>
"""

import struct
import sys

SEF_MAGIC = 0x00464553

SEG_TEXT = 0
SEG_DATA = 1
SEG_BSS = 2
SEG_RELOC = 3
SEG_IMPORT = 4
SEG_EXPORT = 5
SEG_PLT = 6
SEG_VERDEF = 7

SEF_MAX_SEGMENTS = 8

# There is deliberately no privilege flag: a SEF image never declares what it
# wants to become. The kernel's boot path grants manager privilege to the one
# image it finds bundled in flash; everything else is user code.
SEF_FLAG_DYNAMIC = 0x0002
SEF_FLAG_LAZY = 0x0004
SEF_FLAG_VERSYM = 0x0008

SEF_R_RELATIVE = 0
SEF_R_HI20 = 1
SEF_R_LO12I = 2
SEF_R_LO12S = 3
SEF_R_CALL = 4
SEF_R_LAZY_CALL = 5
SEF_R_PCREL_HI20 = 6
SEF_R_PCREL_LO12I = 7

SEF_IMPORT_WEAK = 0x00000001

SEF_PLT_STUB_SIZE = 8
SCOPE_SEP = "::"
VER_SEP = "@"

# ELF constants
SHT_NOBITS = 8
SHT_RELA = 4
SHT_SYMTAB = 2
SHF_ALLOC = 0x2
SHF_WRITE = 0x1
SHN_UNDEF = 0
SHN_ABS = 0xFFF1
SHN_COMMON = 0xFFF2
SHN_XINDEX = 0xFFFF

STT_SECTION = 3

# RISC-V relocation types (psABI)
R_RISCV_32 = 1
R_RISCV_HI20 = 26
R_RISCV_LO12_I = 27
R_RISCV_LO12_S = 28
R_RISCV_CALL = 18
R_RISCV_CALL_PLT = 19
R_RISCV_GOT_HI20 = 20
R_RISCV_GOT_LO12 = 23
R_RISCV_RELAX = 51

# relocations whose value is an absolute address; everything else is
# PC-relative / branch / relax / debug and is left alone at load time
ABSOLUTE_RELOCS = {
    R_RISCV_32: SEF_R_RELATIVE,
    R_RISCV_HI20: SEF_R_HI20,
    R_RISCV_LO12_I: SEF_R_LO12I,
    R_RISCV_LO12_S: SEF_R_LO12S,
    R_RISCV_CALL: SEF_R_CALL,
    R_RISCV_CALL_PLT: SEF_R_CALL,
}

# RISC-V encodings we need to recognise or synthesise.
OP_AUIPC = 0x17
OP_JALR = 0x67
OP_LW = 0x03
OP_LD = 0x03
INSN_EBREAK = 0x00100073

MAPPED_MAX = 0x00100000  # 1 MiB sanity bound for the flat image


def ver_hash(text):
    """FNV-1a-32 of a version string. Must agree with sef_ver_hash() in
    loader.c and scorpion_ver_hash() in abi/scorpion.h."""
    h = 2166136261
    for byte in text.encode("latin1"):
        h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
    return h


def split_name(name):
    """Split "[scope::]symbol[@version]" into (scope, symbol, version).

    Only the parts we need hashes or sanity checks for are returned; the
    record always stores `name` verbatim so both the inline spelling and the
    out-of-band hash are available to the loader.
    """
    scope, sym, ver = "", name, ""
    at = name.rfind(VER_SEP)
    if at > 0:
        sym, ver = name[:at], name[at + 1 :]
    sep = sym.find(SCOPE_SEP)
    if sep >= 0:
        scope, sym = sym[:sep], sym[sep + 2 :]
    return scope, sym, ver


def emit_call_pair(insn0, insn1, target, site):
    """Rewrite an auipc+jalr pair at `site` to branch to `target`.

    `disp` is the distance the linker would have encoded for a normal
    PC-relative call, and the split is the same round-to-nearest-4K the
    RISC-V psABI specifies, so this produces exactly the instruction pair
    R_RISCV_CALL_PLT would have emitted.
    """
    rd = (insn0 >> 7) & 0x1F
    disp = target - site
    hi = (disp + 0x800) >> 12
    lo = disp - (hi << 12)
    new0 = ((hi & 0xFFFFF) << 12) | (rd << 7) | OP_AUIPC
    new1 = ((lo & 0xFFF) << 20) | (rd << 7) | OP_JALR
    return new0, new1


class Section:
    def __init__(self, name, type_, flags, addr, offset, size, link, info, entsize):
        self.name = name
        self.type = type_
        self.flags = flags
        self.addr = addr
        self.offset = offset
        self.size = size
        self.link = link
        self.info = info
        self.entsize = entsize


class Symbol:
    def __init__(self, name, value, size, info, shndx):
        self.name = name
        self.value = value
        self.size = size
        self.info = info
        self.shndx = shndx

    @property
    def is_defined(self):
        return self.shndx != SHN_UNDEF and self.shndx != SHN_ABS

    @property
    def is_section(self):
        return (self.info & 0xF) == STT_SECTION


def parse_elf(path):
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"\x7fELF":
        sys.exit(f"error: {path}: not an ELF file")
    if data[4] != 1:  # ELFCLASS32
        sys.exit(f"error: {path}: not a 32-bit ELF")

    ei_data = data[5]
    endian = "<" if ei_data == 1 else ">"
    if ei_data == 2:
        sys.exit(f"error: {path}: big-endian ELF unsupported")

    (
        e_type,
        e_machine,
        e_version,
        e_entry,
        e_phoff,
        e_shoff,
        e_flags,
        e_ehsize,
        e_phentsize,
        e_phnum,
        e_shentsize,
        e_shnum,
        e_shstrndx,
    ) = struct.unpack_from(endian + "HHIIIIIHHHHHH", data, 16)

    if e_machine != 0xF3:
        sys.exit(f"error: {path}: not RISC-V (machine={e_machine:#x})")

    if e_shnum == 0 or e_shentsize != 40:
        sys.exit(f"error: {path}: bad section headers")

    shdr = []
    for i in range(e_shnum):
        shdr.append(struct.unpack_from(endian + "IIIIIIIIII", data, e_shoff + i * 40))

    # section name string table
    shstr = shdr[e_shstrndx]
    shstr_data = data[shstr[4] : shstr[4] + shstr[5]]

    def cstr(buf, off):
        if off >= len(buf):
            return ""
        end = buf.find(b"\x00", off)
        if end < 0:
            return ""
        return buf[off:end].decode("latin1")

    sections = {}
    by_index = []
    for i, s in enumerate(shdr):
        name = cstr(shstr_data, s[0])
        sec = Section(name, s[1], s[2], s[3], s[4], s[5], s[6], s[7], s[9])
        sections[name] = sec
        by_index.append(sec)

    def sym_by_index(idx):
        return symbols[idx] if idx < len(symbols) else None

    def section_by_index(idx):
        return by_index[idx] if idx < len(by_index) else None

    # symbol table (symbols[i] is ELF symbol index i; [0] is the null symbol)
    symbols = []
    if ".symtab" in sections:
        st = sections[".symtab"]
        strtab = section_by_index(st.link)
        str_data = b""
        if strtab is not None:
            str_data = data[strtab.offset : strtab.offset + strtab.size]
        for i in range(st.size // st.entsize):
            st_name, st_value, st_size, st_info, st_other, st_shndx = (
                struct.unpack_from(endian + "IIIBBH", data, st.offset + i * st.entsize)
            )
            name = cstr(str_data, st_name)
            symbols.append(Symbol(name, st_value, st_size, st_info, st_shndx))

    # alloc sections in address order
    alloc = [s for s in sections.values() if (s.flags & SHF_ALLOC) and s.size > 0]
    alloc.sort(key=lambda s: (s.addr, s.size))

    if not alloc:
        sys.exit(f"error: {path}: no allocated sections")

    base = alloc[0].addr
    mapped_end = max(s.addr + s.size for s in alloc)

    if mapped_end - base > MAPPED_MAX:
        sys.exit(f"error: {path}: image too large ({mapped_end - base:#x} bytes)")

    flat = bytearray(mapped_end - base)
    for s in alloc:
        if s.type != SHT_NOBITS:
            flat[s.addr - base : s.addr - base + s.size] = data[
                s.offset : s.offset + s.size
            ]

    # relocations: site -> (type, sym_index, addend)
    relocs = []
    for sec in sections.values():
        if sec.type != SHT_RELA or sec.link == 0:
            continue
        target = by_index[sec.info] if sec.info < len(by_index) else None
        if target is None or (target.flags & SHF_ALLOC) == 0:
            continue  # .rela.debug* etc.: no memory image
        for i in range(sec.size // sec.entsize):
            r_offset, r_info, r_addend = struct.unpack_from(
                endian + "IIi", data, sec.offset + i * sec.entsize
            )
            r_sym = r_info >> 8
            r_type = r_info & 0xFF
            relocs.append((r_offset, r_type, r_sym, r_addend))

    return {
        "entry": e_entry,
        "base": base,
        "mapped_end": mapped_end,
        "flat": flat,
        "sections": sections,
        "by_index": by_index,
        "symbols": symbols,
        "sym_by_index": sym_by_index,
        "section_by_index": section_by_index,
        "relocs": relocs,
    }


def is_absolute_pair(elf, sym_idx):
    """True if the LO12 reloc's symbol points at a `lui` (absolute pair)
    rather than an `auipc` (PC-relative pair)."""
    sym = elf["sym_by_index"](sym_idx)
    if sym is None or not sym.is_defined:
        return False
    off = sym.value - elf["base"]
    flat = elf["flat"]
    if off + 4 > len(flat):
        return False
    insn = struct.unpack_from("<I", flat, off)[0]
    return (insn & 0x7F) == 0x37  # lui


def sign_extend(value, bits):
    if value & (1 << (bits - 1)):
        return value - (1 << bits)
    return value


def got_slot_of(elf, site):
    """Decode the GOT entry address a linker-emitted auipc+lo12 pair points at.

    `-fPIC` reaches an *external* data symbol through the GOT, so the pair is
    `auipc rd, hi; lw rd, lo(rd)` and the load-base-dependent part is the GOT
    address, not the symbol itself. The linker has already resolved the pair,
    so rather than re-deriving the psABI addend convention we read the
    displacement back out of the two encodings and check that it lands in a
    real GOT section. A pair that does not is not ours to rewrite.
    """
    flat = elf["flat"]
    if site + 8 > len(flat):
        return None
    insn0 = struct.unpack_from("<I", flat, site)[0]
    insn1 = struct.unpack_from("<I", flat, site + 4)[0]
    if (insn0 & 0x7F) != OP_AUIPC:
        return None
    if (insn1 & 0x7F) not in (OP_LW, OP_LD):
        return None

    hi = sign_extend((insn0 >> 12) & 0xFFFFF, 20)
    lo = sign_extend((insn1 >> 20) & 0xFFF, 12)
    disp = site + (hi << 12) + lo
    for name in (".got", ".got.plt"):
        sec = elf["sections"].get(name)
        if sec is None or (sec.flags & SHF_ALLOC) == 0:
            continue
        start = sec.addr - elf["base"]
        if start <= disp < start + sec.size:
            return disp
    return None


class Import:
    """One unresolved reference at one image offset."""

    __slots__ = ("rtype", "site", "name", "ver_hash", "flags", "plt")

    def __init__(self, rtype, site, name):
        self.rtype = rtype
        self.site = site
        self.name = name
        self.ver_hash = 0
        self.flags = 0
        self.plt = 0


def main(argv):
    exports = []
    export_file = None
    weak = []
    require = {}   # import name -> version
    verdefs = []
    scope = ""
    lazy = False
    versym = True
    positionals = []
    args = list(argv)

    i = 0
    while i < len(args):
        opt = args[i]
        if opt == "--export":
            exports.append(args[i + 1])
            i += 2
        elif opt == "--export-file":
            export_file = args[i + 1]
            i += 2
        elif opt == "--weak":
            weak.append(args[i + 1])
            i += 2
        elif opt == "--verdef":
            verdefs.append(args[i + 1])
            i += 2
        elif opt == "--scope":
            scope = args[i + 1]
            i += 2
        elif opt == "--require":
            if "=" not in args[i + 1]:
                sys.exit(f"error: --require wants NAME=VERSION, got {args[i+1]!r}")
            name, ver = args[i + 1].split("=", 1)
            require[name] = ver
            i += 2
        elif opt == "--lazy":
            lazy = True
            i += 1
        elif opt == "--no-lazy":
            lazy = False
            i += 1
        elif opt == "--no-versym":
            versym = False
            i += 1
        elif opt.startswith("--"):
            sys.exit(f"error: unknown option {opt}")
        else:
            positionals.append(opt)
            i += 1

    if len(positionals) != 2:
        sys.exit(__doc__)

    elf_path, sef_path = positionals
    elf = parse_elf(elf_path)
    flat = elf["flat"]
    base = elf["base"]
    sections = elf["sections"]

    if export_file:
        with open(export_file) as f:
            exports.extend(line.strip() for line in f if line.strip())

    # --- map / segment layout (linked at 0: vaddr == image offset) ---
    text = sections.get(".text")
    bss = sections.get(".bss")

    if text is None or (text.flags & SHF_ALLOC) == 0:
        sys.exit(f"error: {elf_path}: no allocated .text section")

    text_vaddr = text.addr - base
    text_size = text.size

    # A zero-sized .bss still has an address, and using it as the end of the
    # data segment would describe a hole the flat image never covers: the
    # segment would claim bytes that `flat` does not have, silently truncating
    # everything after it. Only a real .bss ends the data segment.
    has_bss = (
        bss is not None
        and (bss.flags & SHF_ALLOC) != 0
        and bss.size > 0
        and bss.addr >= text.addr + text.size
    )

    if has_bss:
        data_vaddr = text_vaddr + text_size
        data_size = (bss.addr - base) - data_vaddr
        bss_vaddr = bss.addr - base
        bss_size = bss.size
    else:
        data_vaddr = text_vaddr + text_size
        data_size = (elf["mapped_end"] - base) - data_vaddr
        bss_vaddr = 0
        bss_size = 0

    if data_size < 0 or bss_size < 0:
        sys.exit(f"error: {elf_path}: unexpected section order")

    # Every mapped segment has to be backed by real bytes, or the header
    # describes an image the file cannot supply.
    for label, vaddr, size in (
        ("SEG_TEXT", text_vaddr, text_size),
        ("SEG_DATA", data_vaddr, data_size),
        ("SEG_BSS", bss_vaddr, bss_size),
    ):
        if size and vaddr + size > len(flat):
            sys.exit(
                f"error: {elf_path}: {label} [0x{vaddr:x},0x{vaddr+size:x}) "
                f"is past the end of the linked image (0x{len(flat):x})"
            )

    image_end = bss_vaddr + bss_size if bss_size else data_vaddr + data_size

    # --- process relocations ---
    reloc_records = []  # (type, offset, value)
    imports = []        # one Import per relocation site
    sym_by_index = elf["sym_by_index"]
    section_by_index = elf["section_by_index"]
    mapped = len(flat)

    for r_offset, r_type, r_sym, r_addend in elf["relocs"]:
        if r_type == R_RISCV_RELAX:
            continue
        if r_type not in ABSOLUTE_RELOCS and r_type != R_RISCV_GOT_HI20:
            continue
        if r_offset < base or r_offset - base + 4 > mapped:
            continue  # site outside the load image (debug etc.)

        site = r_offset - base

        if r_type == R_RISCV_LO12_I or r_type == R_RISCV_LO12_S:
            if not is_absolute_pair(elf, r_sym):
                continue  # PC-relative pair; no load-time fixup needed

        sym = sym_by_index(r_sym)
        if sym is None:
            continue

        if not sym.is_defined:
            # Unresolved external. Every site gets its own record: the same
            # symbol called from N places occupies N words in the image, and
            # each word has to be written for the call to reach the target.
            if not sym.name:
                continue
            imp = Import(ABSOLUTE_RELOCS.get(r_type, SEF_R_RELATIVE), site, sym.name)
            _, _, inline_ver = split_name(sym.name)
            if inline_ver:
                imp.ver_hash = ver_hash(inline_ver)
            elif sym.name in require:
                imp.ver_hash = ver_hash(require[sym.name])
            if sym.name in weak:
                imp.flags |= SEF_IMPORT_WEAK

            if r_type == R_RISCV_GOT_HI20:
                # External data binds through the GOT: the word the loader
                # must write is the GOT slot, and the auipc/lo12 pair needs
                # fixing up to reach it. That makes this import a plain
                # 32-bit word write at the slot, not a patch of `site`.
                got = got_slot_of(elf, site)
                if got is None:
                    sys.exit(
                        f"error: {elf_path}: cannot resolve the GOT entry for "
                        f"{sym.name!r} referenced at 0x{site:x}"
                    )
                imp.rtype = SEF_R_RELATIVE
                imp.site = got
                imports.append(imp)
                # Two load-time relocs, one per half, each PC-relative to
                # the auipc so the pair computes got_entry at the real base.
                reloc_records.append((SEF_R_PCREL_HI20, site, got))
                reloc_records.append((SEF_R_PCREL_LO12I, site + 4, got))
            else:
                imports.append(imp)
            continue

        if r_type == R_RISCV_GOT_HI20:
            # GOT entry for a symbol this image defines: make the pair
            # load-base relative, and let the .got scan below turn the slot
            # into an absolute relocation.
            got = got_slot_of(elf, site)
            if got is None:
                continue
            reloc_records.append((SEF_R_PCREL_HI20, site, got))
            reloc_records.append((SEF_R_PCREL_LO12I, site + 4, got))
            continue

        sec = section_by_index(sym.shndx)
        if sec is not None and (sec.flags & SHF_ALLOC) == 0:
            continue  # e.g. debug symbol

        value = sym.value + r_addend
        reloc_records.append((ABSOLUTE_RELOCS[r_type], site, value))

    # Two relocations can land on the same offset only if the linker emitted
    # duplicates; collapse them so the loader never sees a double write.
    imports.sort(key=lambda imp: (imp.site, imp.rtype))
    deduped = []
    for imp in imports:
        if deduped and deduped[-1].site == imp.site:
            continue
        deduped.append(imp)
    imports = deduped

    # --- lazy PLT ---
    #
    # Each eligible call site gets a `ebreak` stub. The call site is
    # rewritten to reach the stub; the first execution traps, the handler
    # resolves the symbol and patches the site to branch straight at the
    # target, so the stub is entered at most once.
    plt_vaddr = 0
    plt_size = 0
    plt_bytes = b""

    if lazy:
        stubs = []
        for imp in imports:
            if imp.rtype != SEF_R_CALL:
                continue
            if imp.site + 8 > mapped:
                continue
            insn0 = struct.unpack_from("<I", flat, imp.site)[0]
            insn1 = struct.unpack_from("<I", flat, imp.site + 4)[0]
            if (insn0 & 0x7F) != OP_AUIPC or (insn1 & 0x7F) != OP_JALR:
                continue  # not the auipc+jalr shape a stub can intercept
            stubs.append((imp, insn0, insn1))

        if stubs:
            plt_vaddr = (image_end + SEF_PLT_STUB_SIZE - 1) & ~(SEF_PLT_STUB_SIZE - 1)
            plt_size = len(stubs) * SEF_PLT_STUB_SIZE
            if plt_vaddr + plt_size > MAPPED_MAX:
                sys.exit(f"error: {elf_path}: lazy PLT does not fit the image")

            body = bytearray()
            for index, (imp, insn0, insn1) in enumerate(stubs):
                imp.rtype = SEF_R_LAZY_CALL
                imp.plt = index * SEF_PLT_STUB_SIZE
                new0, new1 = emit_call_pair(insn0, insn1, plt_vaddr + imp.plt, imp.site)
                struct.pack_into("<I", flat, imp.site, new0)
                struct.pack_into("<I", flat, imp.site + 4, new1)
                # Both words are `ebreak`: the first traps, and if the handler
                # ever failed to resolve, the second traps too rather than
                # sliding into whatever follows as if it were code.
                body += struct.pack("<II", INSN_EBREAK, INSN_EBREAK)
            plt_bytes = bytes(body)

            # The call relocations for these sites were queued above, before we
            # knew they were lazy. The loader applies SEG_RELOC records at load
            # time, so leaving them in would re-apply the *link-time* target
            # and silently undo the stub rewrite above -- the call site would
            # jump straight at the original PLT entry, never trap, and the
            # lazy import would be marked bound while its stub is never
            # reached. The import record alone now drives this site.
            stub_sites = {imp.site for imp, _, _ in stubs}
            reloc_records = [
                rec for rec in reloc_records
                if not (rec[0] == SEF_R_CALL and rec[1] in stub_sites)
            ]

    # --- scan .got/.got.plt for slots the linker resolved without a reloc ---
    defined_values = set()
    for sym in elf["symbols"]:
        if sym is not None and sym.is_defined:
            sec = section_by_index(sym.shndx)
            if sec is not None and (sec.flags & SHF_ALLOC):
                defined_values.add(sym.value)
                if sym.is_section:
                    for delta in range(0, min(sec.size, 0x100), 4):
                        defined_values.add(sym.value + delta)

    plt_sec_names = (".plt", ".plt.sec")
    got_records = {}
    for got_name in (".got", ".got.plt"):
        got = sections.get(got_name)
        if got is None or (got.flags & SHF_ALLOC) == 0:
            continue
        for off in range(0, got.size, 4):
            slot_addr = got.addr + off
            word = struct.unpack_from("<I", flat, slot_addr - base)[0]
            if word == 0 or word == 0xFFFFFFFF:
                continue
            skip = False
            for name in plt_sec_names:
                sec = sections.get(name)
                if sec is not None and sec.addr <= word < sec.addr + sec.size:
                    skip = True  # PLT trampoline pointer (external call)
                    break
            if skip:
                continue
            if word in defined_values:
                got_records.setdefault(slot_addr - base, word)

    for slot, word in got_records.items():
        if not any(r[0] == SEF_R_RELATIVE and r[1] == slot for r in reloc_records):
            reloc_records.append((SEF_R_RELATIVE, slot, word))

    # --- exports ---
    # Names are stored verbatim, so a caller may spell them "sym", "sym@ver",
    # "scope::sym" or "scope::sym@ver" and the loader sees the same thing it
    # would have seen in an import. ver_hash repeats the version out of band
    # for producers that keep the two apart.
    #
    # --export takes either NAME (export the ELF symbol of that name) or
    # SYMBOL=WIRE_NAME (export SYMBOL under a different published name, which
    # is how a versioned alias like addone@2.0 is published from a plain
    # `addone_v2` definition).
    export_records = []
    for raw in exports:
        if "=" in raw:
            elf_name, wire = raw.split("=", 1)
        else:
            elf_name, wire = raw, raw
        # --scope applies to every name that does not already carry one,
        # including versioned ones, so --export foo@1.0 under --scope util
        # publishes "util::foo@1.0" rather than leaving it unscoped.
        if scope and SCOPE_SEP not in wire.split(VER_SEP, 1)[0]:
            wire = scope + SCOPE_SEP + wire

        for sym in elf["symbols"]:
            if sym is None or not sym.is_defined or sym.name != elf_name:
                continue
            _, _, inline_ver = split_name(wire)
            export_records.append(
                (sym.value, wire, ver_hash(inline_ver) if inline_ver else 0)
            )
            break
        else:
            sys.exit(f"error: {elf_path}: export {raw!r} not found")

    # --- version definitions ---
    # Anything named in an export's @ver tail is a version this image defines.
    defined_vers = list(verdefs)
    for _, name, _ in export_records:
        _, _, inline_ver = split_name(name)
        if inline_ver and inline_ver not in defined_vers:
            defined_vers.append(inline_ver)

    # --- build SEF ---
    flags = 0
    dynamic = bool(reloc_records or imports or export_records)
    if dynamic:
        flags |= SEF_FLAG_DYNAMIC
    if plt_bytes:
        flags |= SEF_FLAG_LAZY
    if versym and (imports or export_records or defined_vers):
        flags |= SEF_FLAG_VERSYM

    segments = []  # (type, vaddr, size, data)
    segments.append((SEG_TEXT, text_vaddr, text_size, None))
    segments.append((SEG_DATA, data_vaddr, data_size, None))
    if bss_size:
        segments.append((SEG_BSS, bss_vaddr, bss_size, None))

    reloc_data = b"".join(struct.pack("<III", t, o, v) for (t, o, v) in reloc_records)
    if reloc_data:
        segments.append((SEG_RELOC, 0, len(reloc_data), reloc_data))

    import_data = b""
    for imp in imports:
        nb = imp.name.encode("latin1")
        if versym:
            import_data += struct.pack(
                "<IIIIII",
                imp.rtype,
                imp.site,
                imp.plt,
                imp.ver_hash,
                imp.flags,
                len(nb),
            )
        else:
            import_data += struct.pack("<III", imp.rtype, imp.site, len(nb))
        import_data += nb
        import_data += b"\x00" * ((4 - len(nb) % 4) % 4)
    if import_data:
        segments.append((SEG_IMPORT, 0, len(import_data), import_data))

    export_data = b""
    for value, name, vhash in export_records:
        nb = name.encode("latin1")
        if versym:
            export_data += struct.pack("<III", value, vhash, len(nb))
        else:
            export_data += struct.pack("<II", value, len(nb))
        export_data += nb
        export_data += b"\x00" * ((4 - len(nb) % 4) % 4)
    if export_data:
        segments.append((SEG_EXPORT, 0, len(export_data), export_data))

    if plt_bytes:
        segments.append((SEG_PLT, plt_vaddr, plt_size, plt_bytes))

    verdef_data = b""
    for ver in defined_vers:
        nb = ver.encode("latin1")
        verdef_data += struct.pack("<I", len(nb)) + nb
        verdef_data += b"\x00" * ((4 - len(nb) % 4) % 4)
    if verdef_data:
        segments.append((SEG_VERDEF, 0, len(verdef_data), verdef_data))

    if len(segments) > SEF_MAX_SEGMENTS:
        sys.exit(
            f"error: {elf_path}: {len(segments)} segments exceeds "
            f"SEF_MAX_SEGMENTS={SEF_MAX_SEGMENTS}"
        )

    out = bytearray()
    out += struct.pack("<IIHH", SEF_MAGIC, elf["entry"], len(segments), flags)

    dc = 12 + len(segments) * 16
    body = bytearray()
    for st, vaddr, size, payload in segments:
        if payload is None:
            start = dc
            dc += size
            out += struct.pack("<IIII", st, vaddr, size, start)
            body += flat[vaddr : vaddr + size]
        else:
            out += struct.pack("<IIII", st, vaddr, size, dc)
            dc += len(payload)
            body += payload

    out += body

    with open(sef_path, "wb") as f:
        f.write(out)

    print(
        f"Created {sef_path}: {len(out)} bytes, {len(segments)} segments, "
        f"entry=0x{elf['entry']:x}, flags=0x{flags:x}"
    )
    print(
        f"  relocs={len(reloc_records)} imports={len(imports)} "
        f"exports={len(export_records)}"
    )
    if plt_bytes:
        print(f"  lazy PLT: {len(plt_bytes) // SEF_PLT_STUB_SIZE} stubs at 0x{plt_vaddr:x}")
    if defined_vers:
        print(f"  version defs: {', '.join(defined_vers)}")

    # Import sites must be distinct: the whole point of the v2.1 record set is
    # that every site binds, so a duplicate here is a tool bug worth failing on.
    sites = [imp.site for imp in imports]
    if len(sites) != len(set(sites)):
        sys.exit(f"error: {elf_path}: internal error, duplicate import sites")


if __name__ == "__main__":
    main(sys.argv[1:])
