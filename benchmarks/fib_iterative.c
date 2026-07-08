#include <stdio.h>

int fib(int n) {
    int a = 0, b = 1;
    while (n-- > 0) {
        int t = a + b;
        a = b;
        b = t;
    }
    return a;
}

int main(void) {
    long long total = 0;
    for (int n = 0; n < 10000; n++)
        total += fib(n);
    printf("%lld\n", total);
    return 0;
}
