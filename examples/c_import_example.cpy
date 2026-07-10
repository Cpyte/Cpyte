# C Import Example
# Demonstrates importing C source files and calling C functions
# Usage: python mainpie.py --jit examples/c_import_example.cpy

import "examples/example_math.c"

struct Vector2D:
    float x
    float y

public create_vector(x: float, y: float) -> Vector2D:
    Vector2D v
    v.x = x
    v.y = y
    return v

public print_vector(v: Vector2D):
    print(v.x)
    print(v.y)

public main() -> int:
    print(1)
    int a = 15
    int b = 25
    int sum = c_add(a, b)
    print(sum)

    print(2)
    int product = c_multiply(a, b)
    print(product)

    print(3)
    int diff = c_subtract(b, a)
    print(diff)

    print(4)
    int quot = c_divide(b, a)
    print(quot)

    print(5)
    int mod = c_modulus(b, a)
    print(mod)

    print(6)
    int fact = c_factorial(10)
    print(fact)

    print(7)
    int fib = c_fibonacci(15)
    print(fib)

    print(8)
    int gcd = c_gcd(48, 18)
    print(gcd)

    print(9)
    int lcm = c_lcm(12, 18)
    print(lcm)

    print(10)
    int is_prime = c_is_prime(17)
    print(is_prime)

    print(11)
    int rev = c_reverse_number(12345)
    print(rev)

    print(12)
    int sum_digits = c_sum_of_digits(12345)
    print(sum_digits)

    print(13)
    Vector2D v1 = create_vector(3.5, 4.5)
    float len = c_vector_length(v1.x, v1.y)
    print(len)

    print(14)
    int arr_sum = c_array_sum(5)
    print(arr_sum)

    return 0
