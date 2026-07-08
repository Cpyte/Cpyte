public is_prime(n: int) -> int:
    if n < 2:
        return 0
    if n == 2:
        return 1
    if n % 2 == 0:
        return 0
    int i = 3
    while i * i <= n:
        if n % i == 0:
            return 0
        i = i + 2
    return 1

public main() -> int:
    int count = 0
    int n = 2
    while n < 1000000:
        count = count + is_prime(n)
        n = n + 1
    print(count)
    return 0
