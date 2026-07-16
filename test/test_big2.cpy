def main() -> int:
    big a = 18446744073709551616
    big b = 18446744073709551616
    big sum = a + b
    big diff = a - 100
    big prod = a * 2
    big quot = a / 2
    big rem = a % 2
    big neg = -a

    print(a)
    print("\n")
    print(b)
    print("\n")
    print(sum)
    print("\n")
    print(diff)
    print("\n")
    print(prod)
    print("\n")
    print(quot)
    print("\n")
    print(rem)
    print("\n")
    print(neg)
    print("\n")

    if a > b:
        print("gt")
    elif a == b:
        print("eq")
    else:
        print("lt")
    print("\n")

    return 0