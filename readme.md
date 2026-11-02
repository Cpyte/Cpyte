NOTICE: It seems cpyte has been going through a lot of changes, including the OOP work which was quite fabulous and python-like. I liked how the syntax changes very interestingly and made the language better. However, after a lot of experimentation and many breaking changes, I've decided that the syntax lock is now settled. The language is being stabilized, and development will slow down as we focus on reliability and polish instead of constant churn. After that, improvements will be delivered through the @std/improvements package with the extension hook framework.

Check out the official documentation [here](https://gitea.5gnew.io.vn/Cpyte-Project/Cpyte/src/branch/main/source/cpyte/cpy_language_documentation.md).

## Package Extensions

Cpyte supports a package extension system that allows packages to extend the compiler with custom keywords, operators, and compiler hooks. Packages can provide:

- **Custom Keywords**: Add new language keywords via `package.json`
- **Custom Operators**: Define new operators for syntax extensions
- **Compiler Hooks**: Extend lexing, parsing, semantic analysis, and code generation
- **Runtime Extensions**: Add runtime code and libraries

### Package Structure

Packages are stored in `.cpm/modules/package_name/version/` and can include:

- `package.json` - Extension manifest declaring capabilities
- `parser_hooks.py` - Custom syntax extensions
- `semantic_hooks.py` - Custom type checking and analysis rules
- `codegen_hooks.py` - Custom LLVM IR generation
- `runtime_hooks.py` - Runtime code and libraries
- `*.cpy` - Main package entry point
- `*.ll` - Prebuilt LLVM IR

### Example Package

See `examples/example_package/` for a complete example package with:
- Custom keywords: `async`, `await`, `defer`
- Custom operator: `~~`
- Parser, semantic, codegen, and runtime hooks

### Using Extension Packages

```cpy
import @package_name

# Use custom keywords and syntax from the package
async def my_function() -> Promise:
    # Custom syntax
    defer cleanup()
    return result
```

## Examples

You'll find comprehensive examples in the `examples/` directory that cover C imports, header imports, 64-bit support, and standard library usage:

| File | Description |
|------|-------------|
| `examples/c_import_example.cpy` | Importing and calling functions from C source files |
| `examples/example_math.c` | C source with math utility functions for the C import example |
| `examples/h_import_example.cpy` | Importing functions declared in C header files |
| `examples/example_functions.h` / `example_functions.c` | Header and implementation for the H import example |
| `examples/64bit_example.cpy` | Comprehensive `int64`/`uint64` arithmetic, hex literals, type promotion |
| `examples/c_library_imports.cpy` | Using built-in C libraries (stdio, stdlib, math, string, time) |
| `examples/mixed_features.cpy` | Combines C imports, H imports, 64-bit, structs, pointers, linked lists |
| `examples/mixed_helpers.c` / `mixed_helpers.h` | Supporting C/header files for the mixed features example |
| `examples/test.cpy` | macOS event tap example that intercepts keyboard events using ApplicationServices framework |
| `examples/example_package/` | Example package demonstrating extension capabilities with custom keywords, operators, and compiler hooks |

You can also import `.cpy` files — the examples show how to use `import "other.cpy"` to bring in public functions and structs from other Cpy files.

Run any example with:
```bash
cpy examples/<filename>.cpy
```

## Note
Cpyte is experimental software. The compiler is continuously tested with fuzzing and a regression corpus, and every release is published to PyPI and Gitea.

## Memory Management

Cpyte uses a **concurrent tri-color garbage collector** for automatic memory management. Heap-allocated objects (via `new`) are managed automatically — no manual `free` needed.

**Note:** The collector adds a small runtime overhead (~5-10%) compared to manual `malloc/free`. For performance-critical ecosystem projects or embedded use cases, this trade-off may be significant. The GC is required for safety but will slow down programs that do heavy heap allocation.

## Cross-Platform Runtime

The compiler is fully cross-platform. On every target OS (macOS, Linux, Windows) the C runtime (`runtime.c`), the GC runtime (`gc_runtime.c`), and any embedded `ccode:` blocks are **compiled from source** at JIT/AOT time for the host platform, rather than linking pre-built, OS-specific bitcode. This keeps native helpers and the garbage collector correct on each system:

- Portable threading: `pthread` on POSIX; native Windows threads + `CRITICAL_SECTION` on Windows.
- Portable stack scanning for the GC that works on macOS, Linux, and Windows.
- A portable sleep/yield helper replaces the POSIX-only `nanosleep`.

The compiler auto-discovers a C compiler (`clang` → `cc` → `gcc`) and system linker on the current platform, so `cpy`, `cpy build`, and `cpy --aot` all work without per-OS configuration.

## Continuous Integration

GitHub Actions (see `.github/workflows/code_quality.yml`) builds and runs a curated, cross-platform regression corpus on Ubuntu (x86_64 + arm64), macOS (x86_64 + arm64), and Windows. `ci_test.py` AOT-compiles each program with the system linker and executes the result to exercise the full pipeline — lexer, parser, semantic analysis, LLVM codegen, the C and GC runtimes, and linking.

## CI Benchmark Results

The table below is updated by the benchmark CI job on `main`. It is a separate
measurement from any historical benchmark results and is collected on the
dedicated benchmark runner.

<!-- CPYTE_CI_BENCHMARKS_START -->
Latest run: [1856104da4ed](https://github.com/Cpyte/Cpyte/actions/runs/41477481889) · updated 2026-11-02 16:06 UTC

| Benchmark | C | Cpy | Python | Cpy/C | Python/C |
|---|---:|---:|---:|---:|---:|
| `array_sum` | 6.72ms | 6.66ms | 244.54ms | 0.99x | 36.38x |
| `bitwise_mix` | 2.84ms | 3.01ms | 673.79ms | 1.06x | 237.42x |
| `branch_mix` | 4.40ms | 5.11ms | 443.42ms | 1.16x | 100.82x |
| `call_overhead` | 2.88ms | 3.03ms | 346.82ms | 1.05x | 120.41x |
| `fib_iterative` | 16.71ms | 16.98ms | 10.985s | 1.02x | 657.26x |
| `fib_recursive` | 163.45ms | 1.14ms | 7.291s | 0.01x | 44.61x |
| `float_math` | 6.07ms | 6.14ms | 255.00ms | 1.01x | 42.01x |
| `integer_mix` | 3.50ms | 3.50ms | 388.09ms | 1.00x | 110.83x |
| `loop_sum` | 961.82us | 1.05ms | 2.882s | 1.09x | 2996.43x |
| `matrix_mult` | 4.07ms | 4.40ms | 1.809s | 1.08x | 444.32x |
| `mem_alloc` | 948.52us | 992.25us | 17.60ms | 1.05x | 18.55x |
| `prime_count` | 67.00ms | 77.74ms | 1.787s | 1.16x | 26.67x |
| `quicksort` | 3.68ms | 2.61ms | 29.52ms | 0.71x | 8.02x |
| `string_concat` | 993.49us | 1.19ms | 25.70ms | 1.19x | 25.87x |

Process wall time includes startup. These are measurements from the dedicated `cpyte-official-runner-1`, not the historical benchmark figures elsewhere in this README; results can vary with host load and software versions.
<!-- CPYTE_CI_BENCHMARKS_END -->
