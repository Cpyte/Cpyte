#nogc
import stdlib
import string

public main() -> int:
    int64 len = 0
    int64 cap = 16
    char* s = (char*)malloc(cap)
    s[0] = '\0'

    int64 i = 0
    while i < 10000:
        if len + 5 + 1 > cap:
            cap = cap * 2
            s = (char*)realloc(s, cap)

        memcpy(s + len, "hello", 5)
        len = len + 5
        s[len] = '\0'
        i = i + 1

    print(strlen(s))
    free(s)
    return 0
