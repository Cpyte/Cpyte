def main() -> int:
    ubig a = 18446744073709551616
    # conversions: int/int64/uint64/size_t/big -> ubig and ubig -> big
    int i1 = 42
    ubig u1 = i1
    print(u1)
    print("\n")
    int64 i2 = -7
    ubig u2 = i2
    print(u2)
    print("\n")
    uint64 u3v = 7
    ubig u3 = u3v
    print(u3)
    print("\n")
    size_t s1 = 99
    ubig u4 = s1
    print(u4)
    print("\n")
    big b1 = 12345678901234567890
    ubig u5 = b1
    print(u5)
    print("\n")
    big b2 = u5
    print(b2)
    print("\n")
    # casts (ubig)/(big) promote, never inttoptr
    ubig u6 = (ubig)100000000000000000000
    print(u6)
    print("\n")
    big b3 = (big)u6
    print(b3)
    print("\n")
    # exponent and floor division
    ubig p = u6 ** 2
    print(p)
    print("\n")
    ubig f = u6 // 3
    print(f)
    print("\n")
    # comparisons
    ubig x1 = 5
    ubig x2 = 9
    if x1 < x2:
        print("lt ok")
        print("\n")
    if x1 <= x2:
        print("le ok")
        print("\n")
    if x2 > x1:
        print("gt ok")
        print("\n")
    if x2 >= x1:
        print("ge ok")
        print("\n")
    if x1 != x2:
        print("ne ok")
        print("\n")
    ubig x3 = 5
    if x1 == x3:
        print("eq ok")
        print("\n")
    # shift beyond bit width -> zero
    ubig sh = a << 80
    print(sh)
    print("\n")
    ubig sr = a >> 80
    print(sr)
    print("\n")
    # string/int/float conversions treat ubig like big
    int iv = int(p)
    print(iv)
    print("\n")
    float fv = float(a)
    print(fv)
    print("\n")
    str sv = str(a)
    print(sv)
    print("\n")
    # mixed arithmetic with ints
    ubig m = a + 1
    print(m)
    print("\n")
    ubig m2 = a * 2
    print(m2)
    print("\n")
    return 0
