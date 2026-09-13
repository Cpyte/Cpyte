# del on dynamic + has() after del + shadowing by user defs
public def my_has(x: int) -> int:
    return x

public def main() -> int:
    dynamic d
    d = 7
    print(has(d))
    del d
    print(has(d))
    # re-delcare and reuse after dynamic del
    dynamic d
    d = 3
    print(has(d))
    # echo a value through the real builtins inside an expression:
    print(has(d))
    return 0
