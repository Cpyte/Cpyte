#!/usr/bin/env python3
"""Regression checks for compiler API and pointer arithmetic semantics."""

import os
import re
import struct
import sys
import tempfile

from llvmlite import binding as llvm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "source"))

from cpyte import CpyteCompileError, emit_program, parse_and_analyze  # noqa: E402
from cpyte import sef  # noqa: E402
from cpyte.package_manifest import iter_cpm_version_dirs, sort_cpm_versions  # noqa: E402


def compile_program(source):
    parsed, generic_instantiations, _ = parse_and_analyze(
        source, enable_extensions=False
    )
    module, _, _ = emit_program(
        parsed,
        generic_instantiations=generic_instantiations,
        enable_extensions=False,
    )
    llvm.parse_assembly(str(module)).verify()
    return str(module)


def test_extensions_disabled_api():
    ir = compile_program("public main() -> int:\n    return 0\n")
    assert 'define i32 @"main"' in ir


def test_integer_pointer_arithmetic():
    compile_program(
        "public main() -> int:\n"
        "    char* p = 0\n"
        "    int64 offset = 1\n"
        "    p = p + offset\n"
        "    p = offset + p\n"
        "    p = p - offset\n"
        "    int distance = p - p\n"
        "    return distance\n"
    )


def test_invalid_pointer_arithmetic_is_rejected():
    invalid_programs = (
        "public main() -> int:\n"
        "    char* p = 0\n"
        "    float offset = 1.5\n"
        "    p = p + offset\n"
        "    return 0\n",
        "public main() -> int:\n"
        "    char* p = 0\n"
        "    bool offset = true\n"
        "    p = p + offset\n"
        "    return 0\n",
        "public main() -> int:\n"
        "    char* p = 0\n"
        "    int offset = 1\n"
        "    p = offset - p\n"
        "    return 0\n",
    )
    for source in invalid_programs:
        try:
            parse_and_analyze(source, enable_extensions=False)
        except CpyteCompileError:
            continue
        raise AssertionError(f"invalid pointer expression was accepted:\n{source}")


def test_invalid_hex_literal_is_lexer_error():
    try:
        parse_and_analyze(
            "public main() -> int:\n    return 0x\n",
            enable_extensions=False,
        )
    except CpyteCompileError as exc:
        assert "requires at least one digit" in str(exc)
    else:
        raise AssertionError("0x without hexadecimal digits was accepted")


def test_cpm_versions_use_semver_order():
    versions = ["2.9.0", "2.10.0", "2.10.0-beta.2", "2.10.0-beta.11"]
    assert sort_cpm_versions(versions) == [
        "2.10.0",
        "2.10.0-beta.11",
        "2.10.0-beta.2",
        "2.9.0",
    ]
    with tempfile.TemporaryDirectory() as root:
        package = os.path.join(root, "pkg")
        for version in ("2.9.0", "2.10.0"):
            os.makedirs(os.path.join(package, version))
        result = list(iter_cpm_version_dirs(root))
        assert result[0][1].endswith(os.path.join("pkg", "2.10.0"))


def test_compound_index_assignment_evaluates_index_once():
    ir = compile_program(
        "def idx() -> int:\n"
        "    return 0\n"
        "public main() -> int:\n"
        "    int[2] values\n"
        "    values[idx()] += 1\n"
        "    return 0\n"
    )
    assert ir.count('call i32 @"idx"()') == 1


def test_zero_multiplication_preserves_call_side_effects():
    ir = compile_program(
        "def side() -> int:\n"
        "    print(7)\n"
        "    return 3\n"
        "public main() -> int:\n"
        "    return 0 * side()\n"
    )
    assert ir.count('call i32 @"side"()') == 1


def test_counted_loop_unrolling_preserves_integer_wraparound():
    ir = compile_program(
        "public main() -> int:\n"
        "    int i = 2147483647\n"
        "    while i <= 2147483647:\n"
        "        i = i + 1\n"
        "    return i\n"
    )
    assert 'while.cond:' in ir
    assert 'while.body:' in ir


def test_mul_power_of_two_strength_reduction_never_shifts_out_of_range():
    # ``x * (2**k ± 1)`` and ``x * 2**k`` are lowered to shifts. When ``k``
    # reaches the operand's bit width the shifted term is 0 modulo the type,
    # so the reduction must fall back to ``±x`` / ``0`` instead of emitting
    # ``shl iN %x, k`` with ``k >= N`` (an LLVM poison value). All the
    # multipliers below used to hit that path.
    ir = compile_program(
        "def by_pow2(x: int) -> int:\n"
        "    return x * 4294967296\n"  # 2**32 -> 0
        "def by_minus1(x: int) -> int:\n"
        "    return x * 4294967295\n"  # 2**32 - 1 -> -x
        "def by_plus1(x: int) -> int:\n"
        "    return x * 4294967297\n"  # 2**32 + 1 -> x
        "def by_valid(x: int) -> int:\n"
        "    return x * 2147483649\n"  # 2**31 + 1 -> valid shl by 31
        "public main() -> int:\n"
        "    return by_valid(3)\n"
    )
    for width, amount in re.findall(r"shl i(\d+) %[^,]+, (\d+)", ir):
        assert int(amount) < int(width), f"out-of-range shl i{width} by {amount}"
    # The k < width cases must still reduce to shifts, not be dropped.
    assert re.search(r"shl i32 %[^,]+, 31", ir)
    # 2**k - 1 becomes a plain negation at i32 width.
    assert "sub i32 0, %" in ir


