def tick(t int) -> int:
    print(t)
    return t * 2

def main():
    int a = 7
    print(a * 0)
    print(0 * a)
    print(a * 1)
    print(1 * a)
    print(a * 8)
    print(8 * a)
    print(a * 9)
    print(a * 7)
    print(a * 15)
    print(a * 16)
    print(a * 17)
    print(a * 31)
    print(a * 33)
    print(a + 0)
    print(0 + a)
    print(a - 0)
    print(a ^ a)
    print(-5 ^ -5)
    print(3 * 0)
    print(123 * 1)
    print(123 % 1)
    print(123 // 1)
    print(7 * 32)
    print(-3 * 8)
    print(64 * 16)
    print("\n")
    int x = 5
    x = (x << 2)
    print(x)
    x = (x << 3)
    print(x)
    int y = 3
    y = (y << 1) << 4
    print(y)
    int z = 10
    print(z * 5)
    print(z * 6)
    print(z * 11)
    int w = -6
    print(w * 4)
    print(w * 3)

    int b = 7
    print(b & 0)
    print(0 & b)
    print(b | 0)
    print(0 | b)
    print(b * 3)
    print(b * 5)
    print(b * 9)
    print(b * 10)

    int cp = 4
    print(cp * 6)

    int q = 1
    if q == 1:
        print(111)
    else:
        print(222)

    if 0:
        print(333)
    else:
        print(444)

    if 5:
        print(555)
    else:
        print(666)

    int li = 0
    int ci = 0
    while ci < 5:
        li = li + (2 * 3)
        ci = ci + 1
    print(li)
    print(ci)

    int base = 100
    int ho = 0
    int j = 0
    while j < 4:
        if j > 9:
            print(77)
        ho = base + 1
        j = j + 1
    print(ho)

    int q9 = 3
    int m9 = 0
    int x9 = 0
    while m9 < 3:
        x9 = tick(q9)
        m9 = m9 + 1
    print(x9)