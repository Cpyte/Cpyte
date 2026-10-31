#include <stdint.h>
#include <stdio.h>

int main(void) {
    int64_t total = 0;
    for (int64_t i = 0; i < 2000000; ++i)
        total += (i * 17 + 3) % 97;
    printf("%lld\n", (long long)total);
    return 0;
}
