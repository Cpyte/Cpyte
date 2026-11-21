# Regression: same-precedence binary operators must associate LEFT, `**` right.
#
# The recursive Pratt parser used to hand the operator's own precedence to the
# right-hand side (`next_prec = _prec(op)`), which made it fold `a - b + c` as
# `a - (b + c)`. The iterative deep-expression fallback already did the correct
# `+ 1`, so the two parsers disagreed: `a - b + c` evaluated to 5 when written
# plainly and 9 when wrapped in 200 parens.
#
# Every expectation here is plain C/Python arithmetic.

def check(name str, got int64, want int64) -> int:
    if got != want:
        print("FAIL", name, "want", want, "got", got)
        return 1
    return 0

def check_d(name str, got double, want double) -> int:
    double diff = got - want
    if diff < 0.0:
        diff = diff * -1.0
    if diff > 0.000000001:
        print("FAIL", name, "want", want, "got", got)
        return 1
    return 0

def main() -> int:
    int a = 10
    int b = 3
    int c = 2
    int d = 5
    int n = 0

    # add/sub level: the classic sign-flip cases
    n += check("a - b + c", a - b + c, 9)
    n += check("a + b - c", a + b - c, 11)
    n += check("a - b - c", a - b - c, 5)
    n += check("a - b + c - d", a - b + c - d, 4)
    n += check("a + b - c + d", a + b - c + d, 16)

    # mul/div level: left vs right changes both
    n += check("a * b / c", a * b / c, 15)
    n += check("a / b * c", a / b * c, 6)
    n += check("a * b % c", a * b % c, 0)
    n += check("a % b / c", a % b / c, 0)

    # shifts
    n += check("1 << 3 >> 1", (int)1 << 3 >> 1, 4)

    # bitwise, left-assoc within each level and ordered across levels
    n += check("6 & 3 | 1", 6 & 3 | 1, 3)
    n += check("7 ^ 3 & 1", 7 ^ 3 & 1, 6)

    # `**` stays RIGHT-associative: 2 ** 3 ** 2 is 2 ** 9, not 8 ** 2
    n += check("2 ** 3 ** 2", 2 ** 3 ** 2, 512)
    n += check("(2 ** 3) ** 2", (2 ** 3) ** 2, 64)

    # precedence still beats associativity
    n += check("a - b * c", a - b * c, 4)
    n += check("a - b + c * d", a - b + c * d, 17)

    # explicit parentheses still mean what they say
    n += check("a - (b + c)", a - (b + c), 5)

    # same rules for doubles
    double x = 0.25
    double y = 1.0
    double z = 3.0
    n += check_d("x - y + z", x - y + z, 2.25)
    n += check_d("x - y - z", x - y - z, -3.75)

    # long alternating chain: 0+1-2+3-4+5-6+7-8+9-10
    double acc = 0.0
    int i = 1
    while i <= 10:
        double term = (double)i
        if i % 2 == 1:
            acc = acc + term
        else:
            acc = acc - term
        i = i + 1
    n += check_d("manual alternating", acc, -5.0)
    n += check_d("inline alternating", 0.0 + 1.0 - 2.0 + 3.0 - 4.0 + 5.0 - 6.0 + 7.0 - 8.0 + 9.0 - 10.0, -5.0)

    # a folded constant chain must follow the same rule
    n += check("literal 10 - 3 + 2", 10 - 3 + 2, 9)

    if n == 0:
        print("associativity: PASS")
    else:
        print("associativity: FAIL", n)
    return n
