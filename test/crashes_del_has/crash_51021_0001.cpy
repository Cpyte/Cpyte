struct S1:
    str name
    big head

struct S2:
    uint64 name
    int next
    str value

uint64 g3 = 18446744073709551615
int g4 = ((g3 + ((~1441574174183445149 * g3) % 805637849)) % 16930957771346712504)

def main() -> int:
    del g3
    return 0