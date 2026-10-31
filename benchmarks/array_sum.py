count = 1_000_000
values = [0] * count
for i in range(count):
    values[i] = i % 97
total = 0
for value in values:
    total += value
print(total)