def test_auto_type_inference_compiles():
    # `auto` infers its type from the initializer / return value; semantic
    # stamps the concrete type onto the AST before codegen, so these programs
    # lower to valid IR (an unresolved `auto` would fail llvm_type).
    ir = compile_program(
        "struct P:\n"
        "    int x\n"
        "class C:\n"
        "    int n\n"
        "    def __init__(n: int):\n"
        "        this.n = n\n"
        "    def get() -> auto:\n"
        "        return this.n\n"
        "def add(a: int, b: int) -> auto:\n"
        "    return a + b\n"
        "public def main() -> int:\n"
        "    auto i = 5\n"
        "    auto d = 3.5\n"
        "    auto s = \"hi\"\n"
        "    auto p = new P\n"
        "    auto c = new C(7)\n"
        "    auto t = add(1, 2)\n"
        "    print(i)\n"
        "    print(d)\n"
        "    print(s)\n"
        "    print(p.x)\n"
        "    print(c.get())\n"
        "    print(t)\n"
        "    return 0\n"
    )
    assert 'define i32 @"main"' in ir
    assert 'define i32 @"C.get"' in ir


def test_auto_inference_errors_are_diagnostics():
    invalid_programs = (
        (
            "public def main() -> int:\n    auto y\n    return 0\n",
            "needs an initializer",
        ),
        (
            (
                "def f(a: int) -> auto:\n"
                "    if a:\n"
                "        return 1\n"
                "    return \"x\"\n"
                "public def main() -> int:\n    return f(1)\n"
            ),
            "conflicting return types",
        ),
        (
            "struct S:\n    auto x\npublic def main() -> int:\n    return 0\n",
            "struct fields have no initializer",
        ),
        (
            "class C:\n    auto n\npublic def main() -> int:\n    return 0\n",
            "class fields have no initializer",
        ),
        (
            (
                "def f(x: auto) -> int:\n    return 0\n"
                "public def main() -> int:\n    return 0\n"
            ),
            "parameters have no initializer",
        ),
        (
            (
                "def nothing() -> void:\n    return\n"
                "public def main() -> int:\n"
                "    auto z = nothing()\n    return 0\n"
            ),
            "has no concrete type",
        ),
        (
            "public def main() -> int:\n    auto w = (auto)5\n    return 0\n",
            "cannot be used as a cast target",
        ),
    )
    for source, needle in invalid_programs:
        try:
            parse_and_analyze(source, enable_extensions=False)
        except CpyteCompileError as exc:
            assert needle in str(exc), f"{needle!r} not in:\n{exc}"
            continue
        raise AssertionError(f"invalid `auto` program was accepted:\n{source}")


def test_sef_checker_rejects_truncated_relocation_records():
    data = bytearray(
        sef.pack_segments(
            [
                {"type": "text", "data": b"\0" * 4},
                {"type": sef.SEG_RELOC, "data": b"\0" * 12},
            ],
            entry=0,
            flags=sef.SEF_FLAG_DYNAMIC,
        )
    )
    reloc_size_offset = sef.HEADER_SIZE + sef.SEGMENT_SIZE + 8
    size = struct.unpack_from("<I", data, reloc_size_offset)[0]
    data.append(0)
    struct.pack_into("<I", data, reloc_size_offset, size + 1)

    findings = sef.check(bytes(data))
    assert any(
        level == "fail" and "RELOC" in message and "multiple of 12" in message
        for level, message in findings
    )


if __name__ == "__main__":
    test_extensions_disabled_api()
    test_integer_pointer_arithmetic()
    test_invalid_pointer_arithmetic_is_rejected()
    test_invalid_hex_literal_is_lexer_error()
    test_cpm_versions_use_semver_order()
    test_compound_index_assignment_evaluates_index_once()
    test_zero_multiplication_preserves_call_side_effects()
    test_counted_loop_unrolling_preserves_integer_wraparound()
    test_mul_power_of_two_strength_reduction_never_shifts_out_of_range()
    test_auto_type_inference_compiles()
    test_auto_inference_errors_are_diagnostics()
    test_sef_checker_rejects_truncated_relocation_records()
    print("Compiler regression tests passed.")
