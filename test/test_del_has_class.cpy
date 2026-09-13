# del / has / get_attr on classes + global del
int g = 5

class Dog:
    int legs
    str name

public def main() -> int:
    print(g)
    del g
    Dog d
    d.legs = 4
    d.name = "rex"
    print(get_attr(d, "legs"))
    print(has(d, "name"))
    print(has(d, "tail"))
    del d.legs
    print(get_attr(d, "legs"))
    return 0
