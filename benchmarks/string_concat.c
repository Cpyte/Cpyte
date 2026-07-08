#include <stdio.h>
#include <string.h>
#include <stdlib.h>

int main(void) {
    char *s = strdup("");
    for (int i = 0; i < 10000; i++) {
        char *tmp = malloc(strlen(s) + 6);
        sprintf(tmp, "%shello", s);
        free(s);
        s = tmp;
    }
    printf("%zu\n", strlen(s));
    free(s);
    return 0;
}
