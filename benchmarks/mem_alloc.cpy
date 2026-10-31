#nogc
import stdlib

public main() -> int:
    int i = 0
    while i < 5000:
        int* p = (int*)malloc(64)
        *p = i
        free(p)
        i = i + 1
    print("done")
    return 0
