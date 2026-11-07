#nogc
# Unsigned integer arithmetic must use C's unsigned semantics, not the signed
# lowering: values >= 2**63 would otherwise divide/compare/shift wrongly.

public show64(a: uint64, b: uint64) -> int:
    print(a / b)
    print(a % b)
    print(a > b)
    print(a < b)
    print(a >> b)
    print(a // b)
    return 0

public show_sz(a: size_t, b: size_t) -> int:
    print(a / b)
    print(a % b)
    print(a >= b)
    print(a >> b)
    return 0

public main() -> int:
    uint64 big = 18446744073709551615
    show64(big, 3)
    show_sz(big, 4)

    uint64 half = 9223372036854775808
    print(half > 1)
    print(half / 2)
    print(half // 2)
    print(half % 3)

    # A signed operand joins the unsigned operation (C's usual conversions).
    int neg = -1
    uint64 one = 1
    print(neg > one)

    # Unsigned loop counter that crosses 2**63 is still ordered numerically.
    uint64 n = 0
    int steps = 0
    while n < 4:
        n = n + 1
        steps = steps + 1
    print(steps)

    # Narrow unsigned words widen by VALUE (zero-extension), never sign-extend:
    # a uint32 0xFFFFFFFF widened to uint64 is 4294967295, not -1.
    uint32 narrow = 4294967295
    uint64 wide = narrow
    print(wide)
    print(wide / 2)
    print(narrow > 0)
    uint16 small = 65535
    uint64 wide2 = small + 1
    print(wide2)
    int signed_seen = narrow
    print(signed_seen)
    print(narrow)
    print(str(narrow))

    # Narrower-than-int unsigned words promote to `int`, so they compare as
    # signed 32-bit values (a uint8 200 beats -1).
    uint8 byte = 200
    int minus = -1
    print(byte > minus)
    uint16 mid = 40000
    uint16 bigger = 50000
    print(bigger > mid)
    return 0
