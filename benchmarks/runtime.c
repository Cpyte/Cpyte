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

void print_hex(int64_t n) {
    printf("0x%llx\n", (unsigned long long)n);
}

void print_double(double d) {
    printf("%f\n", d);
}

void print_str(const char *s) {
    printf("%s\n", s);
}

static char *
bench_dupstr(const char *buf) {
    size_t len = strlen(buf);
    char *s = malloc(len + 1);
    if (!s) return NULL;
    memcpy(s, buf, len + 1);
    return s;
}

char *
str_of_int64(int64_t n) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%lld", (long long)n);
    return bench_dupstr(buf);
}

char *
str_of_uint64(uint64_t n) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%llu", (unsigned long long)n);
    return bench_dupstr(buf);
}

char *
str_of_ptr(int64_t n) {
    char buf[32];
    snprintf(buf, sizeof(buf), "0x%llx", (unsigned long long)n);
    return bench_dupstr(buf);
}

char *
str_of_double(double d) {
    char buf[64];
    snprintf(buf, sizeof(buf), "%.6f", d);
    return bench_dupstr(buf);
}

int input_int(void) {
    int n;
    if (scanf("%d", &n) != 1) {
        fprintf(stderr, "input_int: failed to read integer\n");
        exit(1);
    }
    return n;
}
