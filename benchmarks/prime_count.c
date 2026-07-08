#include <stdio.h>

int is_prime(int n) {
    if (n < 2) return 0;
    if (n == 2) return 1;
    if (n % 2 == 0) return 0;
    for (int i = 3; i * i <= n; i += 2)
        if (n % i == 0) return 0;
    return 1;
}

int main(void) {
    int count = 0;
    for (int n = 2; n < 1000000; n++)
        count += is_prime(n);
    printf("%d\n", count);
    return 0;
}
