#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unwind.h>


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

typedef struct Type {
    const char *name;
} Type;

typedef struct Dynamic {
    Type *type;
    void *data;
    const char *name;
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

void assign(void *value, Type *type, const char *name) {
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
                Dynamic *data = malloc(sizeof(Dynamic));
                if (data == NULL) {
                    free(dup);
                    return;
                }
                data->type = type;
                data->data = value;
                data->name = dup;
                slot->name = dup;
                slot->data = data;
                dyn_count++;
                return;
            }
            if (strcmp(slot->name, name) == 0) {
                slot->data->type = type;
                slot->data->data = value;
                return;
            }
            idx = (idx + 1) & (dyn_capacity - 1);
        }
    }
}