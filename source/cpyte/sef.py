#! /usr/bin/env python3
"""Scorpion SEF binary formatter: pack, dump, check, size.

SEF (Scorpion Executable) layout, all little-endian (see sef.h and
docs/exec-format.md):

THIS FILE IS RELATED TO THE SCORPION PROJECT.
LET'S EAT POTATOES!!!!!!!!!
:):):):):):):):)

  Offset  Size  Field
  ------  ----  ------------------------
    0      4    magic        = 0x00464553 ("SEF\\0")
    4      4    entry        (byte offset from base of loaded segments)
    8      2    num_segments
   10      2    flags        (bit 0 reserved; 1 = dynamic, 2 = lazy,
                             3 = symbol versions)
   12     16*n  segments[]   (n = num_segments)

  Segment descriptor (16 bytes):
    0   4   type   (0=TEXT, 1=DATA, 2=BSS)
    4   4   vaddr  (byte offset from the base allocation)
    8   4   size
   12   4   offset (byte offset in the file where segment data begins)

The loader (loader.c sef_load) treats all segments as living in one
contiguous allocation of `total = sum(seg.size)` bytes.  Each segment is
placed at `base + vaddr`, TEXT/DATA are copied from the file, BSS is
zero-filled.  PC = base + entry, GP = base, SP = (base + total + 4096) & ~0xF.

This module keeps the core logic in plain functions (no argparse inside) so
it can be embedded directly into another tool's CLI.
"""

import json
import struct

SEF_MAGIC = 0x00464553
SEF_MAX_SEGMENTS = 8

SEG_TEXT = 0
SEG_DATA = 1
SEG_BSS = 2
SEG_RELOC = 3
SEG_IMPORT = 4
SEG_EXPORT = 5
SEGMENT_TYPES = {
    SEG_TEXT: "TEXT",
    SEG_DATA: "DATA",
    SEG_BSS: "BSS",
    SEG_RELOC: "RELOC",
    SEG_IMPORT: "IMPORT",
    SEG_EXPORT: "EXPORT",
}
# Dynamic metadata segments are file-resident records, not memory.
MEMORY_SEGMENTS = (SEG_TEXT, SEG_DATA, SEG_BSS)

SEF_FLAG_DYNAMIC = 0x0002  # carries RELOC/IMPORT/EXPORT metadata
SEF_FLAG_LAZY = 0x0004  # SEG_PLT present; calls bind on 1st call
SEF_FLAG_VERSYM = 0x0008  # import/export records carry symbol versions

# (see sef.h in the WEW-scorpion tree)
SEF_R_RELATIVE = 0  # add base to 32-bit word at offset
SEF_R_HI20 = 1  # add base to lui immediate
SEF_R_LO12I = 2  # add base to addi/lw immediate
SEF_R_LO12S = 3  # add base to store immediate
SEF_R_CALL = 4  # patch auipc+jalr pair at offset to absolute target
SEF_R_LAZY_CALL = 5  # v2.1: lazy call through a SEG_PLT stub
RELOC_TYPES = {
    SEF_R_RELATIVE: "R_RELATIVE",
    SEF_R_HI20: "R_HI20",
    SEF_R_LO12I: "R_LO12I",
    SEF_R_LO12S: "R_LO12S",
    SEF_R_CALL: "R_CALL",
    SEF_R_LAZY_CALL: "R_LAZY_CALL",
}

# v2.1 import record flag bits (sef.h)
SEF_IMPORT_WEAK = 0x00000001  # unresolved is not a link-time failure

HEADER_SIZE = 12
SEGMENT_SIZE = 16

USER_STACK_SIZE = 4096


class SefError(Exception):
    """Raised for malformed SEF data."""


