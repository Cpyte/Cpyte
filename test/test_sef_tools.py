#!/usr/bin/env python3
"""Self-contained regression test for the SEF tooling (source/cpyte/sef.py).

Builds synthetic SEF v2 blobs in memory (little-endian, exactly the layout in
WEW-scorpion/sef.h) and validates check()/digest()/dump() against the loader
semantics — including the v2.1 VERSYM record shapes, the dynamic-metadata
rules, and the absence of any privilege flag (real Scorpion removed it).

Run standalone:  python test/test_sef_tools.py
"""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "source"))

from cpyte import sef


def _align4(n):
    return (n + 3) & ~3


def build_synthetic(flags=sef.SEF_FLAG_DYNAMIC | sef.SEF_FLAG_VERSYM, entry=0):
    """Assemble a 6-segment dynamic SEF (TEXT, DATA, BSS, RELOC, IMPORT, EXPORT)."""
    text = b"\x13\x00\x00\x00\x97\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    data = b"\x00\x00\x00\x18\xff\xff\xff\x7f"

    reloc = b"".join(
        [
            struct.pack("<III", sef.SEF_R_CALL, 0, 0x44),
            struct.pack("<III", sef.SEF_R_RELATIVE, 4, 0x18),
        ]
    )

    def imp(rtype, slot, plt, ver_hash, imp_flags, name):
        nb = name.encode()
        return (
            struct.pack(
                "<IIIIII", rtype, slot, plt, ver_hash, imp_flags, len(nb)
            )
            + nb
            + b"\x00" * (_align4(len(nb)) - len(nb))
        )

    imports = b"".join(
        [
            imp(sef.SEF_R_CALL, 0, 0, 0x1234, 0, "foo"),
            imp(sef.SEF_R_CALL, 1, 0, 0, sef.SEF_IMPORT_WEAK, "bar"),
        ]
    )

    def exp(value, ver_hash, name):
        nb = name.encode()
        return (
            struct.pack("<III", value, ver_hash, len(nb))
            + nb
            + b"\x00" * (_align4(len(nb)) - len(nb))
        )

    exports = b"".join(
        [exp(0, 9, "add"), exp(8, 0, "sub")]
    )

    segments = [
        (sef.SEG_TEXT, 0, len(text)),
        (sef.SEG_DATA, len(text), len(data)),
        (sef.SEG_BSS, len(text) + len(data), 32),
        (sef.SEG_RELOC, 0, len(reloc)),
        (sef.SEG_IMPORT, 0, len(imports)),
        (sef.SEG_EXPORT, 0, len(exports)),
    ]

    # Assign file offsets: header(12) + descriptors(6*16), payloads in order.
    num = len(segments)
    blobs = [text, data, reloc, imports, exports]
    pi = 0
    off = 12 + num * sef.SEGMENT_SIZE
    descs = []
    for stype, vaddr, size in segments:
        if stype == sef.SEG_BSS:
            descs.append((stype, vaddr, size, 0))
            continue
        descs.append((stype, vaddr, size, off))
        payload = blobs[pi]
        assert len(payload) == size, (segments[pi], len(payload), size)
        off += size
        pi += 1

    blob = struct.pack("<IIHH", sef.SEF_MAGIC, entry, num, flags)
    for stype, vaddr, size, soff in descs:
        blob += struct.pack("<IIII", stype, vaddr, size, soff)
    blob += text + data + reloc + imports + exports
    return blob


def test_check_static_rejects_metadata():
    blob = build_synthetic(flags=0)
    findings = sef.check(blob)
    lines = [m for lvl, m in findings if lvl == "fail"]
    assert any("metadata segment in a static SEF" in m for m in lines)


