#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

void print_int(int n) {
    printf("%d\n", n);
}

void print_int64(int64_t n) {
    printf("%lld\n", (long long)n);
}

void print_uint64(uint64_t n) {
    printf("%llu\n", (unsigned long long)n);
}

void print_double(double d) {
    printf("%f\n", d);
}

void print_str(const char *s) {
    printf("%s\n", s);
}

int input_int(void) {
    int n;
    if (scanf("%d", &n) != 1) {
        fprintf(stderr, "input_int: failed to read integer\n");
        exit(1);
    }
    return n;
}
