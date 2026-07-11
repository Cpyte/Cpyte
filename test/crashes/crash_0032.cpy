struct S1:
    double prev

uint64 g2

def main() -> int:
    g2 = (((g2 // (-9223372036854775808 / g2)) / 18446744073709551615) << -48035660)
    print((9223372036854775808 | g2))
    return 0