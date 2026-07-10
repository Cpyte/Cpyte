# C Library Imports Example
# Demonstrates importing standard C libraries (stdio, stdlib, math, string, time)
# Usage: python mainpie.py --jit examples/c_library_imports.cpy

import "stdio"
import "stdlib"
import "math"
import "string"
import "time"

struct SensorReading:
    int id
    float value
    int timestamp

public main() -> int:
    print(1)
    srand(42)
    int r1 = rand()
    int r2 = rand()
    int r3 = rand()
    print(r1)
    print(r2)
    print(r3)

    print(2)
    float pi = 3.1415926535
    float sin_pi = sin(pi / 6.0)
    float cos_pi = cos(pi / 3.0)
    float tan_pi = tan(pi / 4.0)
    print(sin_pi)
    print(cos_pi)
    print(tan_pi)

    print(3)
    float val = 4.0
    float sq = sqrt(val)
    float cubed = pow(val, 3.0)
    float log_val = log(val)
    float exp_val = exp(1.0)
    print(sq)
    print(cubed)
    print(log_val)
    print(exp_val)

    print(4)
    float neg = -3.14159
    float abs_neg = fabs(neg)
    float rounded_floor = floor(3.99)
    float rounded_ceil = ceil(3.01)
    print(abs_neg)
    print(rounded_floor)
    print(rounded_ceil)

    print(5)
    str hello = "Hello, "
    str world = "World!"
    str hello_world = hello + world
    int len_hw = strlen(hello_world)
    print(hello_world)
    print(len_hw)

    print(6)
    str s1 = "apple"
    str s2 = "banana"
    int cmp = strcmp(s1, s2)
    print(cmp)
    int cmp2 = strcmp(s2, s1)
    print(cmp2)
    int cmp3 = strcmp(s1, s1)
    print(cmp3)

    print(7)
    str src = "Hello World"
    void* mem_buf = malloc(32)
    strcpy(mem_buf, src)
    print(mem_buf)
    free(mem_buf)

    print(8)
    str search = "World"
    str position = strstr(src, search)
    print(position)

    print(9)
    int t = time(0)
    print(t)

    print(10)
    int clock_ticks = clock()
    print(clock_ticks)

    print(11)
    str num_str = "12345"
    int parsed = atoi(num_str)
    print(parsed)

    print(12)
    float parsed_float = atof("3.14159")
    print(parsed_float)

    print(13)
    void* mem2 = malloc(100)
    memset(mem2, 0, 100)
    free(mem2)

    print(14)
    SensorReading sr1
    sr1.id = 1
    sr1.value = 36.5
    sr1.timestamp = 1000
    SensorReading sr2
    sr2.id = 2
    sr2.value = 37.2
    sr2.timestamp = 1001
    print(sr1.value)
    print(sr2.value)

    print(15)
    int64 large = 987654321012345678
    print(large)
    uint64 ularge = 0xFDB9753102468ACF
    print(ularge)

    return 0