def read_segments(data):
    """Decode a SEF blob into (header, [segments]).

    header is a dict; each segment is a dict with keys type, vaddr, size,
    offset.  Raises SefError on structural problems.
    """
    if len(data) < HEADER_SIZE:
        raise SefError(
            "file too small: %d bytes, need at least %d" % (len(data), HEADER_SIZE)
        )

    magic, entry, num, flags = struct.unpack_from("<IIHH", data, 0)
    if magic != SEF_MAGIC:
        raise SefError("bad magic 0x%08X (expected 0x%08X)" % (magic, SEF_MAGIC))
    if num > SEF_MAX_SEGMENTS:
        raise SefError("too many segments: %d (max %d)" % (num, SEF_MAX_SEGMENTS))

    need = HEADER_SIZE + num * SEGMENT_SIZE
    if len(data) < need:
        raise SefError("truncated header: %d bytes, need %d" % (len(data), need))

    segments = []
    for i in range(num):
        stype, vaddr, size, offset = struct.unpack_from(
            "<IIII", data, HEADER_SIZE + i * SEGMENT_SIZE
        )
        segments.append(
            {
                "type": stype,
                "vaddr": vaddr,
                "size": size,
                "offset": offset,
            }
        )

    header = {
        "entry": entry,
        "flags": flags,
        "num_segments": num,
    }
    return header, segments


def total_mem(segments):
    """Memory footprint of a SEF image.

    Only TEXT/DATA/BSS occupy the base allocation; RELOC/IMPORT/EXPORT are
    file-resident metadata (loader.c: `if (type <= SEG_BSS) total += size`).
    """
    return sum(seg["size"] for seg in segments if seg["type"] in MEMORY_SEGMENTS)


def _align4(n):
    return (n + 3) & ~3


def decode_relocs(data, offset, size):
    """Decode SEG_RELOC records -> list of dicts {type, offset, value}."""
    out = []
    end = offset + size
    p = offset
    while p + 12 <= end:
        rtype, roff, value = struct.unpack_from("<III", data, p)
        out.append(
            {
                "type": rtype,
                "type_name": RELOC_TYPES.get(rtype, "R_UNKNOWN(%d)" % rtype),
                "offset": roff,
                "value": value,
            }
        )
        p += 12
    return out


def decode_imports(data, offset, size, versym=False):
    """Decode SEG_IMPORT records -> list of dicts {type, slot, plt, hash,
    flags, name}. v2.0 import records are 12 bytes (type, slot, name_len);
    v2.1 (SEF_FLAG_VERSYM) are 24 bytes (type, slot, plt, ver_hash, flags,
    name_len). Names are ALIGN_UP(name_len, 4) padded."""
    out = []
    end = offset + size
    p = offset
    hdr = 24 if versym else 12
    while p + hdr <= end:
        itype, slot = struct.unpack_from("<II", data, p)
        if versym:
            plt, ver_hash, flags, name_len = struct.unpack_from("<IIII", data, p + 8)
        else:
            name_len, = struct.unpack_from("<I", data, p + 8)
            plt = 0
            ver_hash = 0
            flags = 0
        if name_len > end - (p + hdr):
            break
        name = data[p + hdr : p + hdr + name_len].decode("latin1")
        out.append(
            {
                "type": itype,
                "type_name": RELOC_TYPES.get(itype, "R_UNKNOWN(%d)" % itype),
                "slot": slot,
                "plt": plt,
                "ver_hash": ver_hash,
                "flags": flags,
                "name": name,
            }
        )
        p += hdr + _align4(name_len)
    return out


def decode_exports(data, offset, size, versym=False):
    """Decode SEG_EXPORT records -> list of dicts {value, hash, name}.
    v2.0 export records are 8 bytes (value, name_len); v2.1 (VERSYM) are
    12 bytes (value, ver_hash, name_len)."""
    out = []
    end = offset + size
    p = offset
    hdr = 12 if versym else 8
    while p + hdr <= end:
        value, = struct.unpack_from("<I", data, p)
        if versym:
            ver_hash, name_len = struct.unpack_from("<II", data, p + 4)
        else:
            name_len, = struct.unpack_from("<I", data, p + 4)
            ver_hash = 0
        if name_len > end - (p + hdr):
            break
        name = data[p + hdr : p + hdr + name_len].decode("latin1")
        out.append({"value": value, "ver_hash": ver_hash, "name": name})
        p += hdr + _align4(name_len)
    return out


def type_name(stype):
    return SEGMENT_TYPES.get(stype, "UNKNOWN(%d)" % stype)


