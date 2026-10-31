total = 0
for i in range(2_000_000):
    total += (i * 17 + 3) % 97
print(total)
