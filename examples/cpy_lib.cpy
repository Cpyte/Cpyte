public def add(a int, b int) -> int:
    return a + b

public def factorial(n int) -> int:
    if n <= 1:
        return 1
    return n * factorial(n - 1)

private def helper():
    print(42)
