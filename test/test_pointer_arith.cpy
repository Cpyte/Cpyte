# Typed (C-style) pointer arithmetic. Offsets are scaled by sizeof(*ptr) and
# pointer differences are reported in elements, not raw bytes. char*/void*
# keep 1-byte scaling, so existing byte-buffer code is unchanged.

struct Pair:
    int a
    int b

def expect(name str, got int, want int) -> int:
    if got != want:
        print("FAIL", name, got, want)
        return 1
    return 0

def main() -> int:
    int bad = 0

    int[] a = new int[4]
    a[0] = 10
    a[1] = 20
    a[2] = 30
    a[3] = 40
    int* p = (int*)a

    bad += expect("p[0]", p[0], 10)
    bad += expect("p+1", (p + 1)[0], 20)
    bad += expect("p+3", (p + 3)[0], 40)
    bad += expect("1+p", (1 + p)[0], 20)
    bad += expect("(p+3)-p", (p + 3) - p, 3)
    bad += expect("p-(p+2)", p - (p + 2), -2)
    int* back = (p + 3) - 1
    bad += expect("(p+3)-1", back[0], 30)
    bad += expect("negative i64 offset", ((p + 2) + (int64)-1)[0], 20)

    size_t one = 1
    bad += expect("size_t offset", (p + one)[0], 20)

    char* bytes = (char*)p
    char* bytes2 = bytes + 4
    bad += expect("char* byte offset", bytes2 - bytes, 4)

    double[] d = new double[3]
    d[0] = 1.5
    d[1] = 2.5
    d[2] = 3.5
    bad += expect("double[] + 1", int((d + 1)[0]), 2)

    Pair[] pairs = new Pair[3]
    pairs[0].a = 1
    pairs[1].a = 2
    pairs[2].a = 3
    Pair* q = (Pair*)pairs
    Pair* q2 = q + 2
    bad += expect("struct* + 2", q2.a, 3)
    bad += expect("struct* difference", q2 - q, 2)

    if bad == 0:
        print("pointer arithmetic: PASS")
    else:
        print("pointer arithmetic: FAIL")
    return bad
