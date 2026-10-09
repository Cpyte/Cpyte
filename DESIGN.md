# Cpyte design: metadata tables + borrow state + staged roadmap

This document sketches a concrete, low-cost path to expand OOP and make borrow/mut tractable. Keep "runtime minimal, table-driven" in mind.

## 1) Class metadata table (ABI)

Goal: centralize per-class facts in a single constant global emitted once per module/program.

Proposed structure (C/LLVM view):
- `class.meta.<cls>` (global, struct, read-only after codegen)
  - `classid: i64` (already have; keep)
  - `name: i8*` (mangled or C-string; for diagnostics, `str(obj)` notes, etc)
  - `size_bytes: i64` (sizeof(Class struct, including vptr if virtual)
  - `is_virtual: i1`
  - `is_sealed: i1`
  - `is_dataclass: i1`
  - `vt_ptr: i8*` or pointer to vtable type (or null if sealed monomorphic)
  - `anc_count: i32` + `anc_ids` slice? Or just keep `anc.grid` (we already have per-id row)
  - `prop_count: i32`, `method_count: i32` (optional)
  - `flags: i64` (bitmask for future)

Notes:
- Existing `anc.grid` is O(1) for `isinstance/as` — keep it; class.meta can mirror.
- Per-class record enables: `type(obj)`, class name in errors, `__repr__` helpers, interface membership checks, memory dumps (zero extra runtime hot paths except where requested).
- Emit after all classdefs/imports, same time as `emit_ancestor_grid` / vtable globals. IdentifiedStructType or a LiteralStructType; C-string literals.

## 2) Protocol/interface table (OOP expansion)

Represent `interface I: ...` as:
- A protocol identity `proto.id.I` with method slots in a canonical order (name, sig key).
- Per-class `class.impls.<cls>` bitmask/index list of implemented protocols (or build a global `proto<->classes` set only if needed).
- For `isinstance(obj, I)` where I is an interface/protocol: cheap O(1) test via a per-protocol satisfied array indexed by classid? Alternatively `class.meta` has `impl_mask` or a per-protocol global `mask_I` of length `max_classid+1` bits — sparse small (classes ≤ 10^3).
- Prefer compile-time: if static type already known to implement, constant True. For runtime, `anc.grid`-style is easy to extend.

Constraints: keep zero-alloc in hot paths; avoid dictionary lookups at runtime.

## 3) Vtable properties & remaining dunder protocols

Properties:
- Today property access is monomorphic static calls (`Class.prop.get/set`). To allow overriding in subclasses (virtual properties), put property getter/setter fnptr slots into the vtable (or a parallel "prop vtable"). 
- Min change: reserve a small property-slot region after method slots; subclass overrides write different fnptrs.
- Backward compatible: sealed classes need no vtable.

Remaining dunders:
- `__iter__` → `iter(obj)` returns iterator (separate class or generator object)
- `__next__` on iterator
- `__enter__/__exit__` for with-context (AST support + codegen)
- `__bool__` affects truthiness in `if/while/not` (compile-time gate + runtime call)
- `__hash__` (value-based hashing)
- `__del__` (destructor hook) — be careful with GC and finalization order
- `__repr__` vs `__str__`: `print` currently doesn't use `__str__`; wire `str(obj)` builtin (done?) and consider `print` to call `str` when object has `__str__`.

Wire via `emit_*` builtin paths that consult static class + vtable (mirrors `len`/`has`/`__call__`).

## 4) Borrow/mut: scoped static checker (phase A)

Replace `_check_ownership` ad-hoc state with lexical scoping + path-sensitivity where easy.

Data model (per function, compile-time):
- `scope_stack` of frames; each frame holds `names: set[str]`, `state[name] in {free, owned(heap), moved, plain}`, and `borrows: {name -> {ro[], mut[]}}` with borrower identities (block/expr ids)
- Push on `{ ... }` blocks; pop on exit. 
- For loops/if/else: analyze both arms; on join, conservatively OR "tainted" states (moved/free-after-move) or error if arms disagree on critical states.
- `borrow x` → add ro to x's borrows (lifetime = current scope or until end of statement that uses the borrow?); `borrow mut x` → require no other borrows active, add mut.
- `move x` → set x to moved (and forbid re-borrow/move)
- `free(x)` → require owned; set to freed; forbid use
- Writes (`assign`, `x.f = ...`, `x[i]=...`, `*p=...`): if target is immutably borrowed → warn/error as now; also track through aliases? (limitation)
- Return values: treat returned names as escaping (don't warn leak if returned) — already partial.
- Scope termination: at end of block, any active mutable borrow ends? Any immutable borrow ends? End-of-function: report leaks for owned+not-escaped+not-freed.

First, minimal correct win: proper block scoping (pop borrows/state on block exit) — fixes false positives/negatives from today.

## 5) Borrow/mut: runtime-checked debug mode (--safe) (phase B)

Add `--safe` build flag. When set:
- Emit per-object borrow metadata word (8 bytes) for heap-allocated objects that can be borrowed (`new T`): `state = FREE|RO|MUT (2 bits), ro_count (30), gen (32)` or simple. For stack objects, can skip or use shadow.
- `borrow mut p` → check no RO/MUT, set MUT, record borrower id.
- `borrow p` → check no MUT, inc RO, record.
- borrow end (implicit at scope end) → dec/clear (need scope markers; can use RAII-style call pairs emitted at block boundaries: `_borrow_end_ro(n, id)` — cheap)
- writes through borrowed path: check; `move`/`free`: checks.
- Checks are `if (bad) trap` with diagnostic string — elided entirely when `!safe` (compile-time constant branch or don't emit).

Keep layout: borrow word placed just before/after GC header? Or in a separate side table indexed by object address (hash table) — side table avoids changing object layout, easier to mix with GC. Side table: `map<void*, borrow_word>` small.

Release: `--safe` off → borrow/mut becomes compile-time hints only (or ignored); ownership warnings still gated by GC mode.

## 6) Stage plan (smallest steps first)

1. Land pending (ci_bomb + diag script + deps) — done conceptually.
2. Design spec review (this file) — commit to repo (DESIGN.md or AGENTS.md section).
3. OOP A: add class.meta global + emit; wire name/size where useful (no behavior change).
4. OOP B: interface/protocol parsing + minimal `isinstance` for protocols (table-driven).
5. OOP C: property vtable slots (overridable properties).
6. OOP D: remaining dunders (`__bool__`, `__iter__/__next__`, `__enter__/__exit__`, `__hash__`, `__del__`); `print(obj)` uses `__str__`.
7. Borrow A: scoped static checker (block push/pop, proper lifetime end) — replace _check_ownership core.
8. Borrow B: alias-awareness light (AddrOf/BorrowExpr chains) with conservative rules.
9. Borrow C: `--safe` runtime checks (side table) — optional, behind flag.

Acceptance: each step CI corpus green + new targeted tests.
