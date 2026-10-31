#include <stdio.h>

int main(void) {
    double value = 0.25;
    for (int i = 0; i < 2000000; ++i)
        value = value * 1.0000001 + 0.0000005;
    printf("%.6f\n", value);
    return 0;
}
