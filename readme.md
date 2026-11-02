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
Latest run: [956536c464f2](https://github.com/Cpyte/Cpyte/actions/runs/41481876710) · updated 2026-11-02 16:38 UTC

| Benchmark | C | Cpy | Python | Cpy/C | Python/C |
|---|---:|---:|---:|---:|---:|
| `array_sum` | 7.80ms | 7.81ms | 264.99ms | 1.00x | 33.98x |
| `bitwise_mix` | 2.97ms | 3.08ms | 774.67ms | 1.04x | 260.66x |
| `branch_mix` | 4.55ms | 6.08ms | 478.29ms | 1.34x | 105.03x |
| `call_overhead` | 3.27ms | 3.72ms | 384.05ms | 1.14x | 117.45x |
| `fib_iterative` | 17.01ms | 17.26ms | 12.635s | 1.01x | 742.83x |
| `fib_recursive` | 168.59ms | 1.24ms | 8.082s | 0.01x | 47.94x |
| `float_math` | 6.23ms | 6.48ms | 272.36ms | 1.04x | 43.74x |
| `integer_mix` | 3.67ms | 3.75ms | 413.98ms | 1.02x | 112.83x |
| `loop_sum` | 967.00us | 1.14ms | 3.060s | 1.18x | 3164.88x |
| `matrix_mult` | 4.56ms | 4.58ms | 1.976s | 1.00x | 433.27x |
| `mem_alloc` | 995.00us | 1.14ms | 22.62ms | 1.15x | 22.73x |
| `prime_count` | 68.56ms | 79.39ms | 2.073s | 1.16x | 30.23x |
| `quicksort` | 2.38ms | 2.90ms | 39.64ms | 1.22x | 16.62x |
| `string_concat` | 1.11ms | 1.27ms | 32.52ms | 1.15x | 29.30x |

Process wall time includes startup. These are measurements from the dedicated `cpyte-official-runner-1`, not the historical benchmark figures elsewhere in this README; results can vary with host load and software versions.
<!-- CPYTE_CI_BENCHMARKS_END -->
