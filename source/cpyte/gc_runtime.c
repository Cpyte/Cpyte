/*
 * gc_runtime.c - Cpyte concurrent tri-color mark GC
 *
 * Wraps ugc (https://github.com/bullno1/ugc) with:
 *   - Background pthread marker
 *   - Conservative stack scanning
 *   - Object tracking for pointer validation
 *   - Write barrier for tri-color invariant
 *
 * License: BSD-2-Clause (ugc) + project license
 */

#define UGC_IMPLEMENTATION
#define UGC_USE_TAGGED_POINTER 0
#include "ugc.h"

#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <time.h>

/* ── Extended header ────────────────────────────────────────────── */

typedef struct {
    ugc_header_t base;
    size_t       size;   /* payload size for conservative scanning */
} cpyte_obj_t;

#define HDR(cpyte_ptr) ((cpyte_obj_t*)(cpyte_ptr))
#define PAYLOAD(hdr)   ((char*)(hdr) + sizeof(cpyte_obj_t))
#define TO_USER(hdr)   ((void*)PAYLOAD(hdr))
#define TO_HDR(user)   ((cpyte_obj_t*)((char*)(user) - sizeof(cpyte_obj_t)))

/* ── Global GC state ────────────────────────────────────────────── */

static ugc_t         gc;
static pthread_mutex_t gc_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_t     gc_thread;
static volatile int  gc_running   = 0;
static volatile int  gc_paused    = 0;  /* safepoint: mutator paused */

/* allocation pressure for triggering collection */
static volatile size_t gc_alloc_bytes = 0;
static size_t          gc_threshold   = 1024 * 1024;  /* 1 MB default */

/* ── Object hash table (conservative scan validation) ───────────── */

#define OBJ_TABLE_BITS 12
#define OBJ_TABLE_SIZE (1 << OBJ_TABLE_BITS)

typedef struct obj_entry_s {
    void*               addr;   /* cpyte_obj_t* */
    struct obj_entry_s* next;
} obj_entry_t;

static obj_entry_t* obj_table[OBJ_TABLE_SIZE];

static unsigned long hash_ptr(const void* p) {
    unsigned long v = (unsigned long)p;
    v = (v >> 4) * 2654435761UL;   /* Knuth multiplicative hash */
    return v & (OBJ_TABLE_SIZE - 1);
}

static void obj_track(void* hdr) {
    unsigned long h = hash_ptr(hdr);
    obj_entry_t* e = (obj_entry_t*)malloc(sizeof(obj_entry_t));
    e->addr = hdr;
    e->next = obj_table[h];
    obj_table[h] = e;
}

static void obj_untrack(void* hdr) {
    unsigned long h = hash_ptr(hdr);
    obj_entry_t** pp = &obj_table[h];
    while (*pp) {
        if ((*pp)->addr == hdr) {
            obj_entry_t* tmp = *pp;
            *pp = tmp->next;
            free(tmp);
            return;
        }
        pp = &(*pp)->next;
    }
}

static int obj_is_tracked(const void* hdr) {
    unsigned long h = hash_ptr(hdr);
    obj_entry_t* e = obj_table[h];
    while (e) {
        if (e->addr == hdr) return 1;
        e = e->next;
    }
    return 0;
}

/* ── Conservative stack scanner ─────────────────────────────────── */

static void scan_stack_range(void* lo, void* hi) {
    /* scan word-aligned addresses from lo to hi */
    for (void** p = (void**)lo; p < (void**)hi; p++) {
        void* candidate = *p;
        if (!candidate) continue;

        /* quick alignment check */
        if ((unsigned long)candidate & (sizeof(void*) - 1)) continue;

        /* check if candidate points to a tracked GC object */
        cpyte_obj_t* hdr = TO_HDR(candidate);
        if (obj_is_tracked(hdr)) {
            ugc_visit(&gc, &hdr->base);
        }
    }
}

static void scan_stack(void) {
#if defined(__APPLE__)
    /* macOS: use pthread APIs */
    pthread_t self = pthread_self();
    void* stack_addr  = pthread_get_stackaddr_np(self);
    size_t stack_size = pthread_get_stacksize_np(self);
    /* stack grows downward: [stack_addr - stack_size, stack_addr) */
    void* lo = (char*)stack_addr - stack_size;
    void* hi = stack_addr;
    scan_stack_range(lo, hi);
#elif defined(__linux__)
    /* Linux: read /proc/self/maps or use pthread_getattr_np */
    FILE* f = fopen("/proc/self/maps", "r");
    if (f) {
        char line[256];
        while (fgets(line, sizeof(line), f)) {
            if (strstr(line, "[stack]")) {
                unsigned long start, end;
                if (sscanf(line, "%lx-%lx", &start, &end) == 2) {
                    scan_stack_range((void*)start, (void*)end);
                }
                break;
            }
        }
        fclose(f);
    }
#else
    /* Fallback: scan a conservative range around the frame pointer.
     * Not perfect but catches most stack objects. */
    volatile void* frame = __builtin_frame_address(0);
    scan_stack_range((char*)frame - 64 * 1024, (char*)frame + 1024);
#endif
}

/* ── ugc callbacks ──────────────────────────────────────────────── */

