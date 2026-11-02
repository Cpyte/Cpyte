#include <stdio.h>
#include <stdlib.h>

static void quicksort(int *arr, int left, int right) {
    int i = left;
    int j = right;
    int pivot = arr[(left + right) / 2];
    while (i <= j) {
        while (arr[i] < pivot)
            i++;
        while (arr[j] > pivot)
            j--;
        if (i <= j) {
            int tmp = arr[i];
            arr[i] = arr[j];
            arr[j] = tmp;
            i++;
            j--;
        }
    }
    if (left < j)
        quicksort(arr, left, j);
    if (i < right)
        quicksort(arr, i, right);
}

int main(void) {
    int n = 100000;
    int *arr = malloc(n * sizeof(int));
    for (int i = 0; i < n; i++)
        arr[i] = i * 12345 + 67890;
    quicksort(arr, 0, n - 1);
    printf("%d\n%d\n", arr[0], arr[n - 1]);
    free(arr);
    return 0;
}
