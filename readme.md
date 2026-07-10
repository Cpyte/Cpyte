Please go to the official documentation at ['cpy_languages_syntax'](https://gitea.5gnew.io.vn/duytung/Cpyte/src/commit/f11c87dc17517156b3f6bd276b4c0d78cd82370c/source/cpyte/cpy_language_documentation.md)

VERSION 1.6 had a codegen problem related to null pointers.

## Examples

Comprehensive examples demonstrating C imports, header imports, 64-bit support, and standard library usage are available in the `examples/` directory:

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

You can also import `.cpy` files — see `examples/` for examples of `import "other.cpy"` importing public functions and structs from other Cpy files.

Run any example with:
```bash
python source/mainpie.py --jit examples/<filename>.cpy
```
