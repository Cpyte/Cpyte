#include <stdio.h>

int main(void) {
    int total = 0;
    for (int i = 0; i < 2000000; ++i) {
        int value = (i * 31 + 123) & 0x7fffffff;
        total ^= value >> (i % 16);
    }
    printf("%d\n", total);
    return 0;
}