def check(data):
    """Validate a SEF blob against the loader's rules.

    Returns a list of (level, message) findings where level is
    "pass", "warn", or "fail".
    """
    findings = []

    def record(level, msg):
        findings.append((level, msg))

    try:
        header, segments = read_segments(data)
    except SefError as exc:
        record("fail", str(exc))
        return findings

    record("pass", "magic 0x%08X" % SEF_MAGIC)
    record(
        "pass",
        "header %d bytes (header+descriptors)"
        % (HEADER_SIZE + header["num_segments"] * SEGMENT_SIZE),
    )

    total = total_mem(segments)
    if total == 0:
        record("fail", "total segment size is 0")
    else:
        record("pass", "total memory %d bytes" % total)

    dynamic = bool(header["flags"] & SEF_FLAG_DYNAMIC)
    versym = bool(header["flags"] & SEF_FLAG_VERSYM)
    if dynamic:
        record("pass", "flags 0x%04X -> dynamic SEF (RELOC/IMPORT/EXPORT)" % header["flags"])
    else:
        record("pass", "flags 0x%04X -> static SEF" % header["flags"])

    for i, seg in enumerate(segments):
        tname = type_name(seg["type"])
        ok = True

        if seg["type"] not in SEGMENT_TYPES:
            record("fail", "seg %d: unknown type %d" % (i, seg["type"]))
            ok = False

        if seg["type"] in MEMORY_SEGMENTS:
            if seg["vaddr"] + seg["size"] > total:
                record(
                    "fail",
                    "seg %d (%s): vaddr 0x%X + size %d exceeds total %d"
                    % (i, tname, seg["vaddr"], seg["size"], total),
                )
                ok = False
        elif dynamic:
            # File-resident metadata records; only the file range must fit.
            end = seg["offset"] + seg["size"]
            if end > len(data):
                record(
                    "fail",
                    "seg %d (%s): data [0x%X,0x%X) exceeds file size %d"
                    % (i, tname, seg["offset"], end, len(data)),
                )
                ok = False
        else:
            record("fail", "seg %d (%s): metadata segment in a static SEF" % (i, tname))
            ok = False

        if seg["type"] in (SEG_TEXT, SEG_DATA):
            end = seg["offset"] + seg["size"]
            if end > len(data):
                record(
                    "fail",
                    "seg %d (%s): data [0x%X,0x%X) exceeds file size %d"
                    % (i, tname, seg["offset"], end, len(data)),
                )
                ok = False

        if ok:
            desc = "seg %d (%s) vaddr=0x%X size=%d" % (
                i,
                tname,
                seg["vaddr"],
                seg["size"],
            )
            if seg["type"] == SEG_BSS:
                desc += " (zero-filled)"
            else:
                desc += " offset=0x%X" % seg["offset"]
            record("pass", desc)

    # Overlap detection between memory segment vaddr ranges.
    ranges = sorted(
        (seg["vaddr"], seg["vaddr"] + seg["size"], i)
        for i, seg in enumerate(segments)
        if seg["type"] in MEMORY_SEGMENTS
    )
    for (a_start, a_end, a_i), (b_start, b_end, b_i) in zip(ranges, ranges[1:]):
        if b_start < a_end:
            record("fail", "seg %d and seg %d overlap in vaddr" % (a_i, b_i))

    # Validate dynamic metadata records decode cleanly.
    if dynamic:
        for i, seg in enumerate(segments):
            if seg["type"] in (SEG_RELOC, SEG_IMPORT, SEG_EXPORT):
                if seg["offset"] + seg["size"] > len(data):
                    continue  # already flagged
                if seg["type"] == SEG_RELOC:
                    records = decode_relocs(data, seg["offset"], seg["size"])
                    if not records and seg["size"]:
                        record("warn", "seg %d (RELOC): undecodable records" % i)
                        continue
                    record("pass", "seg %d (RELOC): %d record(s)" % (i, len(records)))
                elif seg["type"] == SEG_IMPORT:
                    records = decode_imports(
                        data, seg["offset"], seg["size"], versym=versym
                    )
                    if not records and seg["size"]:
                        record("warn", "seg %d (IMPORT): undecodable records" % i)
                        continue
                    record("pass", "seg %d (IMPORT): %d record(s)" % (i, len(records)))
                else:
                    records = decode_exports(
                        data, seg["offset"], seg["size"], versym=versym
                    )
                    if not records and seg["size"]:
                        record("warn", "seg %d (EXPORT): undecodable records" % i)
                        continue
                    record("pass", "seg %d (EXPORT): %d record(s)" % (i, len(records)))

    entry = header["entry"]
    if entry >= total:
        record("fail", "entry 0x%X beyond total memory 0x%X" % (entry, total))
    else:
        in_text = False
        for seg in segments:
            if (
                seg["type"] == SEG_TEXT
                and seg["vaddr"] <= entry < seg["vaddr"] + seg["size"]
            ):
                in_text = True
                break
        if in_text:
            record("pass", "entry 0x%X inside TEXT" % entry)
        else:
            record(
                "warn",
                "entry 0x%X is outside every TEXT segment (may crash on jump)" % entry,
            )

    return findings


