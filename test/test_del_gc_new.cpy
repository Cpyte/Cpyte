# del on a GC-managed `new` pointer must NOT free() the block
# Regression corpus for Sept 2026 feature work.
# Runs WITHOUT `#nogc`: `new T` allocates via gc_malloc, whose returned
# pointer is interior to a collector header. `del p` therefore unbinds the
# name only (the collector reclaims the block); a manual free() here would
# abort at runtime with an invalid free.
struct Node:
    int value

public def main() -> int:
    char* c = (new char)
    c[0] = 99
    print(int(c[0]))
    del c
    print(has(c))
    Node* n = (new Node)
    n.value = 7
    print(n.value)
    del n
    print(has(n))
    int[] i = (new int[4])
    i[1] = 5
    print(i[1])
    del i
    return 0