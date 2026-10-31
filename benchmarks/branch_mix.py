total = 0
for i in range(2_000_000):
    if i % 3 == 0:
        total += i % 97
    elif i % 5 == 0:
        total -= i % 31
    else:
        total += 1
print(total)
