import sys

def fib(n):
    a, b = 0, 1
    while n > 0:
        t = a + b
        a = b
        b = t
        n -= 1
    return a

total = 0
n = 0
while n < 1000:
    total += fib(n)
    n += 1
print(total)
