import sys

sys.setrecursionlimit(20000)

def recurse(n):
    if n <= 0:
        return 0
    return recurse(n - 1) + 1

total = 0
for i in range(10000):
    total += recurse(5000)
print(total)
