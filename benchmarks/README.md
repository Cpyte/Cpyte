# Benchmarks

Run all complete C/Cpy/Python benchmark triplets with:

```sh
python3 benchmarks/run_benchmarks.py
```

The runner discovers matching `.c`, `.cpy`, and `.py` files by basename,
compiles C with Clang `-O3` and Cpy through the AOT compiler, then compares
program output before reporting median wall time. It performs one warmup and
seven measured runs by default; `--trials`, `--warmups`, and `--filter` adjust
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
