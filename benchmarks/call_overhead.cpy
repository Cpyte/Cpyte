public recurse(n: int) -> int:
    if n <= 0:
        return 0
    return recurse(n - 1) + 1

public main() -> int:
    int total = 0
    int i = 0
    while i < 10000:
        total = total + recurse(5000)
        i = i + 1
    print(total)
    return 0
