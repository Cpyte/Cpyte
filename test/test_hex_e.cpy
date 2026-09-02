def main() -> int:
    # Regression: hex literals containing the letter 'e' as a hex digit must
    # be treated as integers, not floats. 0xff51afd7ed558ccd > 2^63-1.
    size_t a = (size_t)0xff51afd7ed558ccd
    size_t b = a ^ (a >> (size_t)33)
    if b == (size_t)0:
        print("hex-e fold FAIL")
        return 1
    int small = 0xdead
    if (small & 0xeeee) == 0:
        print("hex-e small FAIL")
        return 1
    size_t top = (size_t)0xffffffffffffffff
    if top != (size_t)0xffffffffffffffff:
        print("hex-e fullwidth FAIL")
        return 1
    print("hex-e: PASS")
    return 0
