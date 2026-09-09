def sq(z int) -> int:
    return z ** 2 + z ** 3 + z ** 1 + z ** 0

def ema(t int) -> int:
    return t * 101

def main() -> int:
    # FP identity folds + double constant propagation
    double two = 2.0
    double d = 3.5
    int total = 0
    total = total + int(d * two)           # const-fold -> 7.0
    total = total + int(2.5 * 4.0)         # const-fold -> 10
    total = total + int(d * 1.0)           # identity x*1 -> x
    total = total + int(d * -1.0)          # x * -1  -> -x
    total = total + int(d / 1.0)           # x / 1   -> x
    total = total + int(d - 0.0)           # x - 0   -> x
    total = total + int(2.0 ** 2)          # pow(x,2) -> x*x
    total = total + int(d ** 2)            # pow(x,2) -> x*x (const base via cprop)
    total = total + int(d ** 1.0)          # pow(x,1) -> x
    total = total + int(d ** 0.0)          # pow(x,0) -> 1
    total = total + int(d ** 0.5)          # pow(x,0.5) -> sqrt(x)
    total = total + int(d ** -1.0)         # pow(x,-1) -> 1/x
    total = total + int(2.5 ** 3)          # int const-fold -> 15.625 -> 15
    # integer pow strength reduction (runtime base)
    print(sq(3))                           # 40
    print(sq(-2))                          # -5
    # zero-trip loop literal
    while 0:
        print(999)
    # unroll through an intervening constant decl
    int i = 0
    int acc = 0
    while i < 4:
        acc = acc + i
        i = i + 1
    print(acc)                             # 6
    # zero-iteration counted loop
    int j = 0
    int acc2 = 0
    while j < 0:
        acc2 = acc2 + j
        j = j + 1
    print(acc2)                            # 0
    # pending iv must NOT unroll when an address is taken
    int k = 0
    int* kp = &k
    while k < 2:
        k = k + 1
    print(k)                               # 2
    # LICM still guards calls (g must run per-iteration); ema/ho untouched
    int ticks = 0
    int ho = 0
    while ticks < 3:
        ho = ema(ticks)
        ticks = ticks + 1
    print(ho)                              # 202
    print(ema(1))                          # 101
    # FP const-prop MUST be invalidated when a loop reassigns the variable
    double d2 = 2.0
    int c = 0
    while c < 3:
        d2 = d2 + 1.0
        c = c + 1
    print(int(d2 * 3.0))                   # 15 (5.0 * 3.0; stale 2.0 would print 6)
    print(total)                           # 59
    return 0