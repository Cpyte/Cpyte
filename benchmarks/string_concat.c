#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    size_t len = 0;
    size_t cap = 16;

    char *s = malloc(cap);
    s[0] = '\0';

    for (int i = 0; i < 10000; i++) {
        if (len + 5 + 1 > cap) {
            cap *= 2;
            s = realloc(s, cap);
        }

        memcpy(s + len, "hello", 5);
        len += 5;
        s[len] = '\0';
    }

    printf("%zu\n", strlen(s));
    free(s);
    return 0;
}
