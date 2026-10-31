def fib(n):
    a = 0
    b = 1
    while n > 0:
        t = (a + b) & 0xFFFFFFFF
        if t >= 0x80000000:
            t -= 0x100000000
        a = b
        b = t
        n -= 1
    return a


total = 0
n = 0
while n < 10000:
    total += fib(n)
    n += 1
print(total)