def layout(header, segments):
    """Return a dict describing the derived runtime placement (base-relative)."""
    total = total_mem(segments)
    stack_top = (total + USER_STACK_SIZE) & ~0xF
    return {
        "file_size": HEADER_SIZE
        + header["num_segments"] * SEGMENT_SIZE
        + sum(seg["size"] for seg in segments if seg["type"] != SEG_BSS),
        "total_mem": total,
        "entry": header["entry"],
        "flags": header["flags"],
        "sp": stack_top,
        "stack_size": stack_top - total,
        "num_segments": header["num_segments"],
    }


def digest(data):
    """Return a dict with the dynamic-linking surface of a SEF blob.

    Keys: header info, segments, exports (list of {value, ver_hash, name}),
    imports (list of {type, type_name, slot, plt, ver_hash, flags, name}),
    relocs (list of {type, type_name, offset, value}). Metadata segments are
    optional; a static SEF yields empty lists.
    """
    header, segments = read_segments(data)
    versym = bool(header["flags"] & SEF_FLAG_VERSYM)
    dynamic = bool(header["flags"] & SEF_FLAG_DYNAMIC)

    exports = []
    imports = []
    relocs = []
    if dynamic:
        for seg in segments:
            if seg["type"] == SEG_EXPORT:
                exports.extend(
                    decode_exports(data, seg["offset"], seg["size"], versym=versym)
                )
            elif seg["type"] == SEG_IMPORT:
                imports.extend(
                    decode_imports(data, seg["offset"], seg["size"], versym=versym)
                )
            elif seg["type"] == SEG_RELOC:
                relocs.extend(decode_relocs(data, seg["offset"], seg["size"]))

    return {
        "format": "SEF",
        "entry": header["entry"],
        "flags": header["flags"],
        "dynamic": dynamic,
        "versions": versym,
        "segments": [
            {
                "index": i,
                "type": type_name(seg["type"]),
                "vaddr": seg["vaddr"],
                "size": seg["size"],
                "offset": seg["offset"],
            }
            for i, seg in enumerate(segments)
        ],
        "total_mem": total_mem(segments),
        "exports": exports,
        "imports": imports,
        "relocs": relocs,
    }


def format_digest(info, compact=False):
    """Render a digest() dict as human-readable text (or a one-liner)."""
    if compact:
        return "exports=%d imports=%d relocs=%d" % (
            len(info["exports"]),
            len(info["imports"]),
            len(info["relocs"]),
        )

    out = []
    out.append("SEF digest (dynamic=%s versions=%s)" % (info["dynamic"], info["versions"]))
    for e in info["exports"]:
        if e["ver_hash"]:
            out.append("  export  0x%08X %s (ver 0x%X)" % (e["value"], e["name"], e["ver_hash"]))
        else:
            out.append("  export  0x%08X %s" % (e["value"], e["name"]))
    for imp in info["imports"]:
        extra = ""
        if imp["plt"]:
            extra += " plt=0x%X" % imp["plt"]
        if imp["ver_hash"]:
            extra += " ver=0x%X" % imp["ver_hash"]
        if imp["flags"] & SEF_IMPORT_WEAK:
            extra += " weak"
        out.append(
            "  import  %s slot=0x%X %s%s"
            % (imp["type_name"], imp["slot"], imp["name"], extra)
        )
    for r in info["relocs"]:
        out.append(
            "  reloc   %s off=0x%X val=0x%X" % (r["type_name"], r["offset"], r["value"])
        )
    for seg in info["segments"]:
        if seg["type"] in SEGMENT_TYPES and seg["type"] not in MEMORY_SEGMENTS:
            out.append(
                "  segment #%d %s size=%d" % (seg["index"], seg["type"], seg["size"])
            )
    return "\n".join(out)


