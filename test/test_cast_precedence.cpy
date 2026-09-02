import "stdio"

def check(bits size_t, cap size_t) -> int:
    if bits + (size_t)1 > cap:
        return 1
    return 0

def main():
    int r1 = check((size_t)5, (size_t)64)
    if r1 != 0:
        printf("FAIL: 5+1 > 64 should be false\n")
        return 1
    int r2 = check((size_t)64, (size_t)64)
    if r2 != 1:
        printf("FAIL: 64+1 > 64 should be true\n")
        return 1
    int x = 7
    int y = (int)x + 1
    if y != 8:
        printf("FAIL: cast + 1 precedence\n")
        return 1
    int z = (int)(x + 1)
    if z != 8:
        printf("FAIL: parenthesized cast operand\n")
        return 1
    int neg = (int)-3
    if neg != -3:
        printf("FAIL: unary cast operand\n")
        return 1
    printf("cast precedence regression OK\n")
    return 0
