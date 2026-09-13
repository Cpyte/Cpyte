def f(n int64) -> big:
    return n

def g(n uint64) -> big:
    return n

def fib(n int64) -> big:
    if n <= 2:
        return n
    return fib(n - 1) + fib(n - 2)

def main():
    print(f(7))
    print(f(-7))
    print(g(18446744073709551615))
    print(fib(30))