import "stdio"
import "string"
import "math"

# ── Self-referential struct (BST node) ──────────────────────────
struct BSTNode:
    int data
    BSTNode* left
    BSTNode* right

# ── Generic-like pair struct ────────────────────────────────────
struct Pair:
    int first
    int second

# ── Nested structs ──────────────────────────────────────────────
struct Point:
    int x
    int y

struct Line:
    Point start
    Point end

# ── BST operations ──────────────────────────────────────────────
public def insert(root BSTNode*, value int) -> BSTNode*:
    if root == 0:
        BSTNode* node = new BSTNode
        (*node).data = value
        (*node).left = 0
        (*node).right = 0
        return node
    if value < (*root).data:
        (*root).left = insert((*root).left, value)
    elif value > (*root).data:
        (*root).right = insert((*root).right, value)
    return root

def inorder(root BSTNode*):
    if root == 0:
        return
    inorder((*root).left)
    printf("%d ", (*root).data)
    inorder((*root).right)

def count_nodes(root BSTNode*) -> int:
    if root == 0:
        return 0
    return 1 + count_nodes((*root).left) + count_nodes((*root).right)

def tree_height(root BSTNode*) -> int:
    if root == 0:
        return 0
    int left_h = tree_height((*root).left)
    int right_h = tree_height((*root).right)
    if left_h > right_h:
        return left_h + 1
    return right_h + 1

def search(root BSTNode*, value int) -> int:
    if root == 0:
        return 0
    if (*root).data == value:
        return 1
    if value < (*root).data:
        return search((*root).left, value)
    return search((*root).right, value)

# ── Recursive math ──────────────────────────────────────────────
def factorial(n int) -> int:
    if n <= 1:
        return 1
    return n * factorial(n - 1)

def fibonacci(n int) -> int:
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)

def gcd(a int, b int) -> int:
    while b != 0:
        int t = b
        b = a % b
        a = t
    return a

# ── Bitwise hash ────────────────────────────────────────────────
def bitwise_hash(s: str) -> int:
    int h = 0
    int i = 0
    int len = strlen(s)
    while i < len:
        int c = s[i]
        h = h ^ ((h << 5) + (h >> 2) + c)
        i += 1
    return h

# ── Collatz sequence ────────────────────────────────────────────
def collatz_steps(n int) -> int:
    int steps = 0
    while n > 1:
        if n % 2:
            n = n * 3 + 1
        else:
            n //= 2
        steps += 1
    return steps

