#!/usr/bin/env python3
"""Regression checks for compiler API and pointer arithmetic semantics."""

import ctypes
import hashlib
import os
import re
import struct
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from llvmlite import binding as llvm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "source"))

from cpyte import CpyteCompileError, emit_program, parse_and_analyze  # noqa: E402
from cpyte import compiling, sef  # noqa: E402
from cpyte.package_manifest import (  # noqa: E402
    cpm_module_roots,
    iter_cpm_version_dirs,
    reset_global_registry,
    sort_cpm_versions,
)


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


def _write_cpm_package(root, package_path, name, version, source=None):
    version_dir = os.path.join(root, package_path, version)
    os.makedirs(version_dir, exist_ok=True)
    with open(os.path.join(version_dir, "package.json"), "w") as f:
        f.write(f'{{"name": "{name}", "version": "{version}"}}')
    if source is not None:
        with open(os.path.join(version_dir, f"{name}.cpy"), "w") as f:
            f.write(source)


def test_cpm_module_roots_include_global_and_workspace():
    with tempfile.TemporaryDirectory() as tmp:
        workspace = os.path.join(tmp, "project")
        home = os.path.join(tmp, "home")
        roots = cpm_module_roots(workspace_root=workspace, home=home)
        assert roots == [
            os.path.join(workspace, ".cpm", "modules"),
            os.path.join(home, ".cpm", "modules"),
        ]
        # Without a workspace, a source file next to a .cpm dir is used as the
        # project-local root; the global root is still searched afterwards.
        filedir = os.path.join(tmp, "src")
        roots = cpm_module_roots(filedir=filedir, home=home)
        assert roots == [
            os.path.normpath(os.path.join(filedir, "..", ".cpm", "modules")),
            os.path.join(home, ".cpm", "modules"),
        ]


def _parse_with_roots(source, roots, filepath=None, workspace_root=None):
    """Analyze ``source`` with CPM roots replaced by ``roots``."""
    def roots_for(*_args, **_kwargs):
        return roots

    with (
        patch("cpyte.mainpie.cpm_module_roots", roots_for),
        patch("cpyte.semantic_analasis.cpm_module_roots", roots_for),
    ):
        reset_global_registry()
        try:
            parsed, generics, _ = parse_and_analyze(
                source,
                filepath=filepath,
                workspace_root=workspace_root,
                enable_extensions=True,
            )
            module, _, _ = emit_program(
                parsed,
                generic_instantiations=generics,
                enable_extensions=True,
            )
            return str(module)
        finally:
            reset_global_registry()


def test_global_cpm_import_resolves_without_workspace():
    with tempfile.TemporaryDirectory() as tmp:
        global_root = os.path.join(tmp, "global_modules")
        _write_cpm_package(
            global_root,
            os.path.join("@std", "demo"),
            "demo",
            "1.0",
            "public def demo_value() -> int:\n    return 7\n",
        )
        # Extension-only global packages must import cleanly too.
        _write_cpm_package(
            global_root, os.path.join("@std", "extonly"), "extonly", "1.0"
        )
        _write_cpm_package(
            global_root,
            "plain",
            "plain",
            "1.0",
            "public def plain_value() -> int:\n    return 3\n",
        )

        source = (
            'import "@std/demo"\n'
            'import "@std/extonly"\n'
            'import "plain"\n'
            "public def main() -> int:\n"
            "    return demo_value() + plain_value()\n"
        )
        ir = _parse_with_roots(
            source, [global_root], filepath=os.path.join(tmp, "probe.cpy")
        )
        assert 'define i32 @"demo_value"' in ir
        assert 'define i32 @"plain_value"' in ir