def dump(data):
    """Pretty-print a SEF blob."""
    header, segments = read_segments(data)

    out = []
    out.append("SEF executable")
    out.append("  magic       0x%08X" % SEF_MAGIC)
    out.append("  entry       0x%X" % header["entry"])
    out.append("  segments    %d" % header["num_segments"])
    parts = []
    if header["flags"] & SEF_FLAG_DYNAMIC:
        parts.append("dynamic")
    if header["flags"] & SEF_FLAG_LAZY:
        parts.append("lazy")
    if header["flags"] & SEF_FLAG_VERSYM:
        parts.append("versions")
    out.append("  flags       0x%04X (%s)" % (header["flags"], ", ".join(parts)))
    out.append("")

    out.append("  %-3s %-9s %-10s %-8s %-9s" % ("#", "type", "vaddr", "size", "offset"))
    for i, seg in enumerate(segments):
        off = "-" if seg["type"] == SEG_BSS else "0x%X" % seg["offset"]
        out.append(
            "  %-3d %-9s 0x%-8X %-8d %-9s"
            % (i, type_name(seg["type"]), seg["vaddr"], seg["size"], off)
        )

    total = total_mem(segments)
    stack_top = (total + USER_STACK_SIZE) & ~0xF
    out.append("")
    out.append("  total mem   0x%X (%d bytes)" % (total, total))
    out.append("  gp          0x0 (base)")
    out.append("  sp          0x%X (base + total + 4096, 16-aligned)" % stack_top)

    if header["flags"] & SEF_FLAG_DYNAMIC:
        info = digest(data)
        if info["exports"] or info["imports"] or info["relocs"]:
            out.append("")
            out.append(format_digest(info))

    return "\n".join(out)


def pack_segments(segments, entry, flags=0):
    """Serialize validated segment descriptions into a SEF blob.

    Each segment is a dict with type ("text"/"data"/"bss" or numeric) and
    size (and vaddr).  TEXT/DATA segments carry `data` bytes; BSS segments
    carry a `size`.  Auto-layout assigns contiguous vaddrs when a segment
    has no explicit vaddr.
    """
    typed = []
    for seg in segments:
        stype = seg["type"]
        if isinstance(stype, str):
            norm = stype.lower()
            if norm in ("text", "t"):
                stype = SEG_TEXT
            elif norm in ("data", "d"):
                stype = SEG_DATA
            elif norm in ("bss", "b"):
                stype = SEG_BSS
            else:
                raise SefError("unknown segment type %r" % seg["type"])
        typed.append((stype, seg))

    if not typed:
        raise SefError("no segments to pack")

    # Assign vaddrs: explicit where given, otherwise contiguous in order.
    running = 0
    descs = []
    for stype, seg in typed:
        if stype == SEG_BSS:
            size = seg.get("size", len(seg.get("data", b"")))
        else:
            size = len(seg.get("data", b""))
        if size == 0:
            raise SefError("%s segment has zero size" % type_name(stype))
        vaddr = seg.get("vaddr")
        if vaddr is None:
            vaddr = running
        running = max(running, vaddr + size)
        descs.append(
            {"type": stype, "vaddr": vaddr, "size": size, "data": seg.get("data", b"")}
        )

    if len(descs) > SEF_MAX_SEGMENTS:
        raise SefError(
            "too many segments: %d (max %d)" % (len(descs), SEF_MAX_SEGMENTS)
        )

    total = running
    if total == 0:
        raise SefError("total segment size is 0")

    # Assign file offsets: after the header, payloads in segment order.
    offsets = []
    off = HEADER_SIZE + len(descs) * SEGMENT_SIZE
    for desc in descs:
        offsets.append(off)
        off += desc["size"]

    out = bytearray()
    out += struct.pack("<IIHH", SEF_MAGIC, entry, len(descs), flags)
    for desc, off in zip(descs, offsets):
        out += struct.pack("<IIII", desc["type"], desc["vaddr"], desc["size"], off)
    for desc in descs:
        out += desc["data"]

    return bytes(out)


