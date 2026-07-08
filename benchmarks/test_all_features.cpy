struct Point:
    int x
    int y

struct Line:
    Point start
    Point end

struct ListNode:
    int value
    ListNode* next

struct Pair<T, U>:
    T first
    U second

public make_point(x: int, y: int) -> Point:
    Point p
    p.x = x
    p.y = y
    return p

public make_list(n: int) -> ListNode*:
    ListNode* head
    ListNode* cur
    int i = 0
    while i < n:
        ListNode* node
        node = new ListNode
        (*node).value = i * 10
        (*node).next = 0
        if head == 0:
            head = node
        else:
            (*cur).next = node
        cur = node
        i = i + 1
    return head

public sum_list(head: ListNode*) -> int:
    int total = 0
    ListNode* cur = head
    while cur != 0:
        total = total + (*cur).value
        cur = (*cur).next
    return total

public main() -> int:
    print(1)
    print(2)
    print(3)
    int a = 10
    int b = 20
    int c = a + b
    print(c)

    print(4)
    Point p1
    p1.x = 3
    p1.y = 7
    print(p1.x + p1.y)

    print(5)
    Line line
    line.start.x = 1
    line.start.y = 2
    line.end.x = 3
    line.end.y = 4
    print(line.start.x + line.start.y + line.end.x + line.end.y)

    print(6)
    Pair<int, int> pair
    pair.first = 100
    pair.second = 200
    print(pair.first + pair.second)

    print(7)
    Pair<int, int> p2
    p2.first = 50
    p2.second = 60
    print(p2.first + p2.second)

    print(8)
    Point mp = make_point(9, 10)
    print(mp.x + mp.y)

    print(9)
    int x = 42
    int* ptr
    ptr = &x
    print(*ptr)
    *ptr = 99
    print(x)
    print(*ptr)

    print(10)
    int** pptr
    pptr = &ptr
    print(**pptr)

    print(11)
    int[] arr
    arr = new int[4]
    arr[0] = 1
    arr[1] = 2
    arr[2] = 3
    arr[3] = 4
    print(arr[0] + arr[1] + arr[2] + arr[3])

    print(12)
    str s = "Hello"
    print(s)
    str s2 = " World"
    str s3 = s + s2
    print(s3)

    print(13)
    ListNode* list = make_list(5)
    print(sum_list(list))

    print(14)
    int sz = sizeof(Point)
    print(sz)

    print(15)
    Point[] parr
    parr = new Point[3]
    parr[0].x = 10
    parr[0].y = 20
    parr[1].x = 30
    parr[1].y = 40
    print(parr[0].x + parr[0].y + parr[1].x + parr[1].y)

    print(16)
    int a0 = 5
    int a1 = 15
    int* ap0 = &a0
    int* ap1 = &a1
    print(*ap0 + *ap1)

    print(17)
    print(0)
    return 0
