# Stage 5 OOP: dataclass auto __init__/__eq__/__str__
dataclass class Point:
    int x
    int y
    def area() -> int:
        return this.x * this.y
    def __str__() -> str:
        return "P" + str(this.x)

dataclass class Point3(Point):
    int z

dataclass class Tag:
    int id

dataclass class Critter:
    int hp
    int level

class Cat(Critter):
    def scratch() -> int:
        return 9

class Holder:
    Point p
    def __init__(p: Point):
        this.p = p
    def get() -> int:
        return this.p.x

public def main() -> int:
    Point* a = new Point(3, 4)
    Point* b = new Point(3, 4)
    Point* c = new Point(5, 6)

    print(a.x)
    print(a.y)
    print(a.area())
    print(str(a))
    print(str(new Point(5, 6)))

    print(a == b)
    print(a == c)
    b.x = 7
    print(a == b)
    b.x = 3
    print(a == b)

    Point3* d = new Point3(1, 2, 3)
    print(d.x)
    print(d.z)
    print(d == new Point3(1, 2, 3))
    print(d == new Point3(1, 2, 4))

    Holder* h = new Holder(a)
    print(h.get())
    print(h.p == a)

    Tag* t = new Tag(9)
    print(t.id)
    print(t == new Tag(9))
    print(t == new Tag(8))
    print(str(t))

    Critter* ca = (Critter*)new Cat(10, 2)
    Critter* cb = (Critter*)new Cat(10, 2)
    print(ca.hp)
    print(ca == cb)
    print(ca.level == 2)
    print(str(ca))
    return 0