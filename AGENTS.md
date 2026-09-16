# Notes

## Sept 2026 `del` / `has` / `get_attr` language features

Three Python-flavored features shipped together (parser `DelStmt`, semantic
`_visit_del` + `_infer_has_get_attr`, codegen `emit_delstmt` +
`_emit_builtin_has_get_attr`, formatter `_emit_del`). CI corpus grew to 21/21;
`ci_bomb.py` stays 2500/2500.

- **`del` unbinds a variable or clears an addressable slot.**
  - `del name` on a **static** (non-heap) variable removes it from the whole
    scope chain (`_undefine_from_scope` in `_visit_del`, semantic) — later uses
    are clean `use of undeclared identifier` compile errors, and codegen pops
    it from `locals`/`local_types`/`ssa_values`/`const_vars`.
  - `del name` on a **promoted/`dynamic`** variable marks kind DYN_NONE at
    runtime (writes `{DYN_NONE,0}` into the `_DynValue` slot; falls back to
    the arena `assign(name, 0, 0)`).
  - `del name` on a **heap pointer** (name tracked in `self._heap_names`, from
    the module-level `_is_heap_alloc_expr` helper that gates `malloc/calloc/
    realloc` — the same helper the ownership checker uses) emits `free()` and
    also unbinds the name.
  - `del arr[i]` / `del obj.field` / `del *pp` **clear the slot**: typed
    zero/null store through `_emit_lvalue`. `del_kind` is stamped on the AST
    node by semantic ("static"/"dynamic"/"heap"/"slot") and consumed by
    `emit_delstmt` in `bytecoding.py`. `del` on a `const` or an undeclared
    name is a compile error; a deleted name can be re-declared later.
  - Ownership checker treats `del p` on an owned heap pointer like `free(p)`
    (no false leak/double-free warnings).
