struct S4:
    uint64 data
    str* key
    int64 data
    int64* tail
struct S1:
    uint64 value
    uint64* value
    float name
int g1
big g2

def main() -> int:
    g2 = 0x1
    g1 = (0x100000000 % g1)
    print((g1 and g1))
    return 1000