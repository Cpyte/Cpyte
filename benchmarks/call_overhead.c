#include <stdio.h>

typedef struct {
    int (*apply)(int);
} Operation;

__attribute__((noinline))
static int increment(int value) {
    return value + 1;
}

__attribute__((noinline))
static int double_value(int value) {
    return value * 2;
}

int main(void) {
    Operation increment_op = {increment};
    Operation double_op = {double_value};
    int total = 0;
    for (int i = 0; i < 1000000; ++i) {
        Operation* current = i % 2 == 0 ? &increment_op : &double_op;
        total += current->apply(i % 100);
    }
    printf("%d\n", total);
    return 0;
}
