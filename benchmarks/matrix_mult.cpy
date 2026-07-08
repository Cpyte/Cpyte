public main() -> int:
    int n = 100
    double[] a = new double[n * n]
    double[] b = new double[n * n]
    double[] c = new double[n * n]

    int i = 0
    while i < n:
        int j = 0
        while j < n:
            a[i * n + j] = 1.0
            b[i * n + j] = 2.0
            c[i * n + j] = 0.0
            j = j + 1
        i = i + 1

    i = 0
    while i < n:
        int k = 0
        while k < n:
            int j = 0
            while j < n:
                c[i * n + j] = c[i * n + j] + a[i * n + k] * b[k * n + j]
                j = j + 1
            k = k + 1
        i = i + 1

    print(c[0])
    return 0
