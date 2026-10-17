struct Point:
    int x
    int y

class Counter:
    int n
    def __init__(n: int):
        this.n = n
    def get() -> auto:
        return this.n

def add(a: int, b: int) -> auto:
    return a + b

def pick(a: int) -> auto:
    if a:
        return 100
    else:
        return 200

def make() -> auto:
    return new Counter(5)

auto g = 42

public def main() -> int:
    auto i = 5
    auto d = 3.5
    auto s = "hi"
    auto p = new Point
    p.x = 7
    p.y = 3
    auto c = new Counter(9)
    auto t = add(2, 3)
    auto v = make()

    if i != 5:
        return 1
    if d * 2.0 != 7.0:
        return 2
    if s != "hi":
        return 3
    if p.x + p.y != 10:
        return 4
    if c.get() != 9:
        return 5
    if t != 5:
        return 6
    if pick(1) != 100:
        return 7
    if pick(0) != 200:
        return 8
    if v.n != 5:
        return 9
    if g != 42:
        return 10

    print("auto ok")
    print(i)
    print(t)
    print(pick(1))
    print(pick(0))
    print(c.n)
    print(c.get())
    print(v.n)
    print(g)
    print(p.x)
    print(s)
    return 0
