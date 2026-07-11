def quicksort(arr: int[], left: int, right: int):
    int i = left
    int j = right
    int pivot = arr[(left + right) / 2]
    while i <= j:
        while arr[i] < pivot:
            i = i + 1
        while arr[j] > pivot:
            j = j - 1
        if i <= j:
            int tmp = arr[i]
            arr[i] = arr[j]
            arr[j] = tmp
            i = i + 1
            j = j - 1
    if left < j:
        quicksort(arr, left, j)
    if i < right:
        quicksort(arr, i, right)

public main() -> int:
    int n = 100000
    int[] arr = new int[n]
    int i = 0
    while i < n:
        arr[i] = i * 12345 + 67890
        i = i + 1
    quicksort(arr, 0, n - 1)
    print(arr[0])
    print(arr[n - 1])
    return 0
