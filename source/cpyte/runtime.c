#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>

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

char* input_str(void) {
    size_t cap = 256;
    size_t len = 0;
    char *buf = malloc(cap);
    if (!buf) {
        fprintf(stderr, "input_str: allocation failed\n");
        exit(1);
    }
    int c;
    while ((c = getchar()) != EOF && c != '\n') {
        if (len + 1 >= cap) {
            cap *= 2;
            char *tmp = realloc(buf, cap);
            if (!tmp) {
                free(buf);
                fprintf(stderr, "input_str: allocation failed\n");
                exit(1);
            }
            buf = tmp;
        }
        buf[len++] = (char)c;
    }
    buf[len] = '\0';
    return buf;
}

int __cpy_strcmp(const char *s1, const char *s2) {
    return strcmp(s1, s2);
}
