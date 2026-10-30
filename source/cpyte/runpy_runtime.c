#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef void (*RunpyImpl)(void** args);

typedef struct Dyn_Function {
    uint32_t signature;
    char* name;
    RunpyImpl impl;
    long long generation;
} Function;

typedef struct Table {
    void** keys;
    Function** values;
    uint8_t* used;
    char* slab;
    size_t capacity;
    size_t length;
    size_t used_count;
} Table;

typedef struct Dyn_Class {
    long long Index;
    Table* Table;
} Class;

enum {
    HM_EMPTY = 0,
    HM_DELETED = 1,
    HM_FIRST_TAG = 2,
    HM_TAG_MASK = 0x7f,
    HM_VECTOR_WIDTH = 16
};

#if defined(__GNUC__) || defined(__clang__)
typedef uint8_t hm_u8x16 __attribute__((vector_size(16)));
#define HM_HAS_NATIVE_VECTORS 1
#else
#define HM_HAS_NATIVE_VECTORS 0
#endif

static int _hm_table_valid(const Table* table) {
    return table != NULL && table->slab != NULL && table->keys != NULL &&
        table->values != NULL && table->used != NULL && table->capacity >= 8 &&
        (table->capacity & (table->capacity - 1)) == 0;
}

size_t _hm_next_pow2(size_t n) {
    size_t capacity = 8;
    while (capacity < n) {
        if (capacity > SIZE_MAX / 2) {
            return 0;
        }
        capacity *= 2;
    }
    return capacity;
}

static int _hm_init_table(Table* table, size_t capacity) {
    if (table == NULL || capacity < 8 ||
            (capacity & (capacity - 1)) != 0 ||
            capacity > SIZE_MAX / (2 * sizeof(void*) + sizeof(uint8_t))) {
        return 0;
    }

    size_t keys_size = capacity * sizeof(*table->keys);
    size_t values_size = capacity * sizeof(*table->values);
    size_t used_size = capacity * sizeof(*table->used);
    if (keys_size > SIZE_MAX - values_size ||
            keys_size + values_size > SIZE_MAX - used_size) {
        return 0;
    }

    size_t values_offset = keys_size;
    size_t used_offset = values_offset + values_size;
    size_t total_size = used_offset + used_size;
    char* slab = calloc(1, total_size);
    if (slab == NULL) {
        return 0;
    }

    table->slab = slab;
    table->keys = (void**)slab;
    table->values = (Function**)(slab + values_offset);
    table->used = (uint8_t*)(slab + used_offset);
    table->capacity = capacity;
    table->length = 0;
    table->used_count = 0;
    return 1;
}

Table* create_table(size_t requested_capacity) {
    size_t capacity = _hm_next_pow2(requested_capacity);
    if (capacity == 0) {
        return NULL;
    }
    Table* table = calloc(1, sizeof(*table));
    if (table == NULL) {
        return NULL;
    }
    if (!_hm_init_table(table, capacity)) {
        free(table);
        return NULL;
    }
    return table;
}

int hashmap_is_valid(const Table* table) {
    return _hm_table_valid(table);
}

int _hm_kveq(void* left, void* right) {
    return left == right;
}

size_t _hm_kvhash(void* key) {
    uint64_t value = (uint64_t)(uintptr_t)key;
    value ^= value >> 33;
    value *= UINT64_C(0xff51afd7ed558ccd);
    value ^= value >> 33;
    value *= UINT64_C(0xc4ceb9fe1a85ec53);
    value ^= value >> 33;
    return (size_t)value;
}

static uint8_t _hm_tag(void* key) {
    return (uint8_t)(HM_FIRST_TAG +
        (_hm_kvhash(key) & (size_t)HM_TAG_MASK));
}

static void _hm_load_control_vector(
        const Table* table, size_t start, size_t offset, size_t count,
        uint8_t controls[HM_VECTOR_WIDTH]) {
    memset(controls, HM_EMPTY, HM_VECTOR_WIDTH);
    size_t mask = table->capacity - 1;
    size_t index = (start + offset) & mask;
    size_t first_part = count;
    if (first_part > table->capacity - index) {
        first_part = table->capacity - index;
    }
    memcpy(controls, table->used + index, first_part);
    if (first_part < count) {
        memcpy(controls + first_part, table->used, count - first_part);
    }
}

size_t _hm_kvwalk(const Table* table, size_t start, void* key) {
    if (!_hm_table_valid(table)) {
        return table == NULL ? 0 : table->capacity;
    }

    size_t capacity = table->capacity;
    uint8_t tag = _hm_tag(key);
    uint8_t target_bytes[HM_VECTOR_WIDTH];
    memset(target_bytes, tag, sizeof(target_bytes));
#if HM_HAS_NATIVE_VECTORS
    hm_u8x16 target;
    memcpy(&target, target_bytes, sizeof(target));
#endif
    uint8_t controls[HM_VECTOR_WIDTH];
    for (size_t probed = 0; probed < capacity;) {
        size_t count = capacity - probed;
        if (count > HM_VECTOR_WIDTH) {
            count = HM_VECTOR_WIDTH;
        }
        _hm_load_control_vector(table, start, probed, count, controls);
#if HM_HAS_NATIVE_VECTORS
        hm_u8x16 lanes;
        memcpy(&lanes, controls, sizeof(lanes));
        hm_u8x16 matching = lanes == target;
        hm_u8x16 empty = lanes == (hm_u8x16){0};
        uint8_t matches[HM_VECTOR_WIDTH];
        uint8_t empties[HM_VECTOR_WIDTH];
        memcpy(matches, &matching, sizeof(matches));
        memcpy(empties, &empty, sizeof(empties));
#else
        uint8_t matches[HM_VECTOR_WIDTH];
        uint8_t empties[HM_VECTOR_WIDTH];
        for (size_t lane = 0; lane < count; ++lane) {
            matches[lane] = controls[lane] == tag ? UINT8_MAX : 0;
            empties[lane] = controls[lane] == HM_EMPTY ? UINT8_MAX : 0;
        }
#endif
        for (size_t lane = 0; lane < count; ++lane) {
            size_t index = (start + probed + lane) & (capacity - 1);
            if (empties[lane] != 0) {
                return capacity;
            }
            if (matches[lane] != 0 &&
                    _hm_kveq(table->keys[index], key)) {
                return index;
            }
        }
        probed += count;
    }
    return capacity;
}

size_t _hm_kvfree(const Table* table, size_t start) {
    if (!_hm_table_valid(table)) {
        return table == NULL ? 0 : table->capacity;
    }

    size_t capacity = table->capacity;
    uint8_t controls[HM_VECTOR_WIDTH];
    uint8_t empty_bytes[HM_VECTOR_WIDTH];
    uint8_t deleted_bytes[HM_VECTOR_WIDTH];
    memset(empty_bytes, HM_EMPTY, sizeof(empty_bytes));
    memset(deleted_bytes, HM_DELETED, sizeof(deleted_bytes));
#if HM_HAS_NATIVE_VECTORS
    hm_u8x16 empty_target;
    hm_u8x16 deleted_target;
    memcpy(&empty_target, empty_bytes, sizeof(empty_target));
    memcpy(&deleted_target, deleted_bytes, sizeof(deleted_target));
#endif
    for (size_t probed = 0; probed < capacity;) {
        size_t count = capacity - probed;
        if (count > HM_VECTOR_WIDTH) {
            count = HM_VECTOR_WIDTH;
        }
        _hm_load_control_vector(table, start, probed, count, controls);
#if HM_HAS_NATIVE_VECTORS
        hm_u8x16 lanes;
        memcpy(&lanes, controls, sizeof(lanes));
        hm_u8x16 available =
            (lanes == empty_target) | (lanes == deleted_target);
        uint8_t available_lanes[HM_VECTOR_WIDTH];
        memcpy(available_lanes, &available, sizeof(available_lanes));
#else
        uint8_t available_lanes[HM_VECTOR_WIDTH];
        for (size_t lane = 0; lane < count; ++lane) {
            available_lanes[lane] =
                controls[lane] == HM_EMPTY || controls[lane] == HM_DELETED
                ? UINT8_MAX : 0;
        }
#endif
        for (size_t lane = 0; lane < count; ++lane) {
            if (available_lanes[lane] != 0) {
                return (start + probed + lane) & (capacity - 1);
            }
        }
        probed += count;
    }
    return capacity;
}

static int _hm_rehash(Table* table, size_t requested_capacity,
        void* pending_key, void* pending_value, int insert_pending) {
    if (!_hm_table_valid(table)) {
        return 0;
    }
    size_t capacity = _hm_next_pow2(requested_capacity);
    if (capacity == 0 || capacity <= table->length ||
            (insert_pending && table->length >= capacity)) {
        return 0;
    }

    Table* replacement = calloc(1, sizeof(*replacement));
    if (replacement == NULL || !_hm_init_table(replacement, capacity)) {
        free(replacement);
        return 0;
    }

    for (size_t i = 0; i < table->capacity; ++i) {
        if (table->used[i] < HM_FIRST_TAG) {
            continue;
        }
        size_t start = _hm_kvhash(table->keys[i]) & (capacity - 1);
        size_t slot = _hm_kvfree(replacement, start);
        if (slot == capacity) {
            free(replacement->slab);
            free(replacement);
            return 0;
        }
        replacement->keys[slot] = table->keys[i];
        replacement->values[slot] = table->values[i];
        replacement->used[slot] = _hm_tag(table->keys[i]);
        ++replacement->length;
        ++replacement->used_count;
    }

    if (insert_pending) {
        size_t start = _hm_kvhash(pending_key) & (capacity - 1);
        size_t slot = _hm_kvfree(replacement, start);
        if (slot == capacity) {
            free(replacement->slab);
            free(replacement);
            return 0;
        }
        replacement->keys[slot] = pending_key;
        replacement->values[slot] = pending_value;
        replacement->used[slot] = _hm_tag(pending_key);
        ++replacement->length;
        ++replacement->used_count;
    }

    char* old_slab = table->slab;
    table->keys = replacement->keys;
    table->values = replacement->values;
    table->used = replacement->used;
    table->slab = replacement->slab;
    table->capacity = replacement->capacity;
    table->length = replacement->length;
    table->used_count = replacement->used_count;
    free(old_slab);
    free(replacement);
    return 1;
}

Table* hashmap_put(Table* table, void* key, void* value) {
    if (!_hm_table_valid(table)) {
        return table;
    }

    size_t start = _hm_kvhash(key) & (table->capacity - 1);
    size_t hit = _hm_kvwalk(table, start, key);
    if (hit != table->capacity) {
        table->values[hit] = value;
        return table;
    }

    size_t max_load = table->capacity - table->capacity / 4;
    int grow = table->length >= max_load;
    int compact = table->used_count >= table->capacity - table->capacity / 5;
    if (grow || compact) {
        size_t capacity = table->capacity;
        if (grow) {
            if (capacity > SIZE_MAX / 2) {
                return table;
            }
            capacity *= 2;
        }
        _hm_rehash(table, capacity, key, value, 1);
        return table;
    }

    size_t slot = _hm_kvfree(table, start);
    if (slot == table->capacity) {
        if (table->capacity <= SIZE_MAX / 2) {
            _hm_rehash(table, table->capacity * 2, key, value, 1);
        }
        return table;
    }
    int was_empty = table->used[slot] == HM_EMPTY;
    table->keys[slot] = key;
    table->values[slot] = value;
    table->used[slot] = _hm_tag(key);
    ++table->length;
    if (was_empty) {
        ++table->used_count;
    }
    return table;
}

void* hashmap_get(const Table* table, void* key) {
    if (!_hm_table_valid(table)) {
        return NULL;
    }
    size_t start = _hm_kvhash(key) & (table->capacity - 1);
    size_t hit = _hm_kvwalk(table, start, key);
    return hit != table->capacity ? table->values[hit] : NULL;
}

int hashmap_contains(const Table* table, void* key) {
    if (!_hm_table_valid(table)) {
        return 0;
    }
    size_t start = _hm_kvhash(key) & (table->capacity - 1);
    return _hm_kvwalk(table, start, key) != table->capacity;
}

Table* hashmap_erase(Table* table, void* key) {
    if (!_hm_table_valid(table)) {
        return table;
    }
    size_t start = _hm_kvhash(key) & (table->capacity - 1);
    size_t hit = _hm_kvwalk(table, start, key);
    if (hit != table->capacity) {
        table->keys[hit] = NULL;
        table->values[hit] = NULL;
        table->used[hit] = HM_DELETED;
        --table->length;
    }
    return table;
}

void hashmap_destroy(Table* table) {
    if (table == NULL) {
        return;
    }
    free(table->slab);
    free(table);
}

size_t hashmap_size(const Table* table) {
    return _hm_table_valid(table) ? table->length : 0;
}

int hashmap_is_empty(const Table* table) {
    return hashmap_size(table) == 0;
}

Function* runpy_create_function(uint32_t signature, void* impl, char* name) {
    Function* function = malloc(sizeof(*function));
    if (function == NULL) {
        return NULL;
    }
    function->generation = 0;
    function->signature = signature;
    function->impl = impl;
    function->name = name;
    return function;
}

Table* runpy_create_tables(void) {
    return create_table(8);
}

Class* runpy_create_class(long long index) {
    Class* class_value = calloc(1, sizeof(*class_value));
    if (class_value == NULL) {
        return NULL;
    }
    class_value->Index = index;
    class_value->Table = create_table(8);
    if (class_value->Table == NULL) {
        free(class_value);
        return NULL;
    }
    return class_value;
}

void runpy_destroy_class(Class* class_value) {
    if (class_value == NULL) {
        return;
    }
    hashmap_destroy(class_value->Table);
    free(class_value);
}

Class* runpy_class_addfunc(Function* func, Class* class) {
    if (func == NULL || class == NULL) {
            return NULL;
        }
    hashmap_put(class->Table, func->name, func);
    return class;
}

Class* runpy_class_delfunc(Function* func, Class* class) {
    if (func == NULL || class == NULL) {
            return NULL;
        }
    if (hashmap_contains(class->Table, func->name) != 0) return NULL;
    hashmap_erase(class->Table, func->name);
    return class;
}

Class* runpy_class_updfunc(Function* oldfunc, Function* newfunc, Class* class) {
    if (oldfunc == NULL || class == NULL || newfunc == NULL) {
            return NULL;
        }
    if (!hashmap_contains(class->Table, oldfunc->name)) return NULL;
    newfunc->generation = oldfunc->generation + 1;
    class = runpy_class_delfunc(oldfunc, class);
    return runpy_class_addfunc(newfunc, class);
}

void runpy_function_call(Function* func, void** args) {
    if (func == NULL || func->impl == NULL) {
        return;
    }

    func->impl(args);
}

Function* runpy_class_getfunc(Class* class_value, void* name) {
    if (class_value == NULL || class_value->Table == NULL || name == NULL) {
        return NULL;
    }
    return (Function*)hashmap_get(class_value->Table, name);
}

int runpy_class_hasfunc(Class* class_value, void* name) {
    if (class_value == NULL || class_value->Table == NULL || name == NULL) {
        return 0;
    }
    return hashmap_contains(class_value->Table, name);
}

