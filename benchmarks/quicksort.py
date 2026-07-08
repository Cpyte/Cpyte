import sys
import random

n = 100000
arr = [random.randint(0, 2**31 - 1) for _ in range(n)]
arr.sort()
print(arr[0], arr[-1])
