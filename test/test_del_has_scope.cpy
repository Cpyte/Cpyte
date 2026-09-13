# del in nested scopes + has() in a while condition

public main() -> int:
    int used = 0
    int i = 0
    while i < 2:
        int t = i + 10
        if has(t):
            used = used + t
        del t
        i = i + 1
    print(used)

    if has(missing_var):
        print(1)
    else:
        print(0)

    # del outside then redefined in nested block
    int q = 5
    del q
    if has(q):
        print(1)
    else:
        print(0)
    return 0