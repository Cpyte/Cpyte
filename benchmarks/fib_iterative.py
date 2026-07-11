import sys
import ctypes

def fib(n):
    a = ctypes.c_int32(0)
    b = ctypes.c_int32(1)
    while n > 0:
        t = ctypes.c_int32(a.value + b.value)
        a = b
        b = t
        n -= 1
    return a.value

total = 0
n = 0
while n < 10000:
    total += fib(n)
    n += 1
print(total)
