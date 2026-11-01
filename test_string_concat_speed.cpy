# Test string concatenation speed
# This should benefit from all our optimizations

def main() -> int:
    # Test 1: Small string concatenation (benefits from inline assembly)
    s = ""
    for i in range(10):
        s = s + "x"
    print(s)

    # Test 2: Medium string concatenation (benefits from small string opt)
    s2 = ""
    for i in range(100):
        s2 = s2 + "ab"
    print(len(s2))

    # Test 3: Literal + dynamic (benefits from const concat)
    s3 = "hello"
    s3 = s3 + " world"
    print(s3)

    # Test 4: Dynamic + literal (benefits from const concat)
    s4 = "foo"
    s4 = "bar" + s4
    print(s4)

    return 0
