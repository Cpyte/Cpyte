uint64 g1
int g2 = -786418822
int g3 = (+g2 | 18446744073709551615)
float g4

def main() -> int:
    print(new int[((4294967295 // g2) | 2147483648)])
    return 0