
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <stdbool.h>
#include <string.h>

typedef struct {
    uint8_t *memory;
    size_t capacity;
    size_t offset;
} StackAllocator;

typedef struct {
    size_t previous_offset;
    size_t allocation_offset;
} StackHeader;

bool stack_init(StackAllocator *a, size_t capacity) {
    a->memory = malloc(capacity);
    if (!a->memory) return false;

    a->capacity = capacity;
    a->offset = 0;
    return true;
}

void *stack_alloc(StackAllocator *a, size_t size) {
    const size_t alignment = _Alignof(max_align_t);

    size_t header_pos = a->offset;
    uintptr_t base = (uintptr_t)a->memory;

    uintptr_t raw = base + header_pos + sizeof(StackHeader);
    size_t padding = (size_t)(-(uintptr_t)raw) & (alignment - 1);

    size_t data_pos = header_pos + sizeof(StackHeader) + padding;

    if (data_pos > a->capacity ||
        size > a->capacity - data_pos)
        return NULL;

    StackHeader *header =
        (StackHeader *)(a->memory + data_pos - sizeof(StackHeader));

    header->previous_offset = a->offset;
    header->allocation_offset = data_pos;

    a->offset = data_pos + size;

    return a->memory + data_pos;
}

bool stack_free(StackAllocator *a, void *ptr) {
    if (!ptr || !a->memory) return false;

    uintptr_t base = (uintptr_t)a->memory;
    uintptr_t address = (uintptr_t)ptr;

    if (address < base + sizeof(StackHeader) ||
        address >= base + a->offset)
        return false;

    StackHeader *header =
        (StackHeader *)(address - sizeof(StackHeader));

    if (header->allocation_offset != address - base)
        return false;

    a->offset = header->previous_offset;
    return true;
}

void stack_reset(StackAllocator *a) {
    a->offset = 0;
}

void stack_destroy(StackAllocator *a) {
    free(a->memory);
    a->memory = NULL;
    a->capacity = 0;
    a->offset = 0;
}

#define PAGE_SIZE 4096

typedef struct ArenaPage {
    struct ArenaPage *next;
    size_t used;
    unsigned char data[];
} ArenaPage;

void* arena_alloc(size_t size) {
    size_t needp = size / PAGE_SIZE + (size % PAGE_SIZE != 0);
}
