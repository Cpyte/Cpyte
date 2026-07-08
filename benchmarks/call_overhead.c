#include <stdio.h>

__attribute__((noinline))
static int recurse(int n) {
    if (n <= 0) return 0;
    return recurse(n - 1) + 1;
}

int main(void) {
    int total = 0;
    for (int i = 0; i < 10000; i++)
        total += recurse(5000);
    printf("%d\n", total);
    return 0;
}
