# Mixed Features Example
# Combines 64-bit support, structs, pointers, arrays, standard library imports,
# and advanced control flow in a single comprehensive program
# Usage: python source/mainpie.py --jit examples/mixed_features.cpy

import "stdio"
import "stdlib"
import "math"
import "string"

struct BigNumber:
    int64 high
    uint64 low
    str label

struct Node:
    int64 value
    uint64 flags
    Node* next

struct ResultSet:
    int64 sum
    int64 product
    float average
    int64 min_val
    int64 max_val
    int count

public create_linked_list(n: int64) -> Node*:
    Node* head
    Node* cur
    int64 i = 0
    while i < n:
        Node* node = new Node
        node.value = i * i
        node.flags = i * i * 2
        node.next = 0
        if head == 0:
            head = node
        else:
            cur.next = node
        cur = node
        i = i + 1
    return head

public sum_linked_list(head: Node*) -> int64:
    int64 total
    total = 0
    Node* cur = head
    while cur != 0:
        total = total + cur.value
        cur = cur.next
    return total

public main() -> int:
    print(1)
    int64 n = 10
    Node* list = create_linked_list(n)
    int64 total = sum_linked_list(list)
    print(total)

    print(2)
    float fval = 2.0
    int i = 0
    while i < 10:
        float root = sqrt(fval)
        float power = pow(fval, 3.0)
        print(root)
        print(power)
        fval = fval + 1.0
        i = i + 1

    print(3)
    float[] angles
    angles = new float[4]
    angles[0] = 0.0
    angles[1] = 0.5
    angles[2] = 1.0
    angles[3] = 1.5
    int k = 0
    while k < 4:
        float s = sin(angles[k] * 3.14159)
        print(s)
        k = k + 1

    print(4)
    int64 arr_sum = 0
    int64[] big_arr = new int64[16]
    int m = 0
    while m < 16:
        big_arr[m] = m * 1000000000000
        arr_sum = arr_sum + big_arr[m]
        m = m + 1
    print(arr_sum)

    print(5)
    BigNumber bn
    bn.high = 1234567890123456789
    bn.low = 0xFDB9753102468ACF
    bn.label = "BigNumber"
    print(bn.high)
    print(bn.low)

    print(6)
    int64* ptr_to_64 = new int64
    *ptr_to_64 = 123456789012345678
    print(*ptr_to_64)

    print(7)
    uint64* ptr_to_u64 = new uint64
    *ptr_to_u64 = 0xFFFFFFFF00000000
    print(*ptr_to_u64)

    print(8)
    uint64 ua = 0xFFFFFFFF00000000
    uint64 ub = 0x00000000FFFFFFFF
    uint64 uand = ua & ub
    uint64 uor = ua | ub
    uint64 uxor = ua ^ ub
    print(uand)
    print(uor)
    print(uxor)

    print(9)
    int64 shl_val = total << 2
    int64 shr_val = total >> 2
    print(shl_val)
    print(shr_val)

    print(10)
    int64 hex_a = 0x7FFFFFFFFFFFFFFF
    int64 hex_b = 0x123456789ABCDFFF
    int64 hex_sum = hex_a + hex_b
    print(hex_sum)

    print(11)
    uint64 hex_u = 0xFFFFFFFFFFFFFFFF
    uint64 hex_u2 = 0xFDB9753102468ACF
    uint64 hex_uand = hex_u & hex_u2
    uint64 hex_uor = hex_u | hex_u2
    print(hex_uand)
    print(hex_uor)

    print(12)
    int64 neg = -1234567890123456789
    int64 abs_neg = 0 - neg
    print(neg)
    print(abs_neg)

    print(13)
    int small = 42
    int64 promoted = small
    int64 mixed = small + hex_a
    print(promoted)
    print(mixed)

    print(14)
    str fmt = "Hello"
    printf(fmt)

    print(15)
    uint64 mask = 0xFFFFFFFFFFFFFFFF
    print(mask)

    return 0
