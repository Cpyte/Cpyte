#include <stdio.h>
#include <stdlib.h>

static int cmp(const void *a, const void *b) {
    return *(const int *)a - *(const int *)b;
}

int main(void) {
    int n = 100000;
    int *arr = malloc(n * sizeof(int));
    for (int i = 0; i < n; i++)
        arr[i] = rand();
    qsort(arr, n, sizeof(int), cmp);
    printf("%d %d\n", arr[0], arr[n - 1]);
    free(arr);
    return 0;
}
