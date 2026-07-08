#include <stdio.h>
#include <stdlib.h>

int main(void) {
    int n = 200;
    double *a = malloc(n * n * sizeof(double));
    double *b = malloc(n * n * sizeof(double));
    double *c = calloc(n * n, sizeof(double));

    for (int i = 0; i < n; i++)
        for (int j = 0; j < n; j++) {
            a[i * n + j] = 1.0;
            b[i * n + j] = 2.0;
        }

    for (int i = 0; i < n; i++)
        for (int k = 0; k < n; k++)
            for (int j = 0; j < n; j++)
                c[i * n + j] += a[i * n + k] * b[k * n + j];

    printf("%f\n", c[0]);
    free(a); free(b); free(c);
    return 0;
}
