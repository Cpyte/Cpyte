public main() -> int:
    double value = 0.25
    int i = 0
    while i < 2000000:
        value = value * 1.0000001 + 0.0000005
        i = i + 1
    print(value)
    return 0
