public main() -> int:
    int64 total = 0
    int i = 0
    while i < 2000000:
        if i % 3 == 0:
            total = total + i % 97
        else:
            if i % 5 == 0:
                total = total - i % 31
            else:
                total = total + 1
        i = i + 1
    print(total)
    return 0
