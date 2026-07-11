import string

public main() -> int:
    str s = ""
    int i = 0
    while i < 10000:
        s = s + "hello"
        i = i + 1
    print(strlen(s))
    return 0