def test_workspace_cpm_package_wins_over_global_and_dot_form_imports():
    with tempfile.TemporaryDirectory() as tmp:
        global_root = os.path.join(tmp, "global_modules")
        workspace_root = os.path.join(tmp, "project")
        local_root = os.path.join(workspace_root, ".cpm", "modules")
        _write_cpm_package(
            global_root,
            os.path.join("@std", "demo"),
            "demo",
            "1.0",
            "public def global_demo() -> int:\n    return 1\n",
        )
        _write_cpm_package(
            local_root,
            os.path.join("@std", "demo"),
            "demo",
            "2.0",
            "public def local_demo() -> int:\n    return 2\n",
        )

        source = (
            'import "@std.demo"\n'
            "public def main() -> int:\n"
            "    return local_demo()\n"
        )
        ir = _parse_with_roots(
            source,
            [local_root, global_root],
            filepath=os.path.join(workspace_root, "probe.cpy"),
            workspace_root=workspace_root,
        )
        assert 'define i32 @"local_demo"' in ir
        assert "global_demo" not in ir


def test_deep_std_extension_import_is_still_skipped():
    # `@std.<pkg>.<sub>` is resolved at runtime; it must not be reported as a
    # missing CPM package, unlike the two-segment `@std.<pkg>` package form.
    parse_and_analyze(
        'import "@std.math.foo"\npublic def main() -> int:\n    return 0\n',
        enable_extensions=False,
    )


def test_lsp_import_completions_include_global_cpm_packages():
    try:
        from cpyte import lsp_server
    except ImportError:  # pragma: no cover - optional editor dependency
        return
    with tempfile.TemporaryDirectory() as tmp:
        global_root = os.path.join(tmp, "global_modules")
        _write_cpm_package(
            global_root, os.path.join("@std", "lspdemo"), "lspdemo", "1.0", ""
        )
        def roots_for(*_args, **_kwargs):
            return [global_root]

        with patch("cpyte.lsp_server.cpm_module_roots", roots_for):
            labels = [
                item.label
                for item in lsp_server._import_dir_completions("@std/", None, None)
            ]
        assert any("lspdemo" in label for label in labels)


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


def test_logical_constants_short_circuit_without_duplicate_calls():
    ir = compile_program(
        "def side() -> int:\n"
        "    return 1\n"
        "public main() -> int:\n"
        "    print(0 and side())\n"
        "    print(1 and side())\n"
        "    print(1 or side())\n"
        "    print(0 or side())\n"
        "    return 0\n"
    )
    # Only the two reachable RHS expressions should remain in the IR.
    assert ir.count('call i32 @"side"()') == 2


def test_string_concat_uses_native_memcpy_intrinsic():
    ir = compile_program(
        "def prefix(s: str) -> str:\n"
        '    return "prefix" + s\n'
        "public main() -> int:\n"
        '    print(prefix("x"))\n'
        "    return 0\n"
    )
    assert "llvm.memcpy" in ir
    assert "str_concat_const_gc" not in ir


def test_gc_manages_compiler_generated_arrays():
    ir = compile_program(
        "public main() -> int:\n"
        "    int[] values = new int[4]\n"
        "    append(values, 9)\n"
        "    return values[0]\n"
    )
    assert 'call i8* @"gc_array_alloc"' in ir
    assert 'call i8* @"gc_array_reserve"' in ir


def test_counted_loop_unrolling_preserves_integer_wraparound():
    ir = compile_program(
        "public main() -> int:\n"
        "    int i = 2147483647\n"
        "    while i <= 2147483647:\n"
        "        i = i + 1\n"
        "    return i\n"
    )
    assert "while.cond:" in ir
    assert "while.body:" in ir


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
        '    auto s = "hi"\n'
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
                '    return "x"\n'
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


