import sys

for i in range(500000):
    p = bytearray(64)
    p[0] = i & 0xFF
print("done")
