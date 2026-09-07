def main():
    int i = 0
    int acc = 0
    while i < 5:
        acc = acc + i
        i = i + 1
    print(acc)
    print(i)

    int k = 10
    while k > 0:
        print(k)
        k = k - 2
    print(k)

    int m = 0
    while m < 3:
        m = m + 1
    print(m)

    int j = 1
    while j < 20:
        j = j + 1
    print(j)

    int x = 0
    while x < 4:
        if x == 2:
            print(100)
        x = x + 1
    print(x)

    int total = 0
    for v in [1, 2, 3, 4]:
        total = total + v
    print(total)

    int n = 0
    while n < 0:
        n = n + 1
    print(n)