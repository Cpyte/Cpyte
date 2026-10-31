public main() -> int:
    int64 total = 0
    int64 i = 0
    while i < 2000000:
        total = total + (i * 17 + 3) % 97
        i = i + 1
    print(total)
    return 0
