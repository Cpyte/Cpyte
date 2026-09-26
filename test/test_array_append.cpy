def main() -> int:
    int[] a = new int[0]
    append(a, 10)
    append(a, 20)
    append(a, 30)
    print("len=", len(a))
    print("\n")
    print(a[0], a[1], a[2])
    print("\n")

    int[] b = new int[2]
    append(b, 99)
    print("presized len=", len(b))
    print("\n")
    print(b[0], b[1], b[2])
    print("\n")

    int64[] r = range(5)
    append(r, 99)
    print("range+1 len=", len(r))
    print("\n")
    print(r[5])
    print("\n")

    str[] ss = new str[0]
    append(ss, "hello")
    append(ss, "world")
    print("str len=", len(ss))
    print("\n")
    print(ss[0], ss[1])
    print("\n")

    double[] d = new double[0]
    append(d, 1.5)
    append(d, 2.5)
    print("double len=", len(d))
    print("\n")
    print(d[0], d[1])
    print("\n")

    dynamic[] dl = [1, "two", 3.0]
    append(dl, 4)
    print("dyn len=", len(dl))
    print("\n")
    print(dl[0], dl[1], dl[2], dl[3])
    print("\n")

    dynamic[] dynarr = [ [1, 2], [3, 4] ]
    append(dynarr[0], 99)
    print("dyn-elem:", dynarr[0][0], dynarr[0][1], dynarr[0][2])
    print("\n")

    int[] growth = new int[0]
    int i = 0
    while i < 20:
        append(growth, i * i)
        i = i + 1
    print("growth len=", len(growth))
    print("\n")
    print(growth[19])
    print("\n")

    int[] from_scratch = new int[0]
    append(from_scratch, 4)
    append(from_scratch, 5)
    append(from_scratch, 6)
    append(from_scratch, 7)
    print("scratch len=", len(from_scratch))
    print("\n")
    print(from_scratch[3])
    print("\n")

    return 0