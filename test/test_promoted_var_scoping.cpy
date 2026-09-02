import "stdio"

# Regression: a variable promoted to `dynamic` inside one function must only
# affect that function's scope. The old analyzer tracked promotions by bare
# name across the whole module, so promoting `x` in `f` would wrongly stamp the
# unrelated struct-typed `x` in `g` as dynamic, producing a bogus
# "Unknown field 'val' in struct 'dynamic'" error (and breaking g).

struct Pair:
    int x
    int y

def build(a int) -> Pair:
    Pair p
    p.x = a
    p.y = a + 2
    return p

def getx(p Pair) -> int:
    return p.x

def f() -> int:
    int x = 10
    x = "hello"
    return 0

def g() -> int:
    Pair x = build(5)
    int r = getx(x)
    if r != 5:
        printf("FAIL: expected 5, got %d\n", r)
        return 1
    return 0

def main() -> int:
    int a = f()
    int b = g()
    if b != 0:
        printf("FAIL: g returned %d\n", b)
        return 1
    printf("promotion scoping OK\n")
    return 0
