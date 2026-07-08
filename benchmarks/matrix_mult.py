import sys

n = 200
a = [[1.0] * n for _ in range(n)]
b = [[2.0] * n for _ in range(n)]
c = [[0.0] * n for _ in range(n)]

for i in range(n):
    for k in range(n):
        aik = a[i][k]
        for j in range(n):
            c[i][j] += aik * b[k][j]

print(f"{c[0][0]:.6f}")
