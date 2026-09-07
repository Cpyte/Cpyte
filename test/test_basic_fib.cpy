def fib(n: int) -> int:
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)

def fib_big(n: int) -> big:
    if n <= 1:
        big nn = n
        return nn
    return fib_big(n - 1) + fib_big(n - 2)

def main():
    print(fib(0))
    print(fib(1))
    print(fib(2))
    print(fib(3))
    print(fib(10))
    print(fib(20))
    print(fib(30))
    print(fib(40))
    print(fib(45))
    print(fib(71))
    print(fib(90))
    print(fib(-3))
    print(fib(-1))
    print(fib(0))
    print(fib(1))
    print(fib(46))
    print(fib(47))
    print(fib(100))
    print("\n")
    print(fib_big(0))
    print("\n")
    print(fib_big(1))
    print("\n")
    print(fib_big(10))
    print("\n")
    print(fib_big(63))
    print("\n")
    print(fib_big(100))
    print("\n")
    print(fib_big(200))
    print("\n")
    print(fib_big(-4))
    print("\n")