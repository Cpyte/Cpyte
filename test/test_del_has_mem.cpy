# del / has / get_attr — heap pointers, array slots, pointer existence
#nogc
import stdlib

public def main() -> int:
    # has(ptr) — non-null check
    int* p = 0
    print(has(p))
    p = (int*)malloc(16)
    print(has(p))
    # del on a heap pointer -> free()
    del p
    print(has(p))
    # del arr[i] — clear the slot
    int[] arr = new int[4]
    arr[1] = 99
    print(arr[1])
    del arr[1]
    print(arr[1])
    del arr
    return 0
