#include <stdint.h>
#include <stdio.h>

int main(void) {
    int64_t total = 0;
    for (int i = 0; i < 2000000; ++i) {
        if (i % 3 == 0) total += i % 97;
        else if (i % 5 == 0) total -= i % 31;
        else total += 1;
    }
    printf("%lld\n", (long long)total);
    return 0;
}
