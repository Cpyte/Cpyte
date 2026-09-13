# del / has / get_attr features
# Regression corpus for Sept 2026 feature work.
struct Vec3:
    double x
    double y
    double z

public def main() -> int:
    int a = 42
    print(has(a))
    del a
    print(has(a))
    Vec3 v
    v.x = 1.5
    v.y = 2.5
    v.z = 3.5
    print(has(v, "y"))
    print(has(v, "w"))
    print(get_attr(v, "x"))
    print(get_attr(v, "z"))
    dynamic d
    d = 7
    print(has(d))
    d = 0
    del d
    return 0
