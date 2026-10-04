# Tier-1 Python-feel features: len() on strings, the conditional expression
# `a if c else b`, default/keyword arguments, and the pointer-width integer
# types (ssize_t, ptrdiff_t, intptr_t, uintptr_t).

def clamp(v: int, lo: int, hi: int) -> int:
    # Nested ternary: right-associative, so this is v<lo ? lo : (v>hi ? hi : v)
    return lo if v < lo else (hi if v > hi else v)


def sign(n: int) -> str:
    # A ternary in a `str` position (both branches are i8*).
    return "neg" if n < 0 else ("zero" if n == 0 else "pos")


def sum_ternary(arr: int64[], n: int) -> int64:
    int64 total = 0
    for i in range(n):
        total = total + (arr[i] if arr[i] > 0 else 0)
    return total


# ---- default arguments --------------------------------------------------
def scale(v: int, factor: int = 2, offset: int = 100) -> int:
    return v * factor + offset


def shout(s: str, mark: str = "!") -> str:
    return s + mark


def halve(a: double, b: double = 2.0) -> double:
    return a / b


def no_default(a: int) -> int:
    return a + 1


def main():
    # ---- len() on strings -------------------------------------------------
    print(len("abc"))            # 3
    print(len(""))               # 0
    str s = "hello"
    print(len(s))                # 5
    print(len(s + "!"))          # 6
    print(len(str_split("a b c", " ")))   # 6 (array path still works)

    # ---- conditional expression -------------------------------------------
    print(clamp(99, 0, 10))      # 10
    print(clamp(-9, 0, 10))      # 0
    print(clamp(5, 0, 10))       # 5
    print(sign(-5))              # neg
    print(sign(0))               # zero
    print(sign(5))               # pos

    int a = 4
    # Binary operators bind tighter than the ternary.
    print(1 + 2 if a > 3 else 99)         # 3
    print(2 * 3 if a > 2 else 99)         # 6
    print(1 + 2 if a > 99 else 99)        # 99
    # Parenthesised ternary inside a larger expression.
    print((a if a > 3 else 0) + 100)      # 104
    # Constant condition: the taken branch is the only one evaluated.
    print(1 if true else 2)               # 1
    print(1 if false else 2)              # 2
    # Ternary as a loop condition.
    int i = 0
    while (i < 3 if true else false):
        print(i)                          # 0 1 2
        i = i + 1
    # Ternary feeding a call argument.
    print(clamp(a if a > 100 else 7, 0, 5))   # 5
    # int64 branches must not fold at 32-bit width.
    int64 big = 5000000000
    print(big if a > 3 else 1)            # 5000000000
    # A ternary inside a loop body accumulating an int64 array element.
    int64[4] buf
    buf[0] = 7
    buf[1] = -3
    print(sum_ternary(buf, 2))            # 7

    # ---- pointer-width integer types --------------------------------------
    ssize_t sa = 5
    ptrdiff_t pd = -7
    intptr_t ip = 9
    uintptr_t up = 11
    size_t sz = 13
    print(sa)                             # 5
    print(pd)                             # -7  (must sign-extend, not wrap)
    print(ip)                             # 9
    print(up)                             # 11
    print(sz)                             # 13
    ssize_t sbig = 5000000000
    print(sbig)                           # 5000000000
    print(sa + 1)                         # 6
    print(sz + 1)                         # 14
    ssize_t sneg = sa - 10
    print(sneg)                           # -5
    print(sizeof(ssize_t))                # 8
    print(sizeof(ptrdiff_t))              # 8

    # ---- default arguments ------------------------------------------------
    print(scale(5))                  # 5*2+100  = 110
    print(scale(5, 3))               # 5*3+100  = 115
    print(scale(5, 3, 0))            # 5*3      = 15
    print(shout("hey"))              # hey!
    print(shout("hey", "?"))         # hey?
    print(halve(8.0))                # 4.000000
    print(halve(9.0, 3.0))           # 3.000000
    print(no_default(1))             # 2

    # ---- keyword arguments -----------------------------------------------
    print(scale(5, offset=1))            # 5*2+1   = 11
    print(scale(5, offset=1, factor=10)) # 5*10+1  = 51
    print(scale(v=5, factor=3))         # 5*3+100 = 115
    print(shout(mark=".", s="yo"))       # yo.
    print(no_default(a=41))             # 42
    # A keyword argument whose default expression is a float.
    print(halve(b=4.0, a=10.0))         # 2.500000
    # Defaults are substituted per call site, so they are not shared state.
    print(scale(2), scale(2, offset=0))  # 104 4

    # A default may itself be a ternary / call expression.
    print(scale(3, offset=clamp(99, 0, 7)))   # 3*2+7 = 13