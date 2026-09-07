# Notes

## v2.7.3 ast parse syntax error (line ~635) — TODO: yank

- Reported: `ast.parse` had a syntax error around line 635 in the v2.7.3 (HEAD
  commit ddf5d8e) code.
- Could not be reproduced on this tree under Python 3.14 — all modified files
  (`astparse.py`, `bytecoding.py`, `semantic_analasis.py`, `_runtime_bc.py`,
  `compiling.py`, `mainpie.py`, `formatter.py`) pass `ast.parse`.
- The uncommitted working tree removes the iterative-parser list-literal
  duplication from v2.7.3: the `_parse_expr_iterative` LBRACKET branch is
  replaced by a shared `_parse_list_literal` helper used by both
  `_parse_expr_iterative` and `_parse_atom`. Verified under Python 3.14:
  deep-nested (depth > 120) expressions containing list literals parse via the
  iterative fallback, and all dynamic probes (dyn3/5/6/7) + fuzz (40 programs,
  seed 2026) pass. The v2.7.3 duplicate should not be re-introduced.

## Language & SIMD capabilities guide (post Sept 2026 fixes)

This repo is a compiler (`source/cpyte/`) that lowers cpyte (a C-like,
Python-flavored language) to LLVM. These are the concrete, verified
capabilities and the sharp edges to program around. Full `main.cpy` + CI
corpus (8/8) green on macOS arm64 after these changes.

### SIMD / CPU features — NOW AVAILABLE
- `cpyte build`, `cpyte --aot`, and the JIT all create their LLVM target
  machine from the **host CPU + features** (`make_target_machine` in
  `compiling.py`), so SIMD/NEON/AVX2 come from LLVM's SROA + auto-vectorizer,
  NOT from the source text. Verify with `otool -tV <bin> | grep -E "add\.4s|add\.2d|ld1"`.
- `cpyte build` now runs the optimizer (LLVM default module pipeline) even
  without `--lto`; `-O0` (via `--opt 0`) still opts out for exact debug builds.
- Override the CPU/features globally: `--cpu <name>` (or `--march`, e.g.
  `skylake`, `apple-m1`, `native`) and `--mattr <feats>` (e.g. `+avx2,+fma`).
  These call `set_target_cpu(...)` in `compiling.py`. The `run_scorpion`
  riscv32 path ignores them (fixed triple).
- The auto-vectorizer only fires on **thin affine loops** over contiguous
  buffers. Keep loops flat; avoid `continue`; use `x = x + one` instead of
  `+=`.

### Pointer arithmetic — NOW AVAILABLE
- `char* + n`, `char* - n`, `int* + n` etc. are supported (byte-offset
  arithmetic via `ptrtoint`). `char* - char*` yields the byte difference.
- `str` is a **distinct semantic type** from `char*` (though both lower to
  `i8*`): `str + str` is string concatenation (`_is_string_concat` gates it
  before `_promote`), while `char* + int` is pointer arithmetic. `str`-only
  arithmetic on non-string operands is still rejected.
- `(size_t)ptr` and `(ptr)(size_t)` round-trips preserve the full 64-bit
  address — `size_t` now lowers to `i64` (was accidentally `i32`, truncating
  heap addresses). Buffer editing is therefore possible: `(char*)malloc(n)`,
  `p[i]`, `memcpy`, etc.

### ccode:/llvm: blocks — NOW codegen in imported modules
- Embedded `ccode:` and `llvm:` blocks now propagate through `import`:
  `_import_cpy` re-registers their symbols on the importing analyzer and keeps
  the nodes in `sub_ast`, and `emit_program` emits both `CCode` and `Llvm`
  blocks **before** all function bodies (not only `CCode` as before). Call
  helpers defined in an imported module's `ccode:`/`llvm:` directly.
- `char**` (array of char*), typed pointer arrays (`int*`, `size_t*`),
  `(T*)malloc(n * (T)sizeof(T))`, indexing `a[i]`, `realloc`/`free` (from
  `stdlib`) all work for cache-friendly heap pools.
- Known sharp edges: no pointer arithmetic on `str` (it is not a raw buffer);
  avoid `continue` in hot loops; `+=` on `size_t` is fine but explicit
  `x = x + one` is the safest form.

### Cache-friendly pool pattern (flat pools)
- Each field lives in its own dense array (`nfirst/nterm/...`,
  `echild/enext/ech`, plus `elbl`/`elen` for compressed edges; DAWG adds a flat
  sig registry + `DWStk`), nodes/edges are integers, chains use
  `(size_t)0x7FFFFFFF` as the "no next" sentinel, pools double on push.
- Public APIs unchanged; leaves need explicit `terminal=1` after node push
  (pool default is 0).

### Whole-module compile + cpy sharp edges (discovered building stdlib/math)
- **`public` functions called from another function MUST be defined BEFORE the
  caller** — no forward references are resolved, even across `public` defs in
  the same or imported module. Order callees before callers or you get
  `use of undeclared identifier`.
- **`_switchable_if` is float-safe now (was a crash bug).** Previously
  `emit_if` auto-converted any `if <double-var> == <number>: ... else:/elif: ...`
  chain into an LLVM `switch`, emitting `switch i1` (the var coerced to bool)
  with the `double` constants as case values -> invalid IR
  (`case value is not a constant integer`), breaking whole-module builds because
  the JIT compiles ALL public functions of imported modules. Fixed: the
  `Variable` compares are checked for a `float`/`double` inferred type, and such
  chains now bail to the normal `fcmp` path (no switch). The old workaround
  (compare against a pre-declared local `double zero = 0.0`) is no longer
  required; int/enum chains still lower to `switch`.
- **Whole-module compile**: every `public` function in an imported `.cpy` is
  lowered. A bad construct anywhere (even in uncalled funcs) breaks the whole
  import. Keep every function clean.
- **Parse errors hide from `grep " error "`**: a syntax error during import
  raises `cpyte.astparse.ParseError` as an uncaught traceback and the consumer
  reports a cascade of `use of undeclared identifier` (because the file failed
  to parse). When a consumer says a `public` symbol is undeclared, first check
  the imported file for a PARSE error, not a semantic one.
- **`result` is special only inside decorator functions.** The `result` name
  and the `code()` builtin are routed through the compiler's internal
  `@__code_result`/`@__code_fn` slots **only when the current function returns
  `decorated`** (i.e. it is a decorator factory). Both semantic analysis
  (`_in_decorator`) and codegen (`_in_decorator` in `bytecoding.py`) scope the
  special-casing to that context. Outside a decorator, `result` is an ordinary
  identifier (a struct variable, accumulator, etc.) and `code()` is not a
  builtin. Do NOT rename real variables that happen to be called `result`; the
  old workaround ("rename the accumulator") is no longer required.
- **Cross-module struct field access is fragile.** Field-accessing a struct
  returned by a function works only when the user module *directly* imports
  the module that *defines* the struct (e.g. `SparseMatrix` from `sparse.cpy`).
  If a module (e.g. `transforms.cpy`) imports the definer and returns the
  struct, a consumer can't field-access the result (`cannot access field 'x'
  on non-struct type 'Vector3'`), and adding a direct import of the definer
  next to it triggers `struct.X is already defined` (redefinition). The stdlib
  avoids this by keeping modules independent (self-contained files). Prefer
  writing multi-value results into a caller-provided `double*` buffer and
  keeping modules scalar-only instead of returning structs across files.
- No Python ternary (`x if c else y`), list comprehensions, or lambda in cpy;
  use plain if/else. `math` functions (`sqrt`, `fabs`, `pow`) work in
  expressions but keep them out of parser-fragile positions if unsure.
- Debug workhorse: `/tmp/dbganalyze.py <file.cpy>` prints
  `=== result: True/False n_diags: N ===` + `[line:col]` warnings. Parse errors
  surface as a traceback; run it and read the full output.

### Codegen hardening (codegen fallbacks → hard errors; struct types; div by const 0)
- **Identified structs must be created via `module.context.get_identified_type(name)`,
  never `ir.IdentifiedStructType(ir.global_context, ...)`.** A bare
  `IdentifiedStructType` is never registered in the module's
  `context.identified_types`, so `str(module)` omits the `= type {...}` body and
  the LLVM parser treats the struct as opaque — every GEP/alloc fails
  (`base element of getelementptr must be sized`, `Cannot allocate unsized
  type`). `get_identified_type` registers + dedups by name. `emit_program`
  pre-registers all `StructDef`s opaque BEFORE emitting (so self-referential
  fields like `BSTNode* left` resolve to a pointer to the same type);
  `emit_structdef`/`emit_classdef` then `set_body` when `is_opaque`. Sections
  using the global context also collide across modules JITted in one process.
- **Unresolved types are hard errors now.** `llvm_type`'s fallbacks raise a
  codegen error naming the type instead of silently returning `i32` (that
  silently corrupted struct field layouts — e.g. `BSTNode` fields lowered to
  `i32`). Same for unresolvable generic instantiations (`Pair<int>` arity
  mismatch) and multi-dim `_bc_array_norm` edge cases.
- **Integer division by constant zero emits a clean unconditional trap** with
  the builder parked in a fresh dead block (value is never used). Do not emit
  `sdiv x, 0` in a dead block to keep well-formedness (it verifies, but is
  poison-by-construction). `_emit_int_divmod` already handles INT_MIN/-1 via a
  phi and power-of-two/magic-multiplier strength reduction.

### Memory-check warnings (semantic_analasis.py `_check_ownership`)
- Walks `malloc`/`calloc`/`realloc` as owned heap allocations (not just `new`),
  so the AGENTS-endorsed `(char*)malloc(n)` + `free()` pool pattern does NOT
  false-warn. Emits: use-after-free (read/store through a freed pointer),
  double-free, `free()` after `move`, freeing a never-allocated value,
  reassignment/overwrite leaks, and end-of-function leak warnings (skips names
  that escape via `return`). All gated on `#nogc` for the leak class.

### Codegen fallback / reviewed-bug notes (Sept 2026)
- `_is_type`/codegen now reuses `inferred_type` on `Attr`/`Deref`/`AddrOf` nodes
  (slots added in `astparse.py`; set in semantic analysis) instead of failing to
  recognize struct field expressions of big/ubig/array type.
- `_emit_children` (iterative engine) covers `CastExpr` (→ `[expr]`) and
  `ListLit` (→ `list(items)`); this fixed `test_opt_fib` (`[71 x i64]`)
  truncation errors.
- `strcmp` global scanning no longer raises `UnboundLocalError` when no
  pre-existing `strcmp` function is found — it synthesizes one.
- `_bc_array_norm` strips ALL consecutive `[N]` suffixes (`int[10][20]` →
  `i32**`), not just one.

### ubig (unsigned arbitrary-precision int) — NEW (Sept 2026)
- `ubig` is a **distinct unsigned** sibling of `big`: sign-magnitude BigNum
  with the sign always 0, evaluated by `bigint_*` magnitude ops. `ubig` is not
  a lexer keyword — it's a bare identifier type parsed via `_TYPE_NAMES`
  (`astparse.py`), like `big`, and lowers to `i8*` (`bigint_from_str`/etc.).
- Literals >2^64, `+ - * / // % **`, and comparisons use magnitude semantics;
  subtraction is **underflow-checked** (`ubigint_sub` aborts at runtime with
  `ubig underflow: result of subtraction is negative`). Division/mod are
  truncation-based (non-negative operands), so `bigint_div`/`bigint_mod` are
  reused.
- Bitwise `& | ^ << >>` work over the **full magnitude** via the new runtime
  functions `ubigint_and/or/xor/shl/shr` (`bignum.c`); signed `big` still
  rejects bitwise.
- No minus: `-x` and `ubig x = -5` are compile errors; `--` decrement is
  rejected. A negative runtime `big` assigned into `ubig` is not sign-checked
  (treats the magnitude) — keep values non-negative.
- Conversions: int/int64/uint64/size_t/big → ubig and ubig → big are valid
  (zero-extend via `bigint_from_uint64`); unbox/int()/float()/print/str()
  treat ubig like big. Casts `(ubig)expr`/`(big)expr` promote via
  `_promote_to_ubig`/`_promote_to_big` (never inttoptr).
- Runtime lives at the end of `bignum.c`; the JIT recompiles it from source so
  no `.bc` regeneration is needed. Regression test: `test/test_ubig.cpy`.

**Two backend bugs to avoid re-introducing (fixed):**
- **`big` literals in (2^63, 2^64)** (e.g. `12345678901234567890`) used to
  sign-extend into a *negative* big: `_promote_to_big` always called
  `bigint_from_int`. It now checks the source's `inferred_type` and uses
  `bigint_from_uint64` for `uint64`/`size_t`/`ubig` sources. Every `_promote_to_big`
  call site (casts, assignments, var decls, arithmetic operands) passes
  `getattr(..., "inferred_type", None)`.
- **`bigint_pow` SIGBUS**: `bignum.c` declared `BigNum exp_copy;` uninitialized on
  the stack and called `_bn_reserve(&exp_copy, ...)`, which `realloc`s a garbage
  `limbs` pointer. Initialize it (`BigNum exp_copy = {0};`) or `a ** b` with a
  multi-limb base/exp crashes (e.g. `int((ubig)2**2 ** ...)`). JIT recompiles
  `bignum.c` from source so no artifact rebuild is needed.

### stdlib/math package layout (in WEW-stdlib)
- `scalar.cpy`, `vector2/3/4/n.cpy`, `matrix.cpy` (dense), `matrix2/3/4.cpy`
  (fixed-size), `sparse.cpy` (CSR/COO + SpMV + iterative solvers),
  `interpolate.cpy` (linear/bilinear/trilinear/Lagrange/cubic-hermite/splines),
  `transforms.cpy` (polar/spherical/cylindrical + change of basis),
  `discrete.cpy`, `main.cpy` aggregator.
- Dense `matrix.cpy` core (verified): LU, QR (+MGS), Cholesky, LDL, inverse,
  det, trace, rank, norm, condition, SVD, symmetric eigen (Jacobi), solve/
  solve_spd/least-squares, polar, Hessenberg, bidiagonal, general eigen (shifted
  QR), generalized eigen, power iteration, and iterative solvers CG/Jacobi/
  Gauss-Seidel/SOR/Richardson/BiCGSTAB/GMRES/MINRES. Validate with
  `tmp_validate.cpy`.
