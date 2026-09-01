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

## Language caps verified for cache-friendly pool rewrite (Aug 2026)

- Typed pointer arrays WORK for heap storage: `int*`, `size_t*`, `char*`,
  and `char**` (array of `char*`) can be `(T*)malloc(n * (T)sizeof(T))`'d and
  indexed `a[i]`. `realloc`/`free` exist in `stdlib`.
- `continue` in loops and `+=` compound assignment for `size_t` are
  unverified — avoid `continue`; use explicit `x = x + one`.
- NO pointer arithmetic (`char* + n` is a semantic error). NO pointer→integer
  casts (`size_t p = (size_t)buf` silently yields 0). So unaligned wide copies
  and `buf+8` indexing are impossible; SIMD must come from the LLVM JIT's
  auto-vectorizer (thin affine loops), NOT from `ccode:` helpers (they do not
  codegen in imported modules — deque `to_list` is proof).
- trie/radix/suffix/dawg were rewritten to cache-friendly flat pools: each
  field is its own dense array (`nfirst/nterm/...`, `echild/enext/ech`, plus
  `elbl`/`elen` for compressed edges; DAWG also flat sig registry + `DWStk`),
  nodes/edges are integers, chains use `(size_t)0x7FFFFFFF` as the "no next"
  sentinel, pools double on push. Public APIs unchanged; leaves need explicit
  terminal=1 after node push (pool default is 0). Full `main.cpy` smoke green.
