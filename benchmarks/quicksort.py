n = 100000
arr = [i * 12345 + 67890 for i in range(n)]
arr.sort()
print(arr[0])
print(arr[-1])