def test_optimizer_pipeline_and_loop_execution():
    ir = """
        define i64 @accumulate(i64 %n, ptr %count) {
        entry:
            store i64 0, ptr %count
            br label %condition
        condition:
            %i = phi i64 [0, %entry], [%next, %backedge]
            %sum = phi i64 [0, %entry], [%total, %backedge]
            %more = icmp slt i64 %i, %n
            br i1 %more, label %body, label %exit
        body:
            %product = mul i64 %i, 17
            %offset = add i64 %product, 3
            %value = srem i64 %offset, 97
            %total = add i64 %sum, %value
            %next = add i64 %i, 1
            store i64 %next, ptr %count
            %stop = icmp eq i64 %next, 113
            br i1 %stop, label %early, label %backedge
        backedge:
            br label %condition
        early:
            ret i64 %total
        exit:
            ret i64 %sum
        }
    """
    cases = (
        (0, False, 0, 0, 0),
        (1, False, 1, 0, 0),
        (2, False, 1, 0, 1),
        (3, False, 2, 1, 2),
        (0, True, 1, 1, 0),
        (2, True, 1, 1, 1),
        (3, True, 1, 1, 1),
    )
    for level, size, defaults, customs, functions in cases:
        mod = llvm.parse_assembly(ir)
        mod.verify()
        with (
            patch.object(
                llvm.PassBuilder,
                "getModulePassManager",
                autospec=True,
                side_effect=llvm.PassBuilder.getModulePassManager,
            ) as default_pipeline,
            patch.object(
                llvm,
                "create_new_module_pass_manager",
                wraps=llvm.create_new_module_pass_manager,
            ) as custom_pipeline,
            patch.object(
                llvm.PassBuilder,
                "getFunctionPassManager",
                autospec=True,
                side_effect=llvm.PassBuilder.getFunctionPassManager,
            ) as function_pipeline,
            patch(
                "cpyte.extension_hooks.get_global_hook_registry",
                return_value=SimpleNamespace(get=lambda stage: []),
            ),
        ):
            compiling.optimize(mod, level, opt_size=size)
            assert default_pipeline.call_count == defaults, (level, size)
            assert custom_pipeline.call_count == customs, (level, size)
            assert function_pipeline.call_count == functions, (level, size)
        mod.verify()
        with llvm.create_mcjit_compiler(
            mod, compiling.make_target_machine(opt=level)
        ) as engine:
            engine.finalize_object()
            accumulate = ctypes.CFUNCTYPE(
                ctypes.c_int64, ctypes.c_int64, ctypes.POINTER(ctypes.c_int64)
            )(engine.get_function_address("accumulate"))
            for n in (-1, 0, 1, 17, 112, 113, 114, 1000):
                count = ctypes.c_int64(-1)
                iterations = min(max(n, 0), 113)
                expected = sum((i * 17 + 3) % 97 for i in range(iterations))
                assert accumulate(n, ctypes.byref(count)) == expected, (level, size, n)
                assert count.value == iterations, (level, size, n)


def test_optimized_runtime_cache_revision():
    content = b"int answer(void) { return 42; }\n"
    triple = compiling.host_target().triple
    key = hashlib.sha1(content + triple.encode() + b"-O0").hexdigest()
    raw_ir = "; raw runtime\ndefine i32 @answer() { ret i32 42 }\n"
    legacy_ir = "; old optimizer\ndefine i32 @answer() { ret i32 1 }\n"
    with tempfile.TemporaryDirectory() as root:
        src = Path(root) / "runtime.c"
        src.write_bytes(content)
        cache = Path(root) / "cpyte_rtcache"
        cache.mkdir()
        raw_path = cache / (key + ".ll")
        raw_path.write_text(raw_ir)
        legacy_path = cache / (key + ".opt3o0.ll")
        legacy_path.write_text(legacy_ir)
        with (
            patch("tempfile.gettempdir", return_value=root),
            patch.object(
                compiling.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout=raw_ir),
            ) as clang,
            patch.object(compiling, "optimize", wraps=compiling.optimize) as optimize,
        ):
            for level in (None, 0):
                result, error = compiling._cached_c_ir(
                    "clang", str(src), triple, jit_opt_level=level
                )
                assert result == raw_ir and error is None
            clang.assert_not_called()
            optimize.assert_not_called()
            for level, size in ((3, False), (2, False), (3, True)):
                result, error = compiling._cached_c_ir(
                    "clang",
                    str(src),
                    triple,
                    jit_opt_level=level,
                    jit_opt_size=size,
                )
                assert error is None and result not in (raw_ir, legacy_ir)
                llvm.parse_assembly(result).verify()
                optimize.assert_called_with(
                    optimize.call_args.args[0], level, opt_size=size
                )
                assert "ret i32 42" in result
                suffix = (
                    f".opt{level}o{int(size)}.r{compiling._OPTIMIZER_CACHE_REVISION}.ll"
                )
                assert (cache / (key + suffix)).read_text() == result
                calls = clang.call_count, optimize.call_count
                warm, error = compiling._cached_c_ir(
                    "clang",
                    str(src),
                    triple,
                    jit_opt_level=level,
                    jit_opt_size=size,
                )
                assert error is None and warm == result
                assert (clang.call_count, optimize.call_count) == calls
            assert clang.call_count == optimize.call_count == 3
            with patch.object(
                compiling,
                "_OPTIMIZER_CACHE_REVISION",
                compiling._OPTIMIZER_CACHE_REVISION + 1,
            ):
                compiling._cached_c_ir("clang", str(src), triple, jit_opt_level=3)
            assert clang.call_count == optimize.call_count == 4
        assert raw_path.read_text() == raw_ir
        assert legacy_path.read_text() == legacy_ir