static void gc_scan_cb(ugc_t* g, ugc_header_t* hdr) {
    if (hdr == NULL) {
        /* Root scan phase: scan the thread stack conservatively */
        scan_stack();
        return;
    }

    /* Object scan: conservative scan of the payload */
    cpyte_obj_t* obj = (cpyte_obj_t*)hdr;
    char*   payload = PAYLOAD(obj);
    size_t  size    = obj->size;

    for (size_t i = 0; i + sizeof(void*) <= size; i += sizeof(void*)) {
        void* candidate = *(void**)(payload + i);
        if (!candidate) continue;

        /* alignment check */
        if ((unsigned long)candidate & (sizeof(void*) - 1)) continue;

        cpyte_obj_t* child = TO_HDR(candidate);
        if (obj_is_tracked(child)) {
            ugc_visit(g, &child->base);
        }
    }
}

static void gc_release_cb(ugc_t* g, ugc_header_t* hdr) {
    (void)g;
    cpyte_obj_t* obj = (cpyte_obj_t*)hdr;
    obj_untrack(obj);
    free(obj);
}

/* ── Public API (called from generated code) ────────────────────── */

void gc_init(void) {
    memset(obj_table, 0, sizeof(obj_table));
    ugc_init(&gc, gc_scan_cb, gc_release_cb);
    gc_alloc_bytes = 0;
    gc_running = 1;
}

void* gc_malloc(size_t size) {
    pthread_mutex_lock(&gc_lock);

    cpyte_obj_t* obj = (cpyte_obj_t*)calloc(1, sizeof(cpyte_obj_t) + size);
    if (!obj) {
        /* Emergency: force a full collect and retry */
        ugc_collect(&gc);
        obj = (cpyte_obj_t*)calloc(1, sizeof(cpyte_obj_t) + size);
        if (!obj) {
            pthread_mutex_unlock(&gc_lock);
            fprintf(stderr, "gc_malloc: out of memory (requested %zu bytes)\n", size);
            abort();
        }
    }

    obj->size = size;
    ugc_register(&gc, &obj->base);
    obj_track(obj);

    gc_alloc_bytes += size;

    /* Trigger collection if pressure exceeds threshold */
    if (gc_alloc_bytes >= gc_threshold) {
        gc_alloc_bytes = 0;
        /* Non-blocking: kick the background thread */
    }

    pthread_mutex_unlock(&gc_lock);
    return TO_USER(obj);
}

/* Write barrier: call on every pointer store between heap objects.
 * parent_ptr and child_ptr are the user-visible payload pointers. */
void gc_write_barrier(void* parent_ptr, void* child_ptr) {
    if (!parent_ptr || !child_ptr) return;

    cpyte_obj_t* parent = TO_HDR(parent_ptr);
    cpyte_obj_t* child  = TO_HDR(child_ptr);

    /* quick validity check */
    if (!obj_is_tracked(parent) || !obj_is_tracked(child)) return;

    pthread_mutex_lock(&gc_lock);
    ugc_write_barrier(&gc, UGC_BARRIER_FORWARD, &parent->base, &child->base);
    pthread_mutex_unlock(&gc_lock);
}

void gc_collect(void) {
    pthread_mutex_lock(&gc_lock);
    ugc_collect(&gc);
    gc_alloc_bytes = 0;
    pthread_mutex_unlock(&gc_lock);
}

void gc_set_threshold(size_t bytes) {
    gc_threshold = bytes;
}

/* ── Background GC thread ───────────────────────────────────────── */

static void* gc_thread_fn(void* arg) {
    (void)arg;

    while (gc_running) {
        /* Step the GC if it has work to do */
        pthread_mutex_lock(&gc_lock);
        if (gc.state != UGC_IDLE) {
            ugc_step(&gc);
        } else if (gc_alloc_bytes >= gc_threshold) {
            /* Start a new collection cycle */
            ugc_step(&gc);  /* IDLE -> MARK, scans roots */
            gc_alloc_bytes = 0;
        }
        pthread_mutex_unlock(&gc_lock);

        /* Yield: sleep 1ms between steps */
        struct timespec ts = { 0, 1000000 };  /* 1 ms */
        nanosleep(&ts, NULL);
    }

    return NULL;
}

void gc_start_thread(void) {
    if (gc_thread) return;  /* already started */
    pthread_create(&gc_thread, NULL, gc_thread_fn, NULL);
}

void gc_stop_thread(void) {
    gc_running = 0;
    if (gc_thread) {
        pthread_join(gc_thread, NULL);
        gc_thread = (pthread_t)0;
    }
}

void gc_shutdown(void) {
    gc_stop_thread();
    pthread_mutex_lock(&gc_lock);
    ugc_release_all(&gc);
    pthread_mutex_unlock(&gc_lock);

    /* free remaining hash table entries */
    for (int i = 0; i < OBJ_TABLE_SIZE; i++) {
        obj_entry_t* e = obj_table[i];
        while (e) {
            obj_entry_t* next = e->next;
            free(e);
            e = next;
        }
        obj_table[i] = NULL;
    }
}

/* ── GC state queries ───────────────────────────────────────────── */

int gc_is_collecting(void) {
    return gc.state != UGC_IDLE;
}

size_t gc_heap_size(void) {
    /* approximate: count tracked objects */
    size_t count = 0;
    for (int i = 0; i < OBJ_TABLE_SIZE; i++) {
        obj_entry_t* e = obj_table[i];
        while (e) { count++; e = e->next; }
    }
    return count;
}
