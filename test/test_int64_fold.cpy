def main():
    int64 a = 104729
    int64 b = 104729
    print(a * b)          # 10968163441
    print(a ** 2)         # 10968163441
    int64 c = 5000
    print(c ** 3)         # 125000000000
    uint64 d = 2000000000
    uint64 e = 2000000000
    print(d * e)          # 4000000000000000000
    int f = 46340
    print(f * f)          # 2147395600 (int still wraps 32-bit)
