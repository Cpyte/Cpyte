public fib(n: int) -> int:
    int a = 0
    int b = 1
    while n > 0:
        int t = a + b
        a = b
        b = t
        n = n - 1
    return a

public main() -> int:
    int total = 0
    int n = 0
    while n < 10000:
        total = total + fib(n)
        n = n + 1
    print(total)
    return 0
