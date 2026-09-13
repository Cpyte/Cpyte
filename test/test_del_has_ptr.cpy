# get_attr on struct pointers, del *ptr, del of str, del in a loop

struct Point:
    int x
    int y

public make_point() -> Point:
    Point t
    t.x = 7
    t.y = 13
    return t

public read_pt(p: Point*) -> int:
    return (int)get_attr(p, "x")

public main() -> int:
    Point pt = make_point()
    # runtime-name get_attr on a struct pointer (fields uniform int)
    str f = "y"
    print(get_attr(pt, f))

    Point* pp = &pt
    print(get_attr(pp, "x"))

    # del *ptr — clear the dereferenced slot
    del *pp
    print(pt.x)

    # del a str variable (static)
    str s = "hello"
    print(has(s))
    del s
    print(has(s))

    # del inside a loop
    int i = 0
    while i < 3:
        int tmp = i
        del tmp
        i = i + 1
    print(i)
    return 0