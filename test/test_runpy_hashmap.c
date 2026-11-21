#include <assert.h>
#include <stdint.h>

#include "../source/cpyte/runpy_runtime.c"

int main(void) {
    Table* table = runpy_create_table(0);
    Table* table_identity = table;
    size_t initial_capacity = table->capacity;
    assert(runpy_hashmap_is_valid(table));
    assert(_hm_table_valid(table));
    assert(table->capacity == 8);
    assert(runpy_hashmap_is_empty(table));
    assert(runpy_hashmap_get(table, NULL) == NULL);
    assert(!runpy_hashmap_contains(table, NULL));

    uintptr_t colliding_keys[4];
    size_t collisions_found = 0;
    size_t collision_bucket = 0;
    for (uintptr_t candidate = 1;
            candidate < 10000 && collisions_found < 4; ++candidate) {
        size_t bucket = _hm_kvhash((void*)candidate) & 7;
        if (collisions_found == 0) {
            collision_bucket = bucket;
        }
        if (bucket == collision_bucket) {
            colliding_keys[collisions_found++] = candidate;
        }
    }
    assert(collisions_found == 4);
    for (size_t i = 0; i < 3; ++i) {
        table = runpy_hashmap_put(
            table, (void*)colliding_keys[i], (void*)(colliding_keys[i] + 100));
    }
    table = runpy_hashmap_erase(table, (void*)colliding_keys[0]);
    assert(runpy_hashmap_get(table, (void*)colliding_keys[2]) ==
        (void*)(colliding_keys[2] + 100));
    table = runpy_hashmap_put(
        table, (void*)colliding_keys[3], (void*)(colliding_keys[3] + 100));
    assert(runpy_hashmap_size(table) == 3);
    assert(runpy_hashmap_get(table, (void*)colliding_keys[2]) ==
        (void*)(colliding_keys[2] + 100));
    for (size_t i = 1; i < 4; ++i) {
        table = runpy_hashmap_erase(table, (void*)colliding_keys[i]);
    }
    assert(runpy_hashmap_is_empty(table));

    void* keys[64];
    void* values[64];
    for (uintptr_t i = 0; i < 64; ++i) {
        keys[i] = (void*)(i + 1);
        values[i] = (void*)(i + 101);
        table = runpy_hashmap_put(table, keys[i], values[i]);
        assert(table == table_identity);
    }
    assert(runpy_hashmap_size(table) == 64);
    assert(table == table_identity);
    assert(table->capacity > initial_capacity);
    for (size_t i = 0; i < 64; ++i) {
        assert(runpy_hashmap_contains(table, keys[i]));
        assert(runpy_hashmap_get(table, keys[i]) == values[i]);
    }

    table = runpy_hashmap_put(table, keys[12], NULL);
    assert(runpy_hashmap_contains(table, keys[12]));
    assert(runpy_hashmap_get(table, keys[12]) == NULL);
    table = runpy_hashmap_put(table, keys[12], values[12]);
    assert(runpy_hashmap_get(table, keys[12]) == values[12]);

    table = runpy_hashmap_erase(table, keys[0]);
    assert(!runpy_hashmap_contains(table, keys[0]));
    assert(runpy_hashmap_size(table) == 63);
    for (size_t i = 1; i < 64; ++i) {
        assert(runpy_hashmap_get(table, keys[i]) == values[i]);
    }
    table = runpy_hashmap_erase(table, keys[0]);
    assert(runpy_hashmap_size(table) == 63);

    for (size_t i = 1; i < 64; i += 2) {
        table = runpy_hashmap_erase(table, keys[i]);
    }
    for (uintptr_t i = 1000; i < 1100; ++i) {
        table = runpy_hashmap_put(table, (void*)i, (void*)(i + 500));
    }
    for (uintptr_t i = 1000; i < 1100; ++i) {
        assert(runpy_hashmap_contains(table, (void*)i));
        assert(runpy_hashmap_get(table, (void*)i) == (void*)(i + 500));
    }

    table = runpy_hashmap_put(table, NULL, NULL);
    assert(runpy_hashmap_contains(table, NULL));
    assert(runpy_hashmap_get(table, NULL) == NULL);
    table = runpy_hashmap_erase(table, NULL);
    assert(!runpy_hashmap_contains(table, NULL));

    table = runpy_hashmap_erase(table, keys[2]);
    assert(!runpy_hashmap_contains(table, keys[2]));
    table = runpy_hashmap_put(table, keys[2], values[2]);
    assert(runpy_hashmap_get(table, keys[2]) == values[2]);

    Table* invalid = runpy_create_table(SIZE_MAX);
    assert(invalid == NULL);
    assert(!runpy_hashmap_is_valid(invalid));
    assert(runpy_hashmap_size(invalid) == 0);
    assert(runpy_hashmap_get(invalid, keys[0]) == NULL);
    invalid = runpy_hashmap_put(invalid, keys[0], values[0]);
    assert(runpy_hashmap_size(invalid) == 0);
    runpy_hashmap_destroy(invalid);

    runpy_hashmap_destroy(table);

    Table* runtime_table = runpy_create_tables();
    assert(runpy_hashmap_is_valid(runtime_table));
    runpy_hashmap_destroy(runtime_table);

    Class* class_value = runpy_create_class(42);
    assert(class_value != NULL);
    assert(class_value->Index == 42);
    assert(runpy_hashmap_is_valid(class_value->Table));
    runpy_destroy_class(class_value);
    return 0;
}
