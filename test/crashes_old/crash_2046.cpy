struct S4:
    float* key
    int64 prev
struct S1:
    uint64 y
    int* value
    int64 data
    big* tail
uint64 g1

def main() -> int:
    print(340282366920938463463374607431768211455)
    print((g1 // (+ g1)))
    return 0