import sys

def fib(n):
    if n <= 1:
        return n
    t = fib(n - 1) + fib(n - 2)
    t &= 0xffffffff
    if t >= 0x80000000:
        t -= 0x100000000
    return t

print(fib(38))
