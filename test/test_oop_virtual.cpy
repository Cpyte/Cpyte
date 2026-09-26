# Stage 1 OOP: vtables, dynamic dispatch, sealed, __init__
class Animal:
    str name
    def __init__(n: int):
        this.name = "animal"
    def speak() -> int:
        return 1
    def eat() -> int:
        return 10

class Dog(Animal):
    def speak() -> int:
        return 2

class Cat(Animal):
    def speak() -> int:
        return 3

class Rott(Dog):
    def speak() -> int:
        return 4

sealed class Point:
    int x
    int y
    def __init__(x: int, y: int):
        this.x = x
        this.y = y
    def sum() -> int:
        return this.x + this.y

sealed class Point3(Point):
    def sum() -> int:
        return this.x * this.y

public def main() -> int:
    Dog* d = new Dog(7)
    Cat* c = new Cat(7)
    Rott* r = new Rott(7)
    Point* p = new Point(3, 4)
    Point3* p3 = new Point3(5, 6)
    print(d.speak())
    print(c.speak())
    print(r.speak())
    print(p.sum())
    print(p3.sum())
    Animal* a = (Animal*)new Dog(7)
    print(a.speak())
    Animal* a2 = (Animal*)new Rott(7)
    print(a2.speak())
    print(((Animal*)d).eat())
    Point pv
    pv.x = 3
    pv.y = 4
    print(pv.sum())
    print(get_attr((Animal*)d, "name"))
    print(has(p3, "x"))
    del p3.x
    print(get_attr(p3, "x"))
    return 0