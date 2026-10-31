#nogc
public main() -> int:
    int n = 1000000
    int64[] values = new int64[n]
    int i = 0
    while i < n:
        values[i] = i % 97
        i = i + 1
    int64 total = 0
    i = 0
    while i < n:
        total = total + values[i]
        i = i + 1
    print(total)
    return 0
