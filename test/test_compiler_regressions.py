#!/usr/bin/env python3
"""Regression checks for compiler API and pointer arithmetic semantics."""

import os
import sys

from llvmlite import binding as llvm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "source"))

from cpyte import CpyteCompileError, emit_program, parse_and_analyze  # noqa: E402


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


if __name__ == "__main__":
    test_extensions_disabled_api()
    test_integer_pointer_arithmetic()
    test_invalid_pointer_arithmetic_is_rejected()
    print("Compiler regression tests passed.")
