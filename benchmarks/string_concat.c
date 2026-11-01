#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main() {
    char *s = strdup("");
    for (int i = 0; i < 10000; i++) {
        char *new_s = malloc(strlen(s) + 6);
        strcpy(new_s, s);
        strcat(new_s, "hello");
        free(s);
        s = new_s;
    }
    printf("%ld\n", strlen(s));
    free(s);
    return 0;
}
