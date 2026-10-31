public main() -> int:
    int total = 0
    int i = 0
    while i < 2000000:
        int value = (i * 31 + 123) & 0x7fffffff
        total = total ^ (value >> (i % 16))
        i = i + 1
    print(total)
    return 0
