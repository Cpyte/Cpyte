public fib(n: int) -> int:
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)

public main() -> int:
    print(fib(38))
    return 0
