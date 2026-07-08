struct Point:
    int x
    int y

def make_point(x: int, y: int) -> Point:
    Point p
    p.x = x
    p.y = y
    return p

public main() -> int:x
    Point p
    p.x = 10
    p.y = 20
    print(p.x + p.y)

    int* ptr
    int val = 42
    ptr = &val
    print(*ptr)

    int[] arr
    arr = new int[5]
    arr[0] = 1
    arr[1] = 2
    print(arr[0] + arr[1])

    str s = "hello"
    str s2 = s + " world"
    print(0)

    return 0
