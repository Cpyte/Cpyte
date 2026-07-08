#include <stdio.h>
#include <stdlib.h>

void print_int(int n) {
    printf("%d\n", n);
}

void print_double(double d) {
    printf("%f\n", d);
}

int input_int(void) {
    int n;
    if (scanf("%d", &n) != 1) {
        fprintf(stderr, "input_int: failed to read integer\n");
        exit(1);
    }
    return n;
}