# ── Main ────────────────────────────────────────────────────────
def main():
    printf("========== CPY MAXIMUM COMPLEXITY DEMO ==========\n\n")

    # ── Part 1: BST ─────────────────────────────────────────────
    printf("--- BST (Binary Search Tree) ---\n")
    BSTNode* root = 0
    root = insert(root, 50)
    root = insert(root, 30)
    root = insert(root, 20)
    root = insert(root, 40)
    root = insert(root, 70)
    root = insert(root, 60)
    root = insert(root, 80)
    root = insert(root, 10)
    root = insert(root, 45)
    root = insert(root, 55)
    root = insert(root, 75)
    root = insert(root, 65)
    root = insert(root, 85)

    printf("Inorder: ")
    inorder(root)
    printf("\n")

    printf("Count : %d\n", count_nodes(root))
    printf("Height: %d\n", tree_height(root))

    int sv = 55
    if search(root, sv):
        printf("%d FOUND in tree\n", sv)
    else:
        printf("%d NOT in tree\n", sv)

    sv = 999
    if search(root, sv):
        printf("%d FOUND in tree\n", sv)
    else:
        printf("%d NOT in tree\n", sv)

    # ── Part 2: Recursive math ──────────────────────────────────
    printf("\n--- Recursive Math ---\n")
    int fact10 = factorial(10)
    printf("10! = %d\n", fact10)

    int fib15 = fibonacci(15)
    printf("fib(15) = %d\n", fib15)

    printf("gcd(48, 18) = %d\n", gcd(48, 18))
    printf("gcd(1071, 462) = %d\n", gcd(1071, 462))

    # ── Part 3: Nested structs ──────────────────────────────────
    printf("\n--- Nested Structs ---\n")
    Point p1
    p1.x = 10
    p1.y = 20

    Point p2
    p2.x = 30
    p2.y = 40

    Line ln
    ln.start = p1
    ln.end = p2
    printf("Line: (%d,%d) -> (%d,%d)\n", ln.start.x, ln.start.y, ln.end.x, ln.end.y)

    # ── Part 4: Pointer ops ─────────────────────────────────────
    printf("\n--- Pointers & Heap ---\n")
    int xv = 42
    int* ptr = &xv
    printf("&xv -> %d\n", *ptr)
    *ptr = 99
    printf("After *ptr = 99, xv = %d\n", xv)

    int* hp = new int
    *hp = 77
    printf("Heap int: %d\n", *hp)
    printf("sizeof(int)  = %d\n", sizeof(int))
    printf("sizeof(Point)= %d\n", sizeof(Point))

    # ── Part 5: Arrays ──────────────────────────────────────────
    printf("\n--- Arrays ---\n")
    int[] arr = new int[10]
    int ai = 0
    while ai < 10:
        arr[ai] = ai * ai
        ai += 1
    ai = 0
    while ai < 10:
        printf("arr[%d] = %d\n", ai, arr[ai])
        ai += 1

    # ── Part 6: String ops ──────────────────────────────────────
    printf("\n--- String Operations ---\n")
    str hello = "Hello"
    str world = " World"
    str msg = hello + world
    printf("Concat: \"%s\"\n", msg)
    printf("Length: %d\n", strlen(msg))
    printf("First char: %c\n", msg[0])
    printf("Contains 'World': %s\n", strstr(msg, "World"))

    int cmp = strcmp(hello, "Hello")
    if cmp == 0:
        printf("strcmp: equal\n")
    elif cmp < 0:
        printf("strcmp: less\n")
    else:
        printf("strcmp: greater\n")

    str dup = strdup(msg)
    printf("strdup: %s\n", dup)

    # ── Part 7: Bitwise hash ────────────────────────────────────
    printf("\n--- Bitwise Hash ---\n")
    str text = "Hello, Cpyte!"
    int hash = bitwise_hash(text)
    printf("Hash of \"%s\" = 0x%X\n", text, hash)

    # ── Part 8: Collatz conjecture ──────────────────────────────
    printf("\n--- Collatz Conjecture ---\n")
    int ci = 1
    while ci <= 20:
        int st = collatz_steps(ci)
        printf("collatz(%2d) = %2d steps\n", ci, st)
        ci += 1

    # ── Part 9: 64-bit integers ─────────────────────────────────
    printf("\n--- 64-bit Integers ---\n")
    int64 big = 0x7FFFFFFFFFFFFFFF
    uint64 ub = 0xFFFFFFFFFFFFFFFF
    printf("int64 max:  %lld\n", big)
    printf("uint64 max:  %llu\n", ub)

    # Mixed 32/64 bit arithmetic
    int64 big_sum = big + 1000
    printf("big + 1000 = %lld\n", big_sum)

    # ── Part 10: Float / Math lib ───────────────────────────────
    printf("\n--- Float & Math Library ---\n")
    float pi = 3.14159
    float e = 2.71828
    printf("pi = %.6f\n", pi)
    printf("e  = %.6f\n", e)
    printf("pi + e = %.6f\n", pi + e)
    printf("pi * e = %.6f\n", pi * e)
    printf("sqrt(2) = %.6f\n", sqrt(2.0))
    printf("sin(pi/2) = %.6f\n", sin(pi / 2.0))
    printf("cos(0) = %.6f\n", cos(0.0))
    printf("pow(2.0, 10.0) = %.6f\n", pow(2.0, 10.0))
    printf("log(e) = %.6f\n", log(e))
    printf("floor(3.7) = %.6f\n", floor(3.7))
    printf("ceil(3.2) = %.6f\n", ceil(3.2))
    printf("fabs(-5.5) = %.6f\n", fabs(-5.5))
    printf("fmod(10.5, 3.0) = %.6f\n", fmod(10.5, 3.0))

    # ── Part 11: Binary ops (all operators) ─────────────────────
    printf("\n--- Operator Showcase ---\n")
    int aa = 60
    int bb = 13
    printf("a=%d b=%d\n", aa, bb)
    printf("a + b  = %d\n", aa + bb)
    printf("a - b  = %d\n", aa - bb)
    printf("a * b  = %d\n", aa * bb)
    printf("a / b  = %d\n", aa / bb)
    printf("a // b = %d\n", aa // bb)
    printf("a %% b   = %d\n", aa % bb)
    printf("a & b  = %d\n", aa & bb)
    printf("a | b  = %d\n", aa | bb)
    printf("a ^ b  = %d\n", aa ^ bb)
    printf("~a     = %d\n", ~aa)
    printf("a << 2 = %d\n", aa << 2)
    printf("a >> 2 = %d\n", aa >> 2)
    printf("2 ** 10 = %d\n", 2 ** 10)
    printf("a < b : %d\n", aa < bb)
    printf("a > b : %d\n", aa > bb)
    printf("a == b: %d\n", aa == bb)
    printf("a != b: %d\n", aa != bb)
    printf("a <= b: %d\n", aa <= bb)
    printf("a >= b: %d\n", aa >= bb)

    # Unary
    printf("+a = %d\n", +aa)
    printf("-a = %d\n", -aa)
    printf("not a = %d\n", not aa)
    printf("~~a = %d\n", ~~aa)

    # ── Part 12: Compound assignments ───────────────────────────
    printf("\n--- Compound Assignments ---\n")
    int cv = 10
    printf("init: %d\n", cv)
    cv += 5
    printf("+=5 : %d\n", cv)
    cv -= 3
    printf("-=3 : %d\n", cv)
    cv *= 2
    printf("*=2 : %d\n", cv)
    cv //= 4
    printf("//=4: %d\n", cv)

    # ── Part 13: 67() easter egg ────────────────────────────────
    printf("\n--- Easter Egg ---\n")
    printf("67() = %s\n", 67())

    # ── Part 14: Nested if/elif/else + short-circuit ──────────────
    printf("\n--- Control Flow ---\n")
    int fib_i = 0
    while fib_i < 15:
        int f = fibonacci(fib_i)
        if f % 2:
            if f % 3:
                printf("fib(%2d) = %-4d (odd, not div by 3)\n", fib_i, f)
            elif f % 5:
                printf("fib(%2d) = %-4d (odd, div by 3 not 5)\n", fib_i, f)
            else:
                printf("fib(%2d) = %-4d (odd, div by 3 and 5)\n", fib_i, f)
        else:
            printf("fib(%2d) = %-4d (even)\n", fib_i, f)
        fib_i += 1

    # Short-circuit evaluation (not dividing by zero via short-circuit)
    int flag = 0
    if flag != 0 and 100 / flag > 5:
        printf("SHOULD NOT PRINT (short-circuit fail)\n")
    else:
        printf("Short-circuit AND: correct (skipped div by zero)\n")

    if flag == 0 or 100 / flag > 5:
        printf("Short-circuit OR: correct (skipped second eval)\n")

    # ── Part 15: Sieve of Eratosthenes ──────────────────────────
    printf("\n--- Primes up to 50 ---\n")
    int limit = 50
    int[] sieve_arr = new int[limit + 1]
    int si = 2
    while si <= limit:
        sieve_arr[si] = 1
        si += 1
    si = 2
    while si * si <= limit:
        if sieve_arr[si]:
            int sj = si * si
            while sj <= limit:
                sieve_arr[sj] = 0
                sj += si
        si += 1
    si = 2
    while si <= limit:
        if sieve_arr[si]:
            printf("%d ", si)
        si += 1
    printf("\n")

    printf("\n========== END ==========\n")