Class* runpy_class_replace_table(Class* class_value, Table* new_table) {
    if (class_value == NULL) {
        return NULL;
    }
    if (class_value->Table != NULL) {
        hashmap_destroy(class_value->Table);
    }
    class_value->Table = new_table;
    return class_value;
}

Table* runpy_class_get_table(Class* class_value) {
    if (class_value == NULL) {
        return NULL;
    }
    return class_value->Table;
}

long long runpy_class_get_index(Class* class_value) {
    if (class_value == NULL) {
        return -1;
    }
    return class_value->Index;
}

void runpy_class_set_index(Class* class_value, long long index) {
    if (class_value == NULL) {
        return;
    }
    class_value->Index = index;
}

Function* runpy_function_get_impl(Function* func) {
    if (func == NULL) {
        return NULL;
    }
    return (Function*)func->impl;
}

void runpy_function_set_impl(Function* func, RunpyImpl impl) {
    if (func == NULL) {
        return;
    }
    func->impl = impl;
    func->generation++;
}

char* runpy_function_get_name(Function* func) {
    if (func == NULL) {
        return NULL;
    }
    return func->name;
}

uint32_t runpy_function_get_signature(Function* func) {
    if (func == NULL) {
        return 0;
    }
    return func->signature;
}

long long runpy_function_get_generation(Function* func) {
    if (func == NULL) {
        return -1;
    }
    return func->generation;
}

Table* runpy_table_clone(Table* table) {
    if (table == NULL || !_hm_table_valid(table)) {
        return NULL;
    }
    Table* clone = create_table(table->capacity);
    if (clone == NULL) {
        return NULL;
    }
    for (size_t i = 0; i < table->capacity; ++i) {
        if (table->used[i] >= HM_FIRST_TAG && table->keys[i] != NULL) {
            size_t start = _hm_kvhash(table->keys[i]) & (clone->capacity - 1);
            size_t slot = _hm_kvfree(clone, start);
            if (slot == clone->capacity) {
                if (!_hm_rehash(clone, clone->capacity * 2, table->keys[i], table->values[i], 1)) {
                    hashmap_destroy(clone);
                    return NULL;
                }
            } else {
                int was_empty = clone->used[slot] == HM_EMPTY;
                clone->keys[slot] = table->keys[i];
                clone->values[slot] = table->values[i];
                clone->used[slot] = _hm_tag(table->keys[i]);
                ++clone->length;
                if (was_empty) {
                    ++clone->used_count;
                }
            }
        }
    }
    return clone;
}

int runpy_table_merge(Table* target, Table* source) {
    if (target == NULL || source == NULL || !_hm_table_valid(target) || !_hm_table_valid(source)) {
        return 0;
    }
    for (size_t i = 0; i < source->capacity; ++i) {
        if (source->used[i] >= HM_FIRST_TAG && source->keys[i] != NULL && source->values[i] != NULL) {
            hashmap_put(target, source->keys[i], source->values[i]);
        }
    }
    return 1;
}

int runpy_class_hotswap(Class* class_value, Table* new_table) {
    if (class_value == NULL) {
        return 0;
    }
    return runpy_class_replace_table(class_value, new_table) != NULL ? 1 : 0;
}

int runpy_class_redirect(Class* class_value, Class* other) {
    if (class_value == NULL || other == NULL) {
        return 0;
    }
    if (class_value->Table != NULL) {
        hashmap_destroy(class_value->Table);
    }
    class_value->Table = runpy_table_clone(other->Table);
    if (class_value->Table == NULL) {
        return 0;
    }
    return 1;
}

Function* runpy_class_lookup_or_inherit(Class* class_value, void* name, Class* base) {
    if (class_value == NULL || name == NULL) {
        return NULL;
    }
    Function* f = runpy_class_getfunc(class_value, name);
    if (f != NULL) {
        return f;
    }
    if (base != NULL) {
        return runpy_class_lookup_or_inherit(base, name, NULL);
    }
    return NULL;
}
