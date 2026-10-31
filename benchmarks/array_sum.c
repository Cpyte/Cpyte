#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    const size_t count = 1000000;
    int64_t *values = malloc(count * sizeof(*values));
    if (values == NULL) return 1;
    for (size_t i = 0; i < count; ++i) values[i] = (int64_t)i % 97;
    int64_t sum = 0;
    for (size_t i = 0; i < count; ++i) sum += values[i];
    printf("%lld\n", (long long)sum);
    return 0;
}
