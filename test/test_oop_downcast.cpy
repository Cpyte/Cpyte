# Stage 6 OOP: `x as T` safe runtime downcast
dataclass class Animal:
    int id
    def label() -> str:
        return "animal"

dataclass class Dog(Animal):
    int legs

dataclass class Rott(Dog):
    int ferocity

dataclass class Cat(Animal):
    int lives

sealed dataclass class Point:
    int x
    int y

sealed dataclass class Point3(Point):
    int z

public def main() -> int:
    Animal* a = (Animal*)new Dog(1, 4)
    Animal* a2 = (Animal*)new Cat(2, 9)
    Animal* rott = (Animal*)new Rott(3, 4, 100)

    Dog* d = a as Dog
    if d == null:
        print(-1)
    else:
        print(d.legs)
        print(d.label())

    Cat* c = a2 as Cat
    if c == null:
        print(-1)
    else:
        print(c.lives)

    Cat* bad = a as Cat
    if bad == null:
        print(0)
    else:
        print(-1)

    Rott* r = rott as Rott
    if r == null:
        print(0)
    else:
        print(1)
        print(r.ferocity)

    Rott* into_dog = a as Rott
    if into_dog == null:
        print(1)
    else:
        print(0)

    Dog* dog_from_rott = rott as Dog
    if dog_from_rott == null:
        print(-1)
    else:
        print(dog_from_rott.legs)

    Animal* up = a as Animal
    if up == null:
        print(-1)
    else:
        print(up.id)

    # Sealed hierarchy: monomorphic, no vptr -> `x as T` to a strict descent
    # target is CONST null even if the pointer physically points at a Point3.
    Point* p = new Point(5, 6)
    Point3* p3 = p as Point3
    if p3 == null:
        print(1)
    else:
        print(0)
    Point* p32 = (Point*)new Point3(7, 8, 9)
    Point3* ok = p32 as Point3
    if ok == null:
        print(1)
    else:
        print(ok.z)
    return 0