# Test 64-bit integer support

def test_int64():
    int64 a = 9223372036854775807
    int64 b = -9223372036854775808
    int64 c = a + b
    print(c)

def test_uint64():
    uint64 x = 18446744073709551615
    uint64 y = 0xFFFFFFFFFFFFFFFF
    uint64 z = x + y
    print(z)

def test_hex_literals():
    int64 max_int64 = 0x7FFFFFFFFFFFFFFF
    int64 min_int64 = 0x8000000000000000
    uint64 max_uint64 = 0xFFFFFFFFFFFFFFFF
    print(max_int64)
    print(min_int64)
    print(max_uint64)

def test_arithmetic():
    int64 a = 1000000000000
    int64 b = 2000000000000
    int64 sum = a + b
    int64 diff = b - a
    int64 product = a * 100
    print(sum)
    print(diff)
    print(product)

test_int64()
test_uint64()
test_hex_literals()
test_arithmetic()
