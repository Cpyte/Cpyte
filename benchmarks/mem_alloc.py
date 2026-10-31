for i in range(5000):
    p = bytearray(64)
    p[0] = i & 0xFF
    del p
print("done")
