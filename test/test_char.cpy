# Char literals, char arithmetic and C-style ternary expressions.

def to_upper(c: char) -> char:
    if c >= 'a' and c <= 'z':
        return c - 32
    return c

def count_vowels(s: str) -> int:
    int n = 0
    int i = 0
    while s[i] != '\0':
        char c = s[i]
        if c == 'a' or c == 'e' or c == 'i' or c == 'o' or c == 'u':
            n = n + 1
        i = i + 1
    return n

def sign(x: int) -> int:
    return x > 0 ? 1 : x < 0 ? -1 : 0

public main() -> int:
    char a = 'A'
    print(int(a))
    print(int('z'))
    print(int('\n'))
    print(int('\0'))
    print(int('\x41'))

    char b = a + 1
    print(int(b))
    print(b == 'B')
    print(int(to_upper('m')))

    print(count_vowels("the quick brown fox"))
    str s = "abc"
    print(s[2] == 'c')
    char* p = "xyz"
    print(p[0] == 'x')

    print(sign(5))
    print(sign(-5))
    print(sign(0))
    int x = 7
    print(x > 3 ? 10 : 20)
    print(x > 9 ? 1 : 2)
    char d = x > 3 ? 'y' : 'n'
    print(d == 'y')

    char e = 'a'
    str t = e + "bc"
    print(t)
    return 0
