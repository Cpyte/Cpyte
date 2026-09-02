def main() -> int:
    ubig a = 18446744073709551616
    print(a)
    print("\n")
    ubig b = 100
    ubig c = a + b
    print(c)
    print("\n")
    ubig d = a * 2
    print(d)
    print("\n")
    ubig e = a - 1
    print(e)
    print("\n")
    if a > b:
        print("a > b")
        print("\n")
    ubig f = a / 2
    print(f)
    print("\n")
    ubig g = a % 3
    print(g)
    print("\n")
    # bitwise ops over the full magnitude
    ubig h = a | 15
    print(h)
    print("\n")
    ubig i = a & 15
    print(i)
    print("\n")
    ubig j = a ^ 15
    print(j)
    print("\n")
    ubig k = a << 2
    print(k)
    print("\n")
    ubig l = k >> 2
    print(l)
    print("\n")
    return 0
