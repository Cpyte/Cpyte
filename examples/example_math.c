#include <math.h>

int c_add(int x, int y) {
    return x + y;
}

int c_multiply(int x, int y) {
    return x * y;
}

int c_subtract(int x, int y) {
    return x - y;
}

int c_divide(int x, int y) {
    if (y == 0) return 0;
    return x / y;
}

int c_modulus(int x, int y) {
    if (y == 0) return 0;
    return x % y;
}

int c_factorial(int n) {
    if (n <= 1) return 1;
    return n * c_factorial(n - 1);
}

int c_fibonacci(int n) {
    if (n <= 1) return n;
    return c_fibonacci(n - 1) + c_fibonacci(n - 2);
}

int c_gcd(int a, int b) {
    while (b != 0) {
        int temp = b;
        b = a % b;
        a = temp;
    }
    return a;
}

int c_lcm(int a, int b) {
    return (a / c_gcd(a, b)) * b;
}

int c_is_prime(int n) {
    if (n <= 1) return 0;
    for (int i = 2; i * i <= n; i++) {
        if (n % i == 0) return 0;
    }
    return 1;
}

int c_reverse_number(int n) {
    int rev = 0;
    while (n != 0) {
        rev = rev * 10 + n % 10;
        n = n / 10;
    }
    return rev;
}

int c_sum_of_digits(int n) {
    int sum = 0;
    while (n != 0) {
        sum = sum + n % 10;
        n = n / 10;
    }
    return sum;
}

double c_vector_length(double x, double y) {
    return sqrt(x * x + y * y);
}

int c_array_sum(int n) {
    int total = 0;
    for (int i = 0; i < n; i++) {
        total = total + i * 2;
    }
    return total;
}


