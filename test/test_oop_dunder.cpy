# Stage 2 OOP: dunder operator overloading
class Counter:
    int n
    def __init__(n: int):
        this.n = n
    def __add__(other: Counter) -> int:
        return this.n + other.n
    def __eq__(other: Counter) -> int:
        if this.n == other.n:
            return 1
        return 0
    def __len__() -> int:
        return this.n
    def __str__() -> str:
        return "C" + str(this.n)

class BigCounter(Counter):
    int extra
    def __init__(n: int):
        this.n = n
        this.extra = 100

sealed class Vec:
    int x
    int y
    def __init__(x: int, y: int):
        this.x = x
        this.y = y
    def __mul__(k: int) -> int:
        return this.x * k + this.y * k
    def __getitem__(i: int) -> int:
        if i == 0:
            return this.x
        return this.y

sealed class Vec3(Vec):
    def sum() -> int:
        return this.x * this.x + this.y * this.y

sealed class Grabber:
    def __call__(a: int, b: int) -> int:
        return a * b

class Animal:
    int legs
    def __init__(legs: int):
        this.legs = legs
    def __truediv__(other: Animal) -> int:
        return this.legs / other.legs

class Dog(Animal):
    pass

class Cat(Animal):
    pass

public def main() -> int:
    Counter* a = new Counter(5)
    Counter* b = new Counter(3)
    print(a + b)
    print(a == b)
    print(a == a)
    print(a != b)
    print(len(a))
    print(str(a))
    BigCounter* bc = new BigCounter(10)
    print(bc + b)
    Vec* v = new Vec(3, 4)
    print(v[0])
    print(v[1])
    print(v * 3)
    Vec3* v3 = new Vec3(2, 5)
    print(v3[0])
    print(v3 * 2)
    Grabber* g = new Grabber()
    print(g(6, 7))
    Dog* dg = new Dog(6)
    Cat* ct = new Cat(2)
    print(dg / ct)
    return 0