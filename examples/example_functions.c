#include "example_functions.h"

int x_squared(int x) {
    return x * x;
}

int cube(int x) {
    return x * x * x;
}

int max_of(int a, int b) {
    return a > b ? a : b;
}

int min_of(int a, int b) {
    return a < b ? a : b;
}

int clamp(int value, int min, int max) {
    if (value < min) return min;
    if (value > max) return max;
    return value;
}

int is_even(int n) {
    return n % 2 == 0 ? 1 : 0;
}

int is_odd(int n) {
    return n % 2 != 0 ? 1 : 0;
}

int factorial_h(int n) {
    if (n <= 1) return 1;
    return n * factorial_h(n - 1);
}

int nCr(int n, int r) {
    if (r > n) return 0;
    if (r == 0 || r == n) return 1;
    return nCr(n - 1, r - 1) + nCr(n - 1, r);
}

int nPr(int n, int r) {
    if (r > n) return 0;
    int result = 1;
    for (int i = 0; i < r; i++) {
        result = result * (n - i);
    }
    return result;
}

int count_set_bits(int n) {
    int count = 0;
    while (n != 0) {
        count = count + (n & 1);
        n = n >> 1;
    }
    return count;
}

int has_even_parity(int n) {
    int count = count_set_bits(n);
    return count % 2 == 0 ? 1 : 0;
}

int next_power_of_two(int n) {
    if (n <= 0) return 1;
    int power = 1;
    while (power < n) {
        power = power * 2;
    }
    return power;
}

int sum_range(int start, int end) {
    int total = 0;
    for (int i = start; i <= end; i++) {
        total = total + i;
    }
    return total;
}
