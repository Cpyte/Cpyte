# Stage 4 OOP: super + properties
sealed class Meter:
    int _v
    property value:
        get:
            return this._v
        set:
            this._v = value
    def tick() -> int:
        this._v = this._v + 1
        return this._v

class Animal:
    str name
    int a
    def __init__(a: int):
        this.a = a
        this.name = "animal"
    def speak() -> int:
        return 1
    def loud() -> int:
        return 10

class Dog(Animal):
    int b
    def __init__(a: int, b: int):
        super.__init__(a)
        this.b = b
    def speak() -> int:
        return this.a * 100 + 2
    def loud() -> int:
        return super.loud() + 5
    def base_speak() -> int:
        return super.speak()

class Pup(Dog):
    def __init__(a: int, b: int):
        super.__init__(a, b)
    def base_base() -> int:
        return super.speak()

sealed class Value:
    int _x
    property x:
        get:
            return this._x
        set:
            this._x = value
    property doubled:
        get:
            return this._x * 2
    def bump() -> int:
        this._x = this._x + 3
        return this._x

public def main() -> int:
    Meter* m = new Meter()
    m.value = 42
    print(m.value)
    m.value = m.value + 7
    print(m.value)
    m.tick()
    print(m.value)

    Dog* d = new Dog(7, 9)
    print(d.speak())
    print(d.loud())
    print(d.base_speak())

    Pup* p = new Pup(3, 4)
    print(p.speak())
    print(p.base_speak())
    print(p.loud())
    print(p.base_base())

    Animal* av = (Animal*)new Dog(5, 6)
    print(av.speak())
    print(av.loud())

    Value* v = new Value()
    v.x = 100
    print(v.x)
    print(v.doubled)
    v.bump()
    print(v.doubled)
    return 0