#include <stdio.h>
#include <stdlib.h>

int main(void) {
    for (int i = 0; i < 500000; i++) {
        void *p = malloc(64);
        *(int *)p = i;
        free(p);
    }
    printf("done\n");
    return 0;
}
