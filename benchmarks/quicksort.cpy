import stdlib

def qsort(arr: int[], left: int, right: int):
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
        qsort(arr, left, j)
    if i < right:
        qsort(arr, i, right)

public main() -> int:
    int n = 10000
    int[] arr = new int[n]
    int i = 0
    while i < n:
        arr[i] = rand()
        i = i + 1
    qsort(arr, 0, n - 1)
    print(arr[0])
    print(arr[n - 1])
    return 0