def to_int(value):
    """Parse an int that may arrive as JSON int, bool, or hex string."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise SefError("cannot convert %r to integer" % (value,))


def pack(entry=0, flags=0, text=None, data=None, bss=None, spec=None):
    """Build a SEF blob from CLI/JSON inputs; return the blob bytes."""
    if spec:
        with open(spec, "r", encoding="utf-8") as f:
            spec_data = json.load(f)
        segments = []
        for seg in spec_data.get("segments", []):
            stype = seg["type"]
            if stype == "bss":
                data_bytes = b""
                size = to_int(seg.get("size", 0))
            else:
                with open(seg["file"], "rb") as fh:
                    data_bytes = fh.read()
                size_raw = seg.get("size", None)
                size = len(data_bytes) if size_raw is None else to_int(size_raw)
                if size != len(data_bytes):
                    raise SefError(
                        "%s segment size %d != file size %d (%s)"
                        % (stype, size, len(data_bytes), seg["file"])
                    )
            entry_seg = {
                "type": stype,
                "size": size,
                "data": data_bytes,
            }
            if "vaddr" in seg:
                entry_seg["vaddr"] = to_int(seg["vaddr"])
            segments.append(entry_seg)
        entry_raw = spec_data.get("entry", None)
        entry = 0 if entry_raw is None else to_int(entry_raw)
        flags_raw = spec_data.get("flags", None)
        flags = 0 if flags_raw is None else to_int(flags_raw)
        return pack_segments(segments, entry, flags)

    segments = []
    for path in text or []:
        with open(path, "rb") as fh:
            segments.append({"type": "text", "data": fh.read()})
    for path in data or []:
        with open(path, "rb") as fh:
            segments.append({"type": "data", "data": fh.read()})
    for size in bss or []:
        segments.append({"type": "bss", "size": size})

    return pack_segments(segments, entry, flags)


def print_findings(findings):
    for level, msg in findings:
        if level == "pass":
            marker = "PASS"
        elif level == "warn":
            marker = "WARN"
        else:
            marker = "FAIL"
        print("  %s  %s" % (marker, msg))


def cmd_pack(output, entry=0, flags=0, text=None, data=None, bss=None, spec=None):
    blob = pack(entry=entry, flags=flags, text=text, data=data, bss=bss, spec=spec)
    with open(output, "wb") as f:
        f.write(blob)
    header, segments = read_segments(blob)
    info = layout(header, segments)
    print(
        "Wrote %s: %d bytes, %d segments, entry=0x%X, flags=0x%X"
        % (output, len(blob), info["num_segments"], info["entry"], info["flags"])
    )
    return 0


def cmd_dump(input_path):
    with open(input_path, "rb") as f:
        data = f.read()
    print(dump(data))
    return 0


def cmd_check(input_path):
    with open(input_path, "rb") as f:
        data = f.read()
    findings = check(data)
    print("%s:" % input_path)
    print_findings(findings)
    return 1 if any(level == "fail" for level, _ in findings) else 0


def cmd_size(input_path):
    with open(input_path, "rb") as f:
        data = f.read()
    header, segments = read_segments(data)
    info = layout(header, segments)
    print("  file        %d bytes" % len(data))
    print("  segments    %d" % info["num_segments"])
    print("  total mem   0x%X (%d bytes)" % (info["total_mem"], info["total_mem"]))
    print("  entry       0x%X" % info["entry"])
    print("  flags       0x%04X" % info["flags"])
    print("  stack       0x%X (size %d)" % (info["sp"], info["stack_size"]))
    return 0


def cmd_digest(input_path, json_out=False):
    with open(input_path, "rb") as f:
        data = f.read()
    info = digest(data)
    if json_out:
        print(
            json.dumps(
                info,
                indent=2,
            )
        )
    else:
        print("%s:" % input_path)
        print("%s" % format_digest(info))
    return 0
