import sys

def is_prime(n):
    if n < 2:
        return 0
    if n == 2:
        return 1
    if n % 2 == 0:
        return 0
    i = 3
    while i * i <= n:
        if n % i == 0:
            return 0
        i += 2
    return 1

count = 0
for n in range(2, 1000000):
    count += is_prime(n)
print(count)
