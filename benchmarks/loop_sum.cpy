public main() -> int:
    int64 total = 0
    int64 i = 0
    while i < 20000000:
        total = total + i
        i = i + 1
    print(total)
    return 0