- **`has(...)` — Python-style existence checks.**
  - `has(x)` (bare name): compile-time bool when `x` is statically in/out of
    scope (const 1/0); runtime kind!=DYN_NONE check for promoted/`dynamic`
    vars. **An undeclared name is legal** and means false (this is the whole
    point) — so builtin `has` is intercepted in `_infer_type` BEFORE it can
    emit `use of undeclared identifier`, and `_infer_children` (semantic +
    bytecoding's `_emit_children`) returns `[]` for builtin has/get_attr args
    so the iterative deep-mode paths don't pre-visit the arg. Arg may also be a
    pointer (non-null test) or a `dynamic` value (set test).
  - `has(obj, "f")`: compile-time literal field-exists on any struct/class;
    `has(obj, name_expr)` with a runtime `str` is a strcmp dispatch over the
    known fields (i1 OR-chain, inline — no control flow).
- **`get_attr(obj, "f")` — read any field by name.**
  - Literal name on a known struct/class: static GEP + load (codegen builds a
    synthetic `Attr(meta["obj"], field)` and calls `emit_attr`), returns the
    field's real type. Works on struct values and `Struct*` pointers.
  - Runtime `str` name: strcmp dispatch selects the field index, then a
    two-stage GEP (`[0,0]` to the first field element pointer, then a runtime
    `gep(ptr, [idx])`) + load — llvmlite struct GEP cannot take a non-constant
    index, so the first index must be split out. **Requires all fields to
    share one type** (else a clean compile error with the conflicting types
    listed); a pointer target is loaded once via `_emit_lvalue` so `Vec3*`
    objects work.
- **Shadowable** like `range`/`len`: `def has(...)` / `def get_attr(...)`
  preempts the builtin (checked via scope lookup in semantic and
  `self.functions` in codegen). Both semantics and codegen gate on it.
- **Sharp edges.** `has`/`get_attr` 1-arg+2-arg shape and runtime-name type
  are validated in semantic with helpful notes. `(int)has(...)` prints `-1`
  for true (i1 sext cast quirk — pre-existing, unrelated). All older syntax
  is untouched; `del` is a new lexer keyword but no existing program used it.

## Sept 2026 `del`/`has`/`get_attr` bug-hunt round 2 (GC-new del, LICM removal, guards)

Adversarial fuzzing (`test/test_fuzz_del_has.py`, del/has/get_attr generators on
the base `fuzzer.py` engine, opt0 vs opt3 differential JIT, 7-way parallel)
hunted the feature; **every classified bug was fixed, `ci_bomb.py` stays
2500/2500 and CI corpus grew to 22/22 via `test/test_del_gc_new.cpy`.**

- **`del p` on a GC-managed `new` pointer must NOT `free()` (was a runtime
  SIGABRT).** Under the default GC build, `new T` lowers to `gc_malloc`
  (`_get_malloc_fn`), whose returned pointer is *interior* to the collector's
  `cpyte_obj_t` header — plain libc `free()` of it is an invalid free and
  aborts. User `malloc`/`calloc`/`realloc` calls are NOT affected (they lower
  to plain libc alloc even with the GC on). Fix in semantic: the new module
  helper `_is_gc_managed_expr` (NewExpr, unwrapping CastExpr) feeds the new
  `self._gc_new_names` set alongside `_heap_names` (both VarDecl and Assign
  sites). `_visit_del` stamps `del_kind="static"` (unbind-only, collector
  reclaims) when `not no_gc and name in _gc_new_names`; "heap" → `free()` is
  reserved for raw-malloc-origin pointers. Randomized reproducer:
  `test/crashes_del_has/crash_25420_0001.cpy`.
- **LICM hoisting removed entirely (bytecoding).** `_find_loop_invariants`
  (optimizations.py) was deleted — it was wrong: its name-based read/write sets
  missed memory effects and a hoisted `x = g(k)` moved a side-effecting `g`
  call out of a loop (output change). `emit_while` now imports it no more, no
  longer calls it, and the `hoisted_ids` skip-list in the body loop is gone.
  Only the conservative straight-line const-prop invalidation across the
  backedge remains (correct and regression-tested in `test_opt_alg.cpy`).
- **Struct values in boolean contexts are now clean compile errors.** The
  `not <struct>` / `<struct> and ...` / `<struct> or ...` / `while <struct>:`
  paths crashed codegen with `TypeError: 'int' object is not iterable` (an
  `ir.Constant(struct_type, 0)` construction). `_is_true` (bytecoding) and the
  `emit_unaryop` NOT branch now raise a clear `RuntimeError: cannot evaluate
  struct type ... as a boolean` — the CLI rejects, the fuzz harness classifies
  as a clean reject. Repro: `test/crashes_del_has/crash_25421_0001.cpy`.
- **`test_fuzz_del_has.py` classifier treats a trapped process as UB noise.**
  A negative exit code (SIGTRAP/SIGFPE from an LLVM div-by-zero guard or a
  poison shift) is the program's *own* undefined behavior — opt0 vs opt3 may
  legally place/fold the trap differently, so one-side-trap-with-output is NOT
  a miscompile. Only unequal outputs from two *surviving* runs are a bug; the
  leftover "bugs" in earlier blasts were these UB programs. Also: never count
  a multiprocessing-spawn import failure as a test failure — the worker must
  be importable under `spawn` (a missing symbol in `bytecoding`'s
  `optimizations` import once made every result "UB noise rc=1").
- **Files**: feature codegen `bytecoding.py` (`emit_delstmt`,
  `_emit_builtin_has_get_attr`, `_truthy_expr`/`_is_true` guards),
  `semantic_analasis.py` (`_visit_del` + GC-aware classification), plus the
  regression corpus additions under `test/test_del_has_*.cpy` and
  `test/test_del_gc_new.cpy`.

## Sept 2026 emitter optimization work (FP math, pow, unroll breadth)

- **int64/uint64 const-prop fold truncation bug (fixed).** When an
  `int64`/`uint64` variable was const-propagated into `_try_algebraic_simplify`,
  the substituted `Number` literal emitted at i32 width, and the integer
  constant-fold (`_fold_int_binop` + `_trunc_to_signed`) truncated the result to
  32 bits. Example: `int64 a = 104729; print(a * a)` produced `-1916738447`
  (i32 overflow) instead of `10968163441`. The POW strength-reduction lane
  (`**{0,1,2,3}`) had the same issue. Fix: track the declared local type width
  (`left_width`/`right_width`) at substitution time and use
  `max(emitted_width, left_width, right_width)` for the fold result width; widen
  the POW operand via `_extend_to_i64`. Plain `int` (i32) constants are
  unaffected (width 32 max). Regression test: `test/test_int64_fold.cpy` (added
  to CI corpus).

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

- **`_fold_int_binop` shift-overflow (fixed).** `left << right` on a
  const-prop'd `uint64` lane could shift by an astronomically large constant
  (e.g. `g1 << (g1 * g2)` with `uint64` operands), computing a 1.6e19-bit
  Python int and dying with `OverflowError: too many digits in integer`
  (crash reproducers in the fuzz corpus). Fix: `right >= 64` folds to `0`
  (any shift by >= 64 is 0 after truncation to the ≤ 64-bit emitted width);
  negative shifts already returned None. Bomb test back to 2500/2500.
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

## v4.1.0 release (Sept 2026, published to PyPI + github + gitea)

- Published as commit a67f338, tag `v4.1.0` (origin github.com/cpyte/cpyte +
  gitea.5gnew.io.vn/Cpyte-Project/Cpyte). Bump `pyproject.toml` version first,
  then `python -m build` (regenerates `source/cpyte.egg-info/`), `twine upload
  --repository pypi dist/cpyte-4.1.0.*` (reads token from `~/.pypirc`), then
  `twine upload --repository gitea dist/...`. Wheel smoke-tested in a fresh
  venv (`python -m venv %TEMP%\opencode\cpyte41venv`); install + compile/run of
  corpus programs confirmed the shipped artifact works standalone.
- **Gitea git push needs an explicit credential bypass**: `git push gitea` via
  the Git Credential Manager fails auth, but
  `git -c credential.helper= push https://duytung:Duytung%402015@gitea.5gnew.io.vn/Cpyte-Project/Cpyte.git <ref>`
  works (password is `Duytung@2015`, `@` must be URL-encoded as `%40`).
  `twine` is on PATH only as `python -m twine` in this environment.
- **Version is single-sourced from `__init__.py` now (fixed in v4.2.1).**
  `cpy --version` does `from cpyte import __version__` (mainpie.py:894), so the
  string in `source/cpyte/__init__.py` is the ONLY version you bump at release
  time. `pyproject.toml` carries `dynamic = ["version"]` +
  `[tool.setuptools.dynamic] version = {attr = "cpyte.__version__"}` to derive
  the wheel/egg-info version from that one string — never write a literal
  `version =` in pyproject again. The pre-4.2.1 drift (pyproject 4.1.0/4.2.0 vs
  `__init__.py` "3.4.0") shipped a broken 4.2.0 wheel whose `--version` reported
  3.4.0 and whose update-checker nagged users; verify the rebuilt wheel with
  `python -m build` then `unzip -p dist/*.whl cpyte/__init__.py` and
  `unzip -p dist/*.whl */METADATA | grep Version` and confirm both say the same
  version before `twine upload`.
- **Release hygiene**: `.gitignore` now excludes `dist/`, `build/`, and
  `test/crashes/*.o` / `*.gc.o` / `*.runtime.o` (the 2500×3 fuzz artifacts must
  NOT be committed; `examples/*.o` remain tracked binaries — leave them
  unstaged at release time).
- New GA workflow `.github/workflows/code_quality.yml` (ran `test` + `bomb` +
  `benchmarks` jobs) and `ci_bomb.py` (repo-root fuzz driver, 2500/2500) are
  now tracked and part of the release.

## v4.2.2 release (Sept 2026, bugfix)

- **AOT bigint link broke in `run_aot` (compiling.py).** The AOT path marked
  every bignum function `internal` (`_mark_internal`) BEFORE
  `binding.link_modules`. The program module pre-declares every `bigint_*`
  helper as external, and the LLVM IR linker treats an internal source
  definition plus a same-named external destination declaration as a conflict
  and DROPS the body. So every bignum function outside `_BIGNUM_KEEP`
  (e.g. `bigint_add`, `bigint_sub`, `bigint_pow`) emitted `define`-less in
  `program.o` → `link error: Undefined symbols ... _bigint_add`. The JIT path
  already had the fix (link first, then internalize, comment at compiling.py
  ~1215); `run_aot` did not. Fix mirrors the JIT: link bignum IR while
  external, record `src_names`, internalize survivors after linking, then
  `_prune_module`. Regression: `test_big_return_coerce.cpy` (AOT) was added to
  the CI corpus — `nm program.o | grep bigint` must show `_bigint_add` defined.
- **`return <int>` from a `big`/`ubig` function emitted `inttoptr` (SIGSEGV).**
  `emit_return` only had LLVM types: an int value and an `i8*` return type fell
  into the generic `inttoptr` branch, so `return n` treated the integer as a
  bignum pointer and crashed at runtime (JIT and AOT alike). The workaround
  (`big nn = n; return nn`) is why `test_basic_fib.cpy` never exposed it. Fix:
  track the current function's semantic return type in `self._func_rettype`
  (set + save/restore alongside `_in_decorator` in `emit_funcdef`), and in the
  return-coercion `IntType -> PointerType` branch promote via
  `_promote_to_big(value, inferred_type)` / `_promote_to_ubig` instead of
  `inttoptr`. Regression: `test/test_big_return_coerce.cpy` (direct
  `return n` int64/uint64 → big, + recursive big fib) — JIT and AOT both print
  `7 -7 18446744073709551615 1346269`.
- Release: v4.2.0 (45 files) → v4.2.1 (version single-source fix) → v4.2.2
  (the two fixes above). Published to PyPI + gitea (both whl+sdist, verified
  via pypi.org JSON `latest` and gitea `simple/cpyte/` HTML), tagged `v4.2.2`,
  pushed to `github` + `origin` (gitea, credential bypass). All remotes at
  `4eae54b`.

## v4.2.0/v4.2.1 release process recap (all stashes applied + published)

## v4.2.4 release (Sept 2026, Ubuntu build bugfixes)

- **`gc_runtime.c` missing `_GNU_SOURCE` broke Ubuntu builds.** The Linux
  stack-scan path calls `pthread_getattr_np` (gc_runtime.c ~310), a GNU
  extension that glibc only declares when `_GNU_SOURCE` is defined. Under a
  strict ISO C mode (or a `cc` that does not default to `-std=gnu*`), the
  declaration is hidden -> implicit-declaration error / build failure. Fix:
  guarded `#if !defined(_WIN32) && !defined(_GNU_SOURCE)` `#define _GNU_SOURCE 1`
  at the very top of `gc_runtime.c`, BEFORE any `#include`. The wheel ships
  `gc_runtime.c` verbatim so the fix reaches users on install.
- **JIT compiler probe did not validate the clang-only `-target` flag.**
  `_find_llvm_cc` (compiling.py) probed candidates with only
  `-S -emit-llvm -O0`, but every downstream runtime/bignum compile also passes
  `-target <host triple>` (compiling.py:1047/1131, mainpie.py:588). A
  gcc-family `cc` that squeaked past the `-emit-llvm` probe then died at the
  real invocation with `cc: error: unrecognized command-line option '-target'`.
  Fix: the probe now includes `-target host_target().triple`, so the selected
  compiler is guaranteed to accept the exact flag set actually used; on
  gcc-only Ubuntu the clean "install clang / use --aot" message is shown
  instead of a cryptic mid-compile failure.
- Release: v4.2.4. CI corpus 22/22; wheel verified in a fresh venv (JIT opt3 +
  AOT of `test/test_del_gc_new.cpy` both print `99 0 7 0 5`). Published to
  PyPI + gitea registry, tagged `v4.2.4`, pushed to `github` + `origin` (gitea,
  credential bypass).

## v4.2.0/v4.2.1 release process recap (all stashes applied + published)
