# del / has / get_attr features — runtime dispatch + heap + slot forms
struct Vec3:
    double x
    double y
    double z

public def make_vec(x: double, y: double, z: double) -> Vec3:
    Vec3 v
    v.x = x
    v.y = y
    v.z = z
    return v

public def main() -> int:
    # get_attr/has with a runtime str name — all fields uniform double
    Vec3 v = make_vec(1.0, 2.0, 5.0)
    str n = "y"
    print(has(v, n))
    print(get_attr(v, n))
    # runtime name missing
    str bad = "w"
    print(has(v, bad))
    # del obj.field — slot clear
    del v.y
    print(get_attr(v, "y"))  # reads cleared slot -> 0.0
    print(has(v, "y"))  # field still exists in the type -> 1
    return 0
