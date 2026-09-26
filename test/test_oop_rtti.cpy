class Animal:
    int legs
    def __init__(legs: int):
        this.legs = legs
    def speak() -> str:
        return "animal"

class Dog(Animal):
    def speak() -> str:
        return "woof"

class Rott(Dog):
    def speak() -> str:
        return "bark"

sealed class Point:
    int x
    int y
    def __init__(x: int, y: int):
        this.x = x
        this.y = y

sealed class Point3(Point):
    int z
    def __init__(x: int, y: int, z: int):
        this.x = x
        this.y = y
        this.z = z

public def main() -> int:
    Animal* a = (Animal*)new Dog(4)
    Dog* d = new Dog(4)
    Rott* r = new Rott(4)
    Animal* ra = (Animal*)new Rott(4)
    Point* p = new Point(1, 2)
    Point3* p3 = new Point3(1, 2, 3)

    # virtual: static type decides some, runtime id decides others
    print(isinstance(a, Animal))
    print(isinstance(a, Dog))
    print(isinstance(a, Rott))
    print(isinstance(d, Animal))
    print(isinstance(d, Dog))
    print(isinstance(r, Animal))
    print(isinstance(r, Rott))
    print(isinstance(ra, Animal))
    print(isinstance(ra, Dog))
    print(isinstance(ra, Rott))
    print(isinstance(ra, Animal))

    # sealed: monomorphic, declared-type semantics
    print(isinstance(p, Point))
    print(isinstance(p3, Point3))
    print(isinstance(p3, Point))

    if isinstance(ra, Rott):
        print(1)
    else:
        print(0)
    if isinstance(a, Rott):
        print(2)
    else:
        print(0)
    return 0