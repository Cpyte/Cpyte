#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unwind.h>
#include <pthread.h>
#include <math.h>


//Fixed for Non-POSIX os support (ISO C)
static char *cpyte_strdup(const char *s) {
    size_t len = strlen(s) + 1;
    char *copy = malloc(len);
    if (copy != NULL)
        memcpy(copy, s, len);
    return copy;
}

void *bigint_from_str(const char *str);
void *bigint_from_int(int64_t v);
void bigint_print(void *n);
char *bigint_to_str(void *n);

void
print_int(int n) {
    printf("%d\n", n);
}

void
print_int64(int64_t n) {
    printf("%lld\n", (long long)n);
}

void
print_uint64(uint64_t n) {
    printf("%llu\n", (unsigned long long)n);
}

void print_hex(int64_t n) {
    printf("0x%llx\n", (unsigned long long)n);
}

void
print_double(double d) {
    printf("%f\n", d);
}

void
print_str(const char *s) {
    printf("%s\n", s);
}

static char *
_cpy_dupstr(const char *buf) {
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
    return _cpy_dupstr(buf);
}

char *
str_of_uint64(uint64_t n) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%llu", (unsigned long long)n);
    return _cpy_dupstr(buf);
}

char *
str_of_ptr(int64_t n) {
    char buf[32];
    snprintf(buf, sizeof(buf), "0x%llx", (unsigned long long)n);
    return _cpy_dupstr(buf);
}

char *
str_of_double(double d) {
    char buf[64];
    snprintf(buf, sizeof(buf), "%.6f", d);
    return _cpy_dupstr(buf);
}

int
input_int(void) {
    char buf[128];
    if (!fgets(buf, sizeof(buf), stdin)) {
        return 0; // EOF
    }

    char *end;
    const long v = strtol(buf, &end, 10);

    if (end == buf) {
        // empty line or not a number
        fprintf(stderr, "Value Error: stdin is not a number. Perhaps you mean input_str?\n");
        return 0;
    }

    return (int)v;
}

void *
bigint_input(void) {
    char buf[1024];
    if (!fgets(buf, sizeof(buf), stdin)) {
        return bigint_from_int(0); // EOF
    }
    char *p = buf;
    while (*p == ' ' || *p == '\t') p++;
    size_t len = strlen(p);
    while (len > 0 && (p[len-1] == '\n' || p[len-1] == '\r' || p[len-1] == ' ' || p[len-1] == '\t')) {
        p[--len] = '\0';
    }
    return bigint_from_str(p);
}

char*
input_str(void) {
    size_t cap = 256;
    size_t len = 0;
    char *buf = malloc(cap);
    if (!buf) {
        fprintf(stderr, "input_str: allocation failed\n");
        exit(1);
    }
    int c;
    while ((c = getchar()) != EOF && c != '\n') {
        if (len + 1 >= cap) {
            cap *= 2;
            char *tmp = realloc(buf, cap);
            if (!tmp) {
                free(buf);
                fprintf(stderr, "input_str: allocation failed\n");
                exit(1);
            }
            buf = tmp;
        }
        buf[len++] = (char)c;
    }
    buf[len] = '\0';
    return buf;
}

// Bruh, dead code.

typedef struct {
    _Unwind_Exception base;
    const char *type_name;
    const char *message;
} cpy_exception;

static void
cpy_exception_cleanup(_Unwind_Reason_Code reason, _Unwind_Exception *e) {
    (void)reason;
    free(e);
}

#define DW_EH_PE_omit     0xff
#define DW_EH_PE_absptr   0x00
#define DW_EH_PE_uleb128  0x01
#define DW_EH_PE_udata2   0x02
#define DW_EH_PE_udata4   0x03
#define DW_EH_PE_udata8   0x04
#define DW_EH_PE_sleb128  0x09
#define DW_EH_PE_sdata2   0x0a
#define DW_EH_PE_sdata4   0x0b
#define DW_EH_PE_sdata8   0x0c
#define DW_EH_PE_pcrel    0x10
#define DW_EH_PE_indirect 0x80

static uintptr_t
cpy_read_uleb128(const uint8_t **p) {
    uintptr_t result = 0;
    int shift = 0;
    uint8_t byte;
    do {
        byte = *(*p)++;
        result |= (uintptr_t)(byte & 0x7f) << shift;
        shift += 7;
    } while (byte & 0x80);
    return result;
}

static uintptr_t
cpy_read_sleb128(const uint8_t **p) {
    uintptr_t result = 0;
    int shift = 0;
    uint8_t byte;
    do {
        byte = *(*p)++;
        result |= (uintptr_t)(byte & 0x7f) << shift;
        shift += 7;
    } while (byte & 0x80);
    if ((byte & 0x40) && shift < (int)sizeof(uintptr_t) * 8)
        result |= ~((uintptr_t)0) << shift;
    return result;
}

static uintptr_t
cpy_read_encoded_ptr(const uint8_t **p, uint8_t enc) {
    if (enc == DW_EH_PE_omit)
        return 0;
    uintptr_t result = 0;
    const uint8_t *pp = *p;
    switch (enc & 0x0f) {
    case DW_EH_PE_absptr:
        memcpy(&result, pp, sizeof(void *));
        pp += sizeof(void *);
        break;
    case DW_EH_PE_uleb128:
        result = cpy_read_uleb128(&pp);
        break;
    case DW_EH_PE_sleb128:
        result = cpy_read_sleb128(&pp);
        break;
    case DW_EH_PE_udata2: {
        uint16_t v;
        memcpy(&v, pp, 2);
        result = v;
        pp += 2;
        break;
    }
    case DW_EH_PE_udata4: {
        uint32_t v;
        memcpy(&v, pp, 4);
        result = v;
        pp += 4;
        break;
    }
    case DW_EH_PE_udata8: {
        uint64_t v;
        memcpy(&v, pp, 8);
        result = v;
        pp += 8;
        break;
    }
    case DW_EH_PE_sdata2: {
        int16_t v;
        memcpy(&v, pp, 2);
        result = (intptr_t)v;
        pp += 2;
        break;
    }
    case DW_EH_PE_sdata4: {
        int32_t v;
        memcpy(&v, pp, 4);
        result = (intptr_t)v;
        pp += 4;
        break;
    }
    case DW_EH_PE_sdata8: {
        int64_t v;
        memcpy(&v, pp, 8);
        result = (intptr_t)v;
        pp += 8;
        break;
    }
    default:
        return 0;
    }
    if (result && (enc & DW_EH_PE_pcrel))
        result += (uintptr_t)pp;
    if (result && (enc & DW_EH_PE_indirect))
        result = *(uintptr_t *)result;
    *p = pp;
    return result;
}

typedef struct {
    uintptr_t landing_pad;
    int found;
    int is_handler;
} cpy_call_site;

static void
cpy_find_call_site(_Unwind_Context *context, cpy_call_site *out) {
    out->landing_pad = 0;
    out->found = 0;
    out->is_handler = 0;
    const uint8_t *lsda = (const uint8_t *)_Unwind_GetLanguageSpecificData(context);
    if (lsda == 0)
        return;
    uintptr_t func_start = _Unwind_GetRegionStart(context);
    uintptr_t ip = _Unwind_GetIP(context);
    if (ip == 0)
        return;
    uintptr_t ip_offset = (ip - 1) - func_start;

    const uint8_t *p = lsda;
    uint8_t lpstart_enc = *p++;
    uintptr_t lp_start = lpstart_enc == DW_EH_PE_omit
                             ? func_start
                             : cpy_read_encoded_ptr(&p, lpstart_enc);
    uint8_t ttype_enc = *p++;
    if (ttype_enc != DW_EH_PE_omit)
        (void)cpy_read_uleb128(&p);
    uint8_t call_site_enc = *p++;
    uintptr_t call_site_len = cpy_read_uleb128(&p);
    const uint8_t *cs_end = p + call_site_len;
    while (p < cs_end) {
        uintptr_t start = cpy_read_encoded_ptr(&p, call_site_enc);
        uintptr_t len = cpy_read_encoded_ptr(&p, call_site_enc);
        uintptr_t landing = cpy_read_encoded_ptr(&p, call_site_enc);
        uintptr_t action = cpy_read_uleb128(&p);
        if (start <= ip_offset && ip_offset < start + len) {
            out->found = 1;
            if (landing != 0) {
                out->landing_pad = lp_start + landing;
                out->is_handler = action != 0;
            }
            return;
        }
    }
}

_Unwind_Reason_Code
cpy_personality(
    int version, _Unwind_Action actions, uint64_t exception_class,
    struct _Unwind_Exception *exception_object, struct _Unwind_Context *context) {
    (void)version;
    (void)exception_class;
    cpy_call_site m;
    cpy_find_call_site(context, &m);
    if (actions & _UA_SEARCH_PHASE)
        return (m.found && m.is_handler) ? _URC_HANDLER_FOUND : _URC_CONTINUE_UNWIND;
    if ((actions & _UA_CLEANUP_PHASE) && m.found && m.landing_pad) {
        _Unwind_SetGR(context, __builtin_eh_return_data_regno(0), (uintptr_t)exception_object);
        _Unwind_SetGR(context, __builtin_eh_return_data_regno(1), (uintptr_t)0);
        _Unwind_SetIP(context, m.landing_pad);
        return _URC_INSTALL_CONTEXT;
    }
    return _URC_CONTINUE_UNWIND;
}

void
cpy_raise_exception(const char *type_name, const char *message) {
    cpy_exception *exc = (cpy_exception *)calloc(1, sizeof(cpy_exception));
    exc->base.exception_class = 0x4347505945584300ULL;
    exc->base.exception_cleanup = cpy_exception_cleanup;
    exc->type_name = type_name;
    exc->message = message;
    _Unwind_Reason_Code rc = _Unwind_RaiseException(&exc->base);
    fprintf(stderr, "uncaught exception (%d): %s: %s\n", (int)rc, type_name, message);
    abort();
}

void
cpy_resume(void *exception_object) {
    _Unwind_Resume((_Unwind_Exception *)exception_object);
    abort();
}

// The big big support for dynamic vars!!!!!
// Happy? :):):)

// A variable becomes "dynamic" when an assignment changes its type
// (e.g. `int a = 5` followed by `a = "hello"`). Dynamic values are stored
// in a name-keyed table tagged with a runtime kind.

enum {
    DYN_NONE = 0,
    DYN_INT = 1,      // int32 (sign-extended to 64)
    DYN_INT64 = 2,
    DYN_UINT64 = 3,
    DYN_CHAR = 4,     // i8 (zero-extended)
    DYN_BOOL = 5,     // 0/1
    DYN_DOUBLE = 6,   // f64, stored as bit-cast u64
    DYN_STR = 7,      // char* / i8*
    DYN_BIG = 8,      // bigint*
    DYN_PTR = 9,      // any other pointer
    DYN_LIST = 10     // dynamic[] array (DynValue*)
};

typedef struct {
    const char *name;
    uint64_t data;
    int kind;
} Dynamic;

typedef struct {
    const char *name;
    Dynamic *data;
} DynSlot;

static DynSlot *dyn_table;
static size_t dyn_capacity;
static size_t dyn_count;

static uint32_t
dyn_hash(const char *s) {
    uint32_t h = 2166136261u;
    while (*s) {
        h ^= (uint8_t)*s++;
        h *= 16777619u;
    }
    return h;
}

static void
dyn_rehash(DynSlot *table, size_t capacity) {
    for (size_t i = 0; i < capacity; i++)
        table[i].name = NULL;
    for (size_t i = 0; i < dyn_capacity; i++) {
        DynSlot *slot = &dyn_table[i];
        if (slot->name == NULL)
            continue;
        size_t idx = dyn_hash(slot->name) & (capacity - 1);
        while (table[idx].name != NULL)
            idx = (idx + 1) & (capacity - 1);
        table[idx].name = slot->name;
        table[idx].data = slot->data;
    }
}

static int
dyn_grow(void) {
    size_t new_cap = dyn_capacity ? dyn_capacity * 2 : 64;
    DynSlot *new_table = calloc(new_cap, sizeof(DynSlot));
    if (new_table == NULL)
        return -1;
    dyn_rehash(new_table, new_cap);
    free(dyn_table);
    dyn_table = new_table;
    dyn_capacity = new_cap;
    return 0;
}

static Dynamic *
dyn_lookup(const char *name) {
    if (dyn_table == NULL)
        return NULL;
    size_t idx = dyn_hash(name) & (dyn_capacity - 1);
    for (;;) {
        DynSlot *slot = &dyn_table[idx];
        if (slot->name == NULL)
            return NULL;
        if (strcmp(slot->name, name) == 0)
            return slot->data;
        idx = (idx + 1) & (dyn_capacity - 1);
    }
}

void assign(const char *name, int kind, uint64_t data) {
    uint32_t h = dyn_hash(name);

    for (;;) {
        if (dyn_table == NULL || (dyn_count + 1) * 10 >= dyn_capacity * 7) {
            if (dyn_grow() != 0)
                return;
        }

        size_t idx = h & (dyn_capacity - 1);
        for (;;) {
            DynSlot *slot = &dyn_table[idx];
            if (slot->name == NULL) {
                char *dup = cpyte_strdup(name);
                if (dup == NULL)
                    return;
                Dynamic *data_slot = malloc(sizeof(Dynamic));
                if (data_slot == NULL) {
                    free(dup);
                    return;
                }
                data_slot->name = dup;
                data_slot->kind = kind;
                data_slot->data = data;
                slot->name = dup;
                slot->data = data_slot;
                dyn_count++;
                return;
            }
            if (strcmp(slot->name, name) == 0) {
                slot->data->kind = kind;
                slot->data->data = data;
                return;
            }
            idx = (idx + 1) & (dyn_capacity - 1);
        }
    }
}

static int
dyn_is_numeric(int kind) {
    return kind == DYN_INT || kind == DYN_INT64 || kind == DYN_UINT64 ||
           kind == DYN_CHAR || kind == DYN_BOOL || kind == DYN_DOUBLE;
}

uint64_t
dyn_as(const char *name, int want_kind) {
    Dynamic *d = dyn_lookup(name);
    if (d == NULL) {
        fprintf(stderr, "dynamic variable '%s' does not exist\n", name);
        abort();
    }

    if (d->kind == DYN_NONE) {
        fprintf(stderr, "dynamic variable '%s' is NOT initialized\n", name);
        abort();
    }
    int k = d->kind;
    if (k == want_kind)
        return d->data;
    if (dyn_is_numeric(k) && dyn_is_numeric(want_kind)) {
        if (k != DYN_DOUBLE && want_kind != DYN_DOUBLE)
            return d->data;
        if (k == DYN_DOUBLE) {
            double v;
            memcpy(&v, &d->data, sizeof(v));
            return (uint64_t)(int64_t)v;
        }
        double v = (k == DYN_UINT64) ? (double)(uint64_t)d->data
                                     : (double)(int64_t)d->data;
        uint64_t out;
        memcpy(&out, &v, sizeof(out));
        return out;
    }
    if ((k == DYN_STR || k == DYN_PTR) && (want_kind == DYN_STR || want_kind == DYN_PTR))
        return d->data;
    fprintf(stderr, "dynamic variable '%s' type mismatch (%d -> %d)\n", name, k, want_kind);
    abort();
}

int
dyn_truthy(const char *name) {
    Dynamic *d = dyn_lookup(name);
    if (d == NULL || d->kind == DYN_NONE)
        return 0;
    if (d->kind == DYN_DOUBLE) {
        double v;
        memcpy(&v, &d->data, sizeof(v));
        return v != 0.0;
    }
    return d->data != 0;
}

void
dyn_print(const char *name) {
    Dynamic *d = dyn_lookup(name);
    if (d == NULL || d->kind == DYN_NONE) {
        printf("0\n");
        return;
    }
    switch (d->kind) {
    case DYN_INT:
        printf("%d\n", (int32_t)d->data);
        break;
    case DYN_INT64:
        printf("%lld\n", (long long)(int64_t)d->data);
        break;
    case DYN_UINT64:
        printf("%llu\n", (unsigned long long)d->data);
        break;
    case DYN_CHAR:
        printf("%d\n", (int)(int8_t)(uint8_t)d->data);
        break;
    case DYN_BOOL:
        printf("%d\n", d->data ? 1 : 0);
        break;
    case DYN_DOUBLE: {
        double v;
        memcpy(&v, &d->data, sizeof(v));
        printf("%f\n", v);
        break;
    }
    case DYN_STR:
        printf("%s\n", (const char *)(uintptr_t)d->data);
        break;
    case DYN_BIG:
        bigint_print((void *)(uintptr_t)d->data);
        break;
    case DYN_PTR:
        printf("0x%llx\n", (unsigned long long)d->data);
        break;
    default:
        printf("0\n");
        break;
    }
}

char *
dyn_str(const char *name) {
    Dynamic *d = dyn_lookup(name);
    if (d == NULL || d->kind == DYN_NONE)
        return cpyte_strdup("0");
    switch (d->kind) {
    case DYN_INT:
        return str_of_int64((int32_t)d->data);
    case DYN_INT64:
        return str_of_int64((int64_t)d->data);
    case DYN_UINT64:
        return str_of_uint64(d->data);
    case DYN_CHAR:
        return str_of_int64((int32_t)(int8_t)(uint8_t)d->data);
    case DYN_BOOL:
        return cpyte_strdup(d->data ? "1" : "0");
    case DYN_DOUBLE: {
        double v;
        memcpy(&v, &d->data, sizeof(v));
        return str_of_double(v);
    }
    case DYN_STR:
        return cpyte_strdup((const char *)(uintptr_t)d->data);
    case DYN_BIG:
        return bigint_to_str((void *)(uintptr_t)d->data);
    case DYN_PTR:
        return str_of_ptr((int64_t)d->data);
    default:
        return cpyte_strdup("0");
    }
}

// Array length registry
//
// `new T[n]` returns a bare T* with no length attached, so there is no way to
// iterate it (`for x in arr`) or to know its element count at runtime. To keep
// the array ABI (plain pointer: indexing, C interop, sizeof, free) untouched,
// the length is kept in this side table keyed by the array pointer. The GC
// release callback unregisters entries when an object is collected, and the
// `free()` builtin unregisters under #nogc, so entries never go stale.

#define ARR_TABLE_BITS 12
#define ARR_TABLE_SIZE (1 << ARR_TABLE_BITS)

typedef struct arr_entry_s {
    void               *arr;
    size_t              len;
    struct arr_entry_s *next;
} arr_entry_t;

static arr_entry_t        *arr_table[ARR_TABLE_SIZE];
static pthread_mutex_t     arr_lock = PTHREAD_MUTEX_INITIALIZER;

static unsigned long
hash_arr_ptr(const void *p) {
    unsigned long v = (unsigned long)p;
    v = (v >> 4) * 2654435761UL;   /* Knuth multiplicative hash */
    return v & (ARR_TABLE_SIZE - 1);
}

void
cpyte_array_register(void *arr, int64_t n) {
    if (arr == NULL) return;
    unsigned long h = hash_arr_ptr(arr);
    pthread_mutex_lock(&arr_lock);
    arr_entry_t *e = arr_table[h];
    while (e) {
        if (e->arr == arr) {
            e->len = (size_t)n;
            pthread_mutex_unlock(&arr_lock);
            return;
        }
        e = e->next;
    }
    e = (arr_entry_t *)malloc(sizeof(arr_entry_t));
    if (e != NULL) {
        e->arr  = arr;
        e->len  = (size_t)n;
        e->next = arr_table[h];
        arr_table[h] = e;
    }
    pthread_mutex_unlock(&arr_lock);
}

int64_t
cpyte_array_len(void *arr) {
    if (arr == NULL) return 0;
    unsigned long h = hash_arr_ptr(arr);
    pthread_mutex_lock(&arr_lock);
    arr_entry_t *e = arr_table[h];
    while (e) {
        if (e->arr == arr) {
            int64_t n = (int64_t)e->len;
            pthread_mutex_unlock(&arr_lock);
            return n;
        }
        e = e->next;
    }
    pthread_mutex_unlock(&arr_lock);
    return 0;
}

void
cpyte_array_unregister(void *arr) {
    if (arr == NULL) return;
    unsigned long h = hash_arr_ptr(arr);
    pthread_mutex_lock(&arr_lock);
    arr_entry_t **pp = &arr_table[h];
    while (*pp != NULL) {
        if ((*pp)->arr == arr) {
            arr_entry_t *tmp = *pp;
            *pp = tmp->next;
            free(tmp);
            break;
        }
        pp = &(*pp)->next;
    }
    pthread_mutex_unlock(&arr_lock);
}

// ---------------------------------------------------------------------------
// Slot-indexed dynamic values + polymorphic runtime dispatch.
//
// A dynamic value is a tagged (kind, data) pair. The compiler assigns each
// dynamic local a compile-time slot id into this fixed arena, so reads/writes
// are two loads/stores (no hash, no malloc) and values can flow through
// registers. All dispatch operations take (kind, data) VALUES so results are
// usable directly in arithmetic, comparisons, printing and function calls.
// ---------------------------------------------------------------------------

#define DYN_ARENA_SLOTS 2048

typedef struct {
    int      kind;
    uint64_t data;
} DynValue;

static DynValue dyn_arena[DYN_ARENA_SLOTS];

DynValue *
dyn_slot_ptr(int slot) {
    return &dyn_arena[slot];
}

void
dyn_set(int slot, int kind, uint64_t data) {
    dyn_arena[slot].kind = kind;
    dyn_arena[slot].data = data;
}

int
dyn_kind(int slot) {
    return dyn_arena[slot].kind;
}

uint64_t
dyn_get(int slot, int want_kind) {
    int k = dyn_arena[slot].kind;
    uint64_t data = dyn_arena[slot].data;
    if (k == want_kind)
        return data;
    if (dyn_is_numeric(k) && dyn_is_numeric(want_kind)) {
        if (k != DYN_DOUBLE && want_kind != DYN_DOUBLE)
            return data;
        if (k == DYN_DOUBLE) {
            double v;
            memcpy(&v, &data, sizeof(v));
            return (uint64_t)(int64_t)v;
        }
        double v = (k == DYN_UINT64) ? (double)(uint64_t)data
                                     : (double)(int64_t)data;
        uint64_t out;
        memcpy(&out, &v, sizeof(out));
        return out;
    }
    if ((k == DYN_STR || k == DYN_PTR) && (want_kind == DYN_STR || want_kind == DYN_PTR))
        return data;
    return data;
}

uint64_t
dyn_as_v(int k, uint64_t b, int want_kind) {
    if (k == want_kind)
        return b;
    if (dyn_is_numeric(k) && dyn_is_numeric(want_kind)) {
        if (k != DYN_DOUBLE && want_kind != DYN_DOUBLE)
            return b;
        if (k == DYN_DOUBLE) {
            double v;
            memcpy(&v, &b, sizeof(v));
            return (uint64_t)(int64_t)v;
        }
        double v = (k == DYN_UINT64) ? (double)(uint64_t)b : (double)(int64_t)b;
        uint64_t out;
        memcpy(&out, &v, sizeof(out));
        return out;
    }
    if ((k == DYN_STR || k == DYN_PTR) && (want_kind == DYN_STR || want_kind == DYN_PTR))
        return b;
    return b;
}

int
dyn_truthy_v(int k, uint64_t b) {
    if (k == DYN_NONE)
        return 0;
    if (k == DYN_DOUBLE) {
        double v;
        memcpy(&v, &b, sizeof(v));
        return v != 0.0;
    }
    return b != 0;
}

static void
dyn_elem_to_file(FILE *f, int k, uint64_t b);

static void
dyn_list_to_file(FILE *f, uint64_t b) {
    DynValue *arr = (DynValue *)(uintptr_t)b;
    int64_t n = cpyte_array_len(arr);
    fputc('[', f);
    for (int64_t i = 0; i < n; i++) {
        if (i > 0)
            fputs(", ", f);
        dyn_elem_to_file(f, arr[i].kind, arr[i].data);
    }
    fputc(']', f);
}

static void
dyn_elem_to_file(FILE *f, int k, uint64_t b) {
    switch (k) {
    case DYN_INT:
        fprintf(f, "%d", (int32_t)b);
        break;
    case DYN_INT64:
        fprintf(f, "%lld", (long long)(int64_t)b);
        break;
    case DYN_UINT64:
        fprintf(f, "%llu", (unsigned long long)b);
        break;
    case DYN_CHAR:
        fprintf(f, "%d", (int)(int8_t)(uint8_t)b);
        break;
    case DYN_BOOL:
        fprintf(f, "%d", b ? 1 : 0);
        break;
    case DYN_DOUBLE: {
        double v;
        memcpy(&v, &b, sizeof(v));
        fprintf(f, "%f", v);
        break;
    }
    case DYN_STR:
        fprintf(f, "%s", (const char *)(uintptr_t)b);
        break;
    case DYN_BIG: {
        char *s = bigint_to_str((void *)(uintptr_t)b);
        fprintf(f, "%s", s ? s : "");
        break;
    }
    case DYN_PTR:
        fprintf(f, "0x%llx", (unsigned long long)b);
        break;
    case DYN_LIST:
        dyn_list_to_file(f, b);
        break;
    default:
        fprintf(f, "0");
        break;
    }
}

void
dyn_print_v(int k, uint64_t b) {
    if (k == DYN_NONE) {
        printf("0\n");
        return;
    }
    dyn_elem_to_file(stdout, k, b);
    printf("\n");
}

char *
dyn_str_v(int k, uint64_t b) {
    char *buf = NULL;
    size_t sz = 0;
    FILE *f = open_memstream(&buf, &sz);
    if (f == NULL)
        return cpyte_strdup("0");
    if (k == DYN_NONE)
        fprintf(f, "0");
    else
        dyn_elem_to_file(f, k, b);
    fclose(f);
    return buf;
}

enum {
    DYNOP_ADD = 0, DYNOP_SUB = 1, DYNOP_MUL = 2, DYNOP_DIV = 3, DYNOP_MOD = 4,
    DYNOP_EQ = 5, DYNOP_NE = 6, DYNOP_LT = 7, DYNOP_LE = 8, DYNOP_GT = 9,
    DYNOP_GE = 10,
    DYNOP_NEG = 11, DYNOP_NOT = 12,
    DYNOP_BAND = 13, DYNOP_BOR = 14, DYNOP_BXOR = 15, DYNOP_SHL = 16,
    DYNOP_SHR = 17
};

static double
dyn_to_double(int k, uint64_t b) {
    switch (k) {
    case DYN_DOUBLE: {
        double v;
        memcpy(&v, &b, sizeof(v));
        return v;
    }
    case DYN_UINT64:
        return (double)(uint64_t)b;
    default:
        return (double)(int64_t)b;
    }
}

void
dyn_op(DynValue *out, int op, int k1, uint64_t b1, int k2, uint64_t b2) {
    if (op >= DYNOP_EQ && op <= DYNOP_GE) {
        int r;
        if (k1 == DYN_STR && k2 == DYN_STR) {
            int c = strcmp((const char *)(uintptr_t)b1, (const char *)(uintptr_t)b2);
            switch (op) {
            case DYNOP_EQ: r = c == 0; break;
            case DYNOP_NE: r = c != 0; break;
            case DYNOP_LT: r = c < 0; break;
            case DYNOP_LE: r = c <= 0; break;
            case DYNOP_GT: r = c > 0; break;
            default:       r = c >= 0; break;
            }
        } else if (k1 == k2 && (k1 == DYN_PTR || k1 == DYN_LIST)) {
            switch (op) {
            case DYNOP_EQ: r = b1 == b2; break;
            case DYNOP_NE: r = b1 != b2; break;
            default:
                fprintf(stderr, "dynamic ordering comparison not supported for kind %d\n", k1);
                abort();
            }
        } else if (dyn_is_numeric(k1) && dyn_is_numeric(k2)) {
            if (k1 == DYN_DOUBLE || k2 == DYN_DOUBLE) {
                double a = dyn_to_double(k1, b1), c = dyn_to_double(k2, b2);
                switch (op) {
                case DYNOP_EQ: r = a == c; break;
                case DYNOP_NE: r = a != c; break;
                case DYNOP_LT: r = a < c; break;
                case DYNOP_LE: r = a <= c; break;
                case DYNOP_GT: r = a > c; break;
                default:       r = a >= c; break;
                }
            } else if (k1 == DYN_UINT64 && k2 == DYN_UINT64) {
                uint64_t a = b1, c = b2;
                switch (op) {
                case DYNOP_EQ: r = a == c; break;
                case DYNOP_NE: r = a != c; break;
                case DYNOP_LT: r = a < c; break;
                case DYNOP_LE: r = a <= c; break;
                case DYNOP_GT: r = a > c; break;
                default:       r = a >= c; break;
                }
            } else {
                int64_t a = (int64_t)b1, c = (int64_t)b2;
                switch (op) {
                case DYNOP_EQ: r = a == c; break;
                case DYNOP_NE: r = a != c; break;
                case DYNOP_LT: r = a < c; break;
                case DYNOP_LE: r = a <= c; break;
                case DYNOP_GT: r = a > c; break;
                default:       r = a >= c; break;
                }
            }
        } else if (k1 == DYN_NONE && k2 == DYN_NONE) {
            r = (op == DYNOP_EQ) ? 1 : (op == DYNOP_NE) ? 0 : 0;
        } else {
            /* Python: unrelated kinds are unequal; ordering is an error. */
            if (op == DYNOP_EQ) r = 0;
            else if (op == DYNOP_NE) r = 1;
            else {
                fprintf(stderr,
                        "dynamic ordering comparison between incompatible kinds (%d and %d)\n",
                        k1, k2);
                abort();
            }
        }
        out->kind = DYN_BOOL;
        out->data = (uint64_t)r;
        return;
    }

    if (dyn_is_numeric(k1) && dyn_is_numeric(k2)) {
        if (k1 == DYN_DOUBLE || k2 == DYN_DOUBLE) {
            double a = dyn_to_double(k1, b1), c = dyn_to_double(k2, b2), r;
            switch (op) {
            case DYNOP_ADD: r = a + c; break;
            case DYNOP_SUB: r = a - c; break;
            case DYNOP_MUL: r = a * c; break;
            case DYNOP_DIV: r = a / c; break;
            case DYNOP_MOD: r = fmod(a, c); break;
            default:        r = 0.0; break;
            }
            out->kind = DYN_DOUBLE;
            memcpy(&out->data, &r, sizeof(r));
            return;
        }
        if (k1 == DYN_UINT64 && k2 == DYN_UINT64) {
            uint64_t a = b1, c = b2, r = 0;
            switch (op) {
            case DYNOP_ADD: r = a + c; break;
            case DYNOP_SUB: r = a - c; break;
            case DYNOP_MUL: r = a * c; break;
            case DYNOP_DIV: r = a / c; break;
            case DYNOP_MOD: r = a % c; break;
            case DYNOP_BAND: r = a & c; break;
            case DYNOP_BOR:  r = a | c; break;
            case DYNOP_BXOR: r = a ^ c; break;
            case DYNOP_SHL:  r = a << c; break;
            case DYNOP_SHR:  r = a >> c; break;
            default:         r = 0; break;
            }
            out->kind = DYN_UINT64;
            out->data = r;
            return;
        }
        int64_t a = (int64_t)b1, c = (int64_t)b2, r = 0;
        switch (op) {
        case DYNOP_ADD: r = a + c; break;
        case DYNOP_SUB: r = a - c; break;
        case DYNOP_MUL: r = a * c; break;
        case DYNOP_DIV: r = a / c; break;
        case DYNOP_MOD: r = a % c; break;
        case DYNOP_BAND: r = a & c; break;
        case DYNOP_BOR:  r = a | c; break;
        case DYNOP_BXOR: r = a ^ c; break;
        case DYNOP_SHL:  r = a << c; break;
        case DYNOP_SHR:  r = a >> c; break;
        default:         r = 0; break;
        }
        out->kind = DYN_INT64;
        out->data = (uint64_t)r;
        return;
    }

    if (op == DYNOP_ADD && k1 == DYN_STR && k2 == DYN_STR) {
        const char *a = (const char *)(uintptr_t)b1, *c = (const char *)(uintptr_t)b2;
        size_t al = strlen(a), cl = strlen(c);
        char *buf = (char *)malloc(al + cl + 1);
        if (buf == NULL) {
            out->kind = DYN_STR;
            out->data = 0;
            return;
        }
        memcpy(buf, a, al);
        memcpy(buf + al, c, cl);
        buf[al + cl] = 0;
        out->kind = DYN_STR;
        out->data = (uint64_t)(uintptr_t)buf;
        return;
    }

    fprintf(stderr, "dynamic operator %d not supported for kinds %d and %d\n",
            op, k1, k2);
    abort();
}

void
dyn_op1(DynValue *out, int op, int k1, uint64_t b1) {
    if (op == DYNOP_NOT) {
        out->kind = DYN_BOOL;
        out->data = (uint64_t)(dyn_truthy_v(k1, b1) == 0);
        return;
    }
    if (op == DYNOP_NEG) {
        if (k1 == DYN_DOUBLE) {
            double v, r;
            memcpy(&v, &b1, sizeof(v));
            r = -v;
            out->kind = DYN_DOUBLE;
            memcpy(&out->data, &r, sizeof(r));
            return;
        }
        if (dyn_is_numeric(k1)) {
            out->kind = DYN_INT64;
            out->data = (uint64_t)(-(int64_t)b1);
            return;
        }
    }
    fprintf(stderr, "dynamic unary operator %d not supported for kind %d\n", op, k1);
    abort();
}

DynValue *
dyn_list_get(uint64_t list_bits, int64_t idx) {
    DynValue *arr = (DynValue *)(uintptr_t)list_bits;
    if (arr == NULL) {
        fprintf(stderr, "indexing NULL dynamic list\n");
        abort();
    }
    int64_t n = cpyte_array_len(arr);
    if (idx < 0 || idx >= n) {
        fprintf(stderr, "dynamic list index %lld out of range (length %lld)\n",
                (long long)idx, (long long)n);
        abort();
    }
    return &arr[idx];
}

/* str_split(str, sep) — split a string by a separator into a DynValue list.
 * Returns a malloc'd DynValue[] array of DYN_STR elements, registered in the
 * array-length table so `for x in list` works.  The empty string yields a
 * single-element list containing "". */
DynValue *
str_split(const char *str, const char *sep) {
    if (str == NULL) {
        fprintf(stderr, "split: NULL string argument\n");
        abort();
    }
    if (sep == NULL || sep[0] == '\0') {
        fprintf(stderr, "split: NULL or empty separator\n");
        abort();
    }

    size_t sep_len = strlen(sep);

    /* First pass: count parts. */
    size_t count = 1;
    const char *p = str;
    while (1) {
        const char *hit = strstr(p, sep);
        if (hit == NULL) break;
        count++;
        p = hit + sep_len;
    }

    /* Allocate DynValue array. */
    DynValue *arr = (DynValue *)malloc(sizeof(DynValue) * count);
    if (arr == NULL) {
        fprintf(stderr, "split: allocation failed\n");
        abort();
    }

    /* Second pass: fill elements. */
    size_t idx = 0;
    const char *start = str;
    while (1) {
        const char *hit = strstr(start, sep);
        size_t part_len;
        if (hit == NULL) {
            part_len = strlen(start);
        } else {
            part_len = (size_t)(hit - start);
        }
        char *part = (char *)malloc(part_len + 1);
        if (part == NULL) {
            fprintf(stderr, "split: allocation failed\n");
            abort();
        }
        memcpy(part, start, part_len);
        part[part_len] = '\0';
        arr[idx].kind = DYN_STR;
        arr[idx].data = (uint64_t)(uintptr_t)part;
        idx++;
        if (hit == NULL) break;
        start = hit + sep_len;
    }

    /* Register the array so for-in iteration works. */
    cpyte_array_register(arr, (int64_t)count);
    return arr;
}

/* Shell glob matching: `*` (any run), `?` (single char) and `[...]` /
 * `[!...]` char classes (with `a-z` ranges). Case-sensitive, no allocation. */
int
glob_match(const char *pat, const char *str) {
    const char *star = NULL;
    const char *ss = NULL;
    while (*str != '\0') {
        if (*pat == '*') {
            star = pat++;
            ss = str;
        } else if (*pat == '?') {
            pat++;
            str++;
        } else if (*pat == '[') {
            const char *close = strchr(pat, ']');
            if (close == NULL) {
                /* Unclosed class: treat `[` literally. */
                if (*pat == *str) {
                    pat++;
                    str++;
                } else if (star) {
                    pat = star + 1;
                    str = ++ss;
                } else {
                    return 0;
                }
            } else {
                int negate = 0;
                const char *p = pat + 1;
                int matched = 0;
                if (*p == '!') {
                    negate = 1;
                    p++;
                }
                while (p < close) {
                    if (p + 2 < close && p[1] == '-') {
                        if ((unsigned char)*str >= (unsigned char)p[0] &&
                            (unsigned char)*str <= (unsigned char)p[2]) {
                            matched = 1;
                            break;
                        }
                        p += 3;
                    } else {
                        if (*p == *str) {
                            matched = 1;
                            break;
                        }
                        p++;
                    }
                }
                if (matched != negate) {
                    pat = close + 1;
                    str++;
                } else if (star) {
                    pat = star + 1;
                    str = ++ss;
                } else {
                    return 0;
                }
            }
        } else if (*pat == *str) {
            pat++;
            str++;
        } else if (star) {
            pat = star + 1;
            str = ++ss;
        } else {
            return 0;
        }
    }
    while (*pat == '*') {
        pat++;
    }
    return *pat == '\0';
}
