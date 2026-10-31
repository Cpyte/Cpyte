value = 0.25
for _ in range(2_000_000):
    value = value * 1.0000001 + 0.0000005
print(f"{value:.6f}")
