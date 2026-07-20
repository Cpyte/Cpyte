# 64-bit Example
# Demonstrates int64 and uint64 support including arithmetic,
# hexadecimal literals, type conversions, and large computations
# Usage: python source/mainpie.py --jit examples/64bit_example.cpy

import "string"

struct LargeData:
    int64 id
    uint64 mask
    int64 timestamp
    int checksum

public make_large_data(id: int64, mask: uint64, ts: int64, cksum: int) -> LargeData:
    LargeData d
    d.id = id
    d.mask = mask
    d.timestamp = ts
    d.checksum = cksum
    return d

public main() -> int:
    print(1)
    int64 max_int64 = 9223372036854775807
    int64 min_int64 = -9223372036854775808
    print(max_int64)
    print(min_int64)

    print(2)
    uint64 max_uint64 = 18446744073709551615
    uint64 zero_u = 0
    print(max_uint64)
    print(zero_u)

    print(3)
    int64 a = 5000000000000
    int64 b = 3000000000000
    int64 sum = a + b
    int64 diff = a - b
    int64 product = a * 2
    int64 quot = a / b
    print(sum)
    print(diff)
    print(product)
    print(quot)

    print(4)
    uint64 ua = 0xFFFFFFFF00000000
    uint64 ub = 0x00000000FFFFFFFF
    uint64 uand = ua & ub
    uint64 uor = ua | ub
    uint64 uxor = ua ^ ub
    uint64 uinvert = ~ua
    print(uand)
    print(uor)
    print(uxor)
    print(uinvert)

    print(5)
    int64 shl_val = a << 2
    int64 shr_val = a >> 2
    print(shl_val)
    print(shr_val)

    print(6)
    int64 hex_a = 0x7FFFFFFFFFFFFFFF
    int64 hex_b = 0x123456789ABCDFFF
    int64 hex_sum = hex_a + hex_b
    print(hex_sum)

    print(7)
    uint64 hex_u = 0xFFFFFFFFFFFFFFFF
    uint64 hex_u2 = 0xFDB9753102468ACF
    uint64 hex_uand = hex_u & hex_u2
    uint64 hex_uor = hex_u | hex_u2
    print(hex_uand)
    print(hex_uor)

    print(8)
    int64 neg = -1234567890123456789
    int64 abs_neg = 0 - neg
    print(neg)
    print(abs_neg)

    print(9)
    int small = 42
    int64 promoted = small
    int64 mixed = small + max_int64
    print(promoted)
    print(mixed)

    print(10)
    uint64 from_int64 = 1000
    print(from_int64)

    print(11)
    LargeData d
    d.id = 1234567890123456789
    d.mask = 0xFDB9753102468ACF
    d.timestamp = 1699999999999999999
    d.checksum = 12345
    print(d.id)
    print(d.mask)
    print(d.timestamp)
    print(d.checksum)

    print(12)
    int64 fibonacci_64 = 0
    int64 f1 = 0
    int64 f2 = 1
    int i = 0
    while i < 90:
        fibonacci_64 = f1 + f2
        f1 = f2
        f2 = fibonacci_64
        i = i + 1
    print(f1)

    print(13)
    int64 fact_64 = 1
    int j = 2
    while j <= 20:
        fact_64 = fact_64 * j
        j = j + 1
    print(fact_64)

    print(14)
    int64 prime_candidate = 999999999989
    int is_prime = 1
    int64 k = 2
    while k * k <= prime_candidate:
        if prime_candidate % k == int64(0):
            is_prime = 0
        k = k + 1
    print(is_prime)

    print(15)
    uint64 hash = 0xFFFFFFFFFFFFFFFF
    str message = "Hello64"
    int len = strlen(message)
    int c = 0
    while c < len:
        int byte_val = message[c]
        hash = hash ^ byte_val
        hash = hash * 0x100000001B3
        c = c + 1
    print(hash)

    return 0
