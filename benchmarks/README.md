# Benchmarks

Run all complete C/Cpy/Python benchmark triplets with:

```sh
python3 benchmarks/run_benchmarks.py
```

For more accurate raw runtime without GC overhead, use manual memory mode:

```sh
python3 benchmarks/run_benchmarks.py --nogc
```

For CPU time measurements (more accurate on Unix, excludes I/O wait):

```sh
python3 benchmarks/run_benchmarks.py --cpu-time
```

The runner discovers matching `.c`, `.cpy`, and `.py` files by basename,
compiles C with Clang `-O3` and Cpy through the AOT compiler, then compares
program output before reporting median time. It performs three warmups and
fifteen measured runs by default; `--trials`, `--warmups`, and `--filter` adjust
the run. `--json PATH` saves raw per-trial samples.

Each timing includes process startup and output capture. Run the suite without
other CPU-intensive workloads for more stable results. Comparisons are rejected
if any language prints a different result. Workloads are intended to exercise
distinct areas, not to provide a complete language shootout; in particular,
runtime, compiler, and allocator behavior can differ between languages.

The suite currently covers recursive and iterative Fibonacci, primality,
matrix multiplication, virtual/function-pointer calls, quicksort, string
concatenation, allocation/free, array traversal, branch-heavy integer work,
integer arithmetic, bitwise work, floating-point arithmetic, and loop throughput.

All Cpyte benchmarks use manual memory management (via `#nogc` directive or
`--nogc` flag) to ensure fair comparison with C's manual memory management.

**Note on fib_recursive**: The Cpyte compiler has a built-in optimization that
replaces naive recursive Fibonacci with a lookup table (up to F(70)) plus
iterative fast-doubling for larger values. This makes the `fib_recursive`
benchmark significantly faster than the naive C implementation and is **not**
a fair algorithm comparison. This benchmark tests the compiler's optimization
capabilities rather than raw recursion performance.

The GitHub Actions benchmark job uses the dedicated
`cpyte-official-runner-1` self-hosted runner. On successful pushes to `main`,
it updates the separate **CI Benchmark Results** table in the root `readme.md`
and pushes that README update back to the branch. This latest-run table is
distinct from historical benchmark measurements.
