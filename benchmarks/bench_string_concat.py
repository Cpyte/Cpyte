#!/usr/bin/env python3
import subprocess
import time
import statistics

def run_benchmark(cmd, iterations=10):
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        subprocess.run(cmd, shell=True, capture_output=True)
        end = time.perf_counter()
        times.append((end - start) * 1000)  # Convert to ms
    return statistics.median(times)

print("String Concatenation Benchmark")
print("=" * 50)

# C version
c_time = run_benchmark("./string_concat_c", 5)
print(f"C:   {c_time:.2f}ms")

# Cpy version
cpy_time = run_benchmark("./string_concat", 5)
print(f"Cpy: {cpy_time:.2f}ms")

# Python version
py_time = run_benchmark("python3 string_concat.py", 5)
print(f"Py:  {py_time:.2f}ms")

print()
print(f"Cpy/C:   {cpy_time/c_time:.2f}x")
print(f"Py/C:    {py_time/c_time:.2f}x")
print(f"Py/Cpy:  {py_time/cpy_time:.2f}x")
