total = 0
for i in range(2_000_000):
    value = (i * 31 + 123) & 0x7FFFFFFF
    total ^= value >> (i % 16)
print(total)
