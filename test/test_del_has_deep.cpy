# deep nesting: has()/get_attr() traversed by the iterative parser/emitter

struct Vec4:
    double a
    double b
    double c
    double d

public main() -> int:
    Vec4 v
    v.a = 1.5
    v.b = 2.5
    v.c = 3.5
    v.d = 4.5
    # deep expression (many nested parens/ints) so the iterative fallback runs
    int nsum = ((((((1 + 2) + 3) + 4) + 5) + 6) + 7) + \
        ((((((8 + 9) + 10) + 11) + 12) + 13) + 14) + \
        ((((((15 + 16) + 17) + 18) + 19) + 20) + 21)
    print(nsum)
    print(has(undeclared_name))
    print(get_attr(v, "c"))
    del v.b
    print(get_attr(v, "b"))
    return 0