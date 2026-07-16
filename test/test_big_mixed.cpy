def main() -> int:
    big x = 18446744073709551616
    int y = 42
    big z = x + y
    print(z)
    print("\n")
    big w = y + x
    print(w)
    print("\n")
    if x > y:
        print("gt")
        print("\n")
    if y < x:
        print("lt")
        print("\n")
    return 0