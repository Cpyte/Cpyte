int g = 0

def main() -> int:
    int x = (int)(size_t)&g
    int bad = 0
    if x * 4294967295 != 0 - x:
        bad = 1
    if x * 4294967296 != 0:
        bad = 2
    if x * 4294967297 != x:
        bad = 3
    if x * 8589934592 != 0:
        bad = 4
    if bad == 0:
        print("ok")
    else:
        print("FAIL")
    return bad