def test_binary_operators_associate_left_except_power():
    """Same-precedence chains must fold left; only ``**`` folds right.

    The recursive Pratt parser used to pass the operator's own precedence to
    its right-hand side, so ``a - b + c`` built ``a - (b + c)``. The iterative
    deep-expression fallback already added the ``+ 1``, which meant the two
    parsers produced different trees for the same source.
    """
    from cpyte.astparse import BinOp, parse_expression
    from cpyte.lexar import Lexer

    def shape(src):
        """(left op right) written back out as a string."""

        def render(node):
            if isinstance(node, BinOp):
                return f"({render(node.left)} {node.op.name} {render(node.right)})"
            return getattr(node, "name", None) or getattr(node, "value", None)

        return render(parse_expression(Lexer(src).tokens, 0)[0])

    assert shape("a - b + c") == "((a MINUS b) PLUS c)"
    assert shape("a - b - c") == "((a MINUS b) MINUS c)"
    assert shape("a * b / c") == "((a STAR b) SLASH c)"
    assert shape("a / b * c") == "((a SLASH b) STAR c)"
    assert shape("a - b + c - d") == "(((a MINUS b) PLUS c) MINUS d)"
    assert shape("1 << 3 >> 2") == "((1 SHL 3) SHR 2)"

    # precedence still dominates associativity
    assert shape("a - b * c") == "(a MINUS (b STAR c))"
    assert shape("a + b | c") == "((a PLUS b) PIPE c)"

    # `**` is the one right-associative operator
    assert shape("2 ** 3 ** 2") == "(2 POW (3 POW 2))"

    # the recursive and iterative parsers must agree on associativity
    from cpyte.astparse import _parse_expr_iterative

    deep = "(" * 200 + "a - b + c" + ")" * 200
    deep_tokens = Lexer(deep).tokens
    # start inside the run of parens, at the first token of the inner expression
    offset = next(
        i for i, t in enumerate(deep_tokens) if getattr(t, "value", None) == "a"
    )
    iterative = _parse_expr_iterative(deep_tokens, offset, 0)[0]

    def render(node):
        if isinstance(node, BinOp):
            return f"({render(node.left)} {node.op.name} {render(node.right)})"
        return getattr(node, "name", None) or getattr(node, "value", None)

    assert render(iterative) == "((a MINUS b) PLUS c)"


if __name__ == "__main__":
    test_extensions_disabled_api()
    test_integer_pointer_arithmetic()
    test_invalid_pointer_arithmetic_is_rejected()
    test_invalid_hex_literal_is_lexer_error()
    test_cpm_versions_use_semver_order()
    test_cpm_module_roots_include_global_and_workspace()
    test_global_cpm_import_resolves_without_workspace()
    test_workspace_cpm_package_wins_over_global_and_dot_form_imports()
    test_deep_std_extension_import_is_still_skipped()
    test_lsp_import_completions_include_global_cpm_packages()
    test_compound_index_assignment_evaluates_index_once()
    test_zero_multiplication_preserves_call_side_effects()
    test_counted_loop_unrolling_preserves_integer_wraparound()
    test_mul_power_of_two_strength_reduction_never_shifts_out_of_range()
    test_auto_type_inference_compiles()
    test_auto_inference_errors_are_diagnostics()
    test_sef_checker_rejects_truncated_relocation_records()
    test_optimizer_pipeline_and_loop_execution()
    test_optimized_runtime_cache_revision()
    test_binary_operators_associate_left_except_power()
    print("Compiler regression tests passed.")