def test_check_rejects_bad_layout():
    # Overlapping memory vaddr ranges must be flagged.
    blob = sef.pack_segments(
        [
            {"type": "text", "data": b"\x13\x00\x00\x00"},
            {"type": "text", "data": b"\x97\x00\x00\x00", "vaddr": 2},
        ],
        entry=0,
    )
    findings = sef.check(blob)
    assert any(
        "overlap in vaddr" in m for lvl, m in findings if lvl == "fail"
    )

    # Entry beyond memory is a hard failure.
    blob2 = sef.pack_segments(
        [{"type": "text", "data": b"\x13\x00\x00\x00"}], entry=0x1000
    )
    findings2 = sef.check(blob2)
    assert any(
        "beyond total memory" in m for lvl, m in findings2 if lvl == "fail"
    )


def test_check_dynamic():
    blob = build_synthetic()
    findings = sef.check(blob)
    failed = [msg for lvl, msg in findings if lvl == "fail"]
    assert not failed, "check failures: %s" % failed
    lines = [msg for lvl, msg in findings]
    assert any("dynamic SEF" in m for m in lines)
    assert any("total memory 56 bytes" in m for m in lines)
    assert any("RELOC): 2 record(s)" in m for m in lines)
    assert any("IMPORT): 2 record(s)" in m for m in lines)
    assert any("EXPORT): 2 record(s)" in m for m in lines)
    assert any("entry 0x0 inside TEXT" in m for m in lines)


def test_check_static():
    blob = sef.pack_segments(
        [
            {"type": "text", "data": b"\x13\x00\x00\x00"},
            {"type": "data", "data": b"\x00\x00\x00\x18"},
            {"type": "bss", "size": 32},
        ],
        entry=0,
    )
    findings = sef.check(blob)
    assert not [m for lvl, m in findings if lvl == "fail"]
    lines = [m for lvl, m in findings]
    assert any("static SEF" in m for m in lines)
    assert not any("metadata segment in a static SEF" in m for m in lines)


def test_digest_records():
    info = sef.digest(build_synthetic())
    assert info["dynamic"] is True
    assert info["versions"] is True
    assert info["total_mem"] == 56
    assert info["entry"] == 0

    assert [e["name"] for e in info["exports"]] == ["add", "sub"]
    assert info["exports"][0]["value"] == 0
    assert info["exports"][0]["ver_hash"] == 9
    assert info["exports"][1]["value"] == 8

    assert [i["name"] for i in info["imports"]] == ["foo", "bar"]
    assert info["imports"][0]["ver_hash"] == 0x1234
    assert info["imports"][1]["flags"] & sef.SEF_IMPORT_WEAK
    assert [r["type_name"] for r in info["relocs"]] == ["R_CALL", "R_RELATIVE"]

    compact = sef.format_digest(info, compact=True)
    assert compact == "exports=2 imports=2 relocs=2", compact

    full = sef.format_digest(info)
    assert "export  0x00000000 add (ver 0x9)" in full
    assert "import  R_CALL slot=0x0 foo ver=0x1234" in full
    assert "weak" in full


def test_dump_table():
    out = sef.dump(build_synthetic())
    assert "SEF executable" in out
    assert "dynamic, versions" in out
    assert "total mem   0x38 (56 bytes)" in out
    assert "RELOC" in out and "IMPORT" in out and "EXPORT" in out


def test_no_privilege_flag():
    """Real Scorpion removed privilege flags; a SEF never declares its own."""
    assert not hasattr(sef, "SEF_FLAG_PRIV_CONTROLLER")
    for value_name in dir(sef):
        if value_name.startswith("SEF_FLAG_"):
            assert getattr(sef, value_name) & 0x0001 == 0, (
                "%s uses the removed privilege bit 0x1" % value_name
            )


def main():
    tests = [
        test_check_dynamic,
        test_check_static,
        test_check_static_rejects_metadata,
        test_check_rejects_bad_layout,
        test_digest_records,
        test_dump_table,
        test_no_privilege_flag,
    ]
    for fn in tests:
        fn()
        print("PASS %s" % fn.__name__)
    print("\nAll %d sef tool tests passed." % len(tests))
    return 0


if __name__ == "__main__":
    sys.exit(main())