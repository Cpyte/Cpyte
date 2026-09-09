# Notes

## Sept 2026 emitter optimization work (FP math, pow, unroll breadth)

- **FP identity/strength-reduction + double const-prop now fire at every
  `--opt` level** (`_try_algebraic_simplify` in `bytecoding.py`). New
  double/float lane runs alongside the integer lane: literal const folding on
  `+ - * /` (div-by-0 skipped), `x*1.0 -> x`, `x*-1.0 -> -x`
  (`_emit_fneg`, mul-by--1 = exact sign xor; `fsub(0,x)` would NOT be, it
  breaks `-0.0`), `x/1.0 -> x`, `x/-1.0 -> -x`, `x-0.0 -> x`. NOT folded (in
  each case rounding/NaN/signed-zero unsafe): `x+0.0`, `x*0.0`, `x-x`,
  `x/0.0` (frem/`//` too). `pow`/`**` is strength-reduced BEFORE the libm
  call for const exponents: int lanes `x**{0,1,2} -> 1/x/x*x` and `x**3 ->
  x*x*x` (exact integer arithmetic, replaces the old sitofp/pow/fptosi double
  round-trip), fp lanes `x**0.0->1.0`, `x**1.0->x`, `x**2.0->x*x`, `x**0.5->
  llvm.sqrt.f64` (intrinsic, no libm), `x**-1.0->1.0/x`. `x**3.0` (fp) is
  deliberately NOT folded (1-ulp); negative int exponents keep the float
  path (semantics of the old fptosi).
- **`double`/`float` constants are tracked (`_const_prop_f`)** alongside int
  const-prop: `double two = 2.0; d * two; pow(d, two)` substitute the literal
  so the FP folds apply. `_set_const_prop` now invalidates the FP entry too,
  and `emit_while` mirrors the `_loop_written_names` save/restore for
  `_const_prop_f` (the stale-FP-constant-across-a-loop bug that `d2 = d2 +
  1.0` inside a `while` would otherwise have produced is regression-tested in
  `test_opt_fp.cpy`: `d2 * 3.0` after the loop prints 15, not stale 6).
- **Counted-loop unrolling now survives intervening constants.** The
  single `pending_iv` tuple in `emit_funcdef` is a `pending_ivs` DICT
  (`name -> start`); `int i = 0; int total = 0; while i < 5:` unrolls (the
  old code cleared on the second VarDecl). Non-`Number` VarDecls (incl.
  `int* p = &i`) and every `While` clear the whole dict, so addr-taken ivs
  still never unroll. `_try_unroll_counted_loop` takes the dict and looks the
  iv up by name.
- **`while 0:` skips emission entirely** (`emit_while` tops out on a literal
  `0` cond); `while j < 0` also short-circuits via the existing n==0 unroll
  path. Regression suite: `test_opt_fp.cpy` (opt3 == opt0, 9 golden values).

## Sept 2026 bug-hunt findings (structural hazards in optimizations.py / bytecoding.py)

- **LICM was silent dead code; now it runs.** `_find_loop_invariants` used
  `_collect_assigned_names` (top-level targets only), so every hoist candidate's
  own target was always in `mutated` -> `[]` forever. Additionally
  `_get_used_variables` read `expr._children`, which does NOT exist on AST
  classes (`__slots__ = ("_token","inferred_type",...)`), so it returned an
  empty set for non-Variable expressions. Both were rewritten as deep stack
  walkers; `_collect_assigned_names` is now dead code (left in place).
- **LICM must not hoist side-effecting calls or memory reads.** Hoisting
  `x = g(k)` out of a loop where `g` does `print` changed output (verified
  miscompile: prints once instead of 8x). `_find_loop_invariants` now rejects
  any RHS containing `Call`/`Deref`/`Attr`/`Index`/`NewExpr`/`InlineAsm`
  (`_expr_may_violate_invariance`). Regression: `test_opt_alg.cpy` `tick` block.
- **Unrolling walks through aliasing writes.** Unroll stores canonical counter
  values into the iv alloca; a loop body that does `*p = ...`/`a[i] = ...`/
  `obj.field = ...` (or takes `&x`) could have those writes clobbered.
  `_try_unroll_counted_loop` now bails when `_loop_has_alias_risk(body)`
  (Deref/AddrOf/Attr/Index/NewExpr/Borrow/Move anywhere in the body).
- Owl-eyed honesty check that saved two false reproductions: for
  `int* p = &i; while i < 2: *p = *p + 3; i = i + 1`, the *intervening*
  `int* p = &i` VarDecl already breaks the "VarDecl immediately before the
  loop" pending-iv machinery, so no unroll happened; and `//` is FLOOR division
  (`7 // -4 == -2`), not trunc. Negative literal divisors also never reach the
  pow2 const path at runtime (`-4` lowers to `sub i32 0, 4`, so the divisor is
  not a constant -> plain `srem`, which is C-truncation-correct).

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
  (`_in_decorator`) and codegen gate the special-casing on the factory context
  (`_is_decorator_factory` in `bytecoding.py`, set only for `rettype ==
  "decorated"` — the decorated ORIGINAL function is emitted with this flag
  `False`, so its body never misroutes ordinary locals named `result`/`args`).
  Outside a decorator, `result` is an ordinary identifier (a struct variable,
  accumulator, etc.) and `code()` is not a builtin. Do NOT rename real
  variables that happen to be called `result`; the old workaround ("rename the
  accumulator") is no longer required.
- **Decorator access variabables `args` / `func_name` / `skip` (Sept 2026).**
  A decorator factory body (`-> decorated`, using `@name` on a func) can
  additionally read **`args`** (the decorated function's incoming arguments as
  dynamic values; `int a = args[i]` unboxes) and **`func_name`** (the
  decorated function's name as a `str`), and can write **`skip = true`** to
  make the wrapper return the default value **without calling the original**.
  Routing: the wrapper boxes its incoming args into a runtime buffer and stores
  a `DynValue*` into global `@__code_args`, stores the name into
  `@__code_fn_name` and `0` into `i1 @__code_skip` BEFORE running the
  decorators; `args`/`func_name`/`skip` in the factory read/write those
  globals at runtime (mirroring the existing `@__code_result`/`@__code_fn`
  pattern). The trampoline (`__name_trampoline`) has a **fixed `() -> DynValue`
  signature** — it pulls the args back out of `@__code_args` itself and unboxes
  them (via `dyn_as_v` + `_unbox_dyn`) where the param types ARE static, so
  `code()` never needs to know the decorated signature. Regression:
  `test/test_decorator_access.cpy` (paramful `add(a,b)` + `skipcompute`;
  prints `add`/`28`/`0`); `test_decorator.cpy` still prints `52`. The semantic
  "must call code()" check is a **deep walk** now (a `code()` call nested under
  `if`/`while`/`try` satisfies it), so a factory can legitimately gate the
  original call behind a condition and use `skip = true` otherwise. Note:
  multiple decorators are not Python-chained — each re-calls the original via
  the trampoline and the last decorator's `result` wins.
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
