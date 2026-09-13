# user `def has` shadows the builtin
public def make_has() -> int:
    return 0

public def has(x: int) -> int:
    return x + 100

public def main() -> int:
    int a = 5
    print(has(a))
    print(make_has())
    return 0
