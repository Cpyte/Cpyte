/*
 * gc_runtime.c - Cpyte concurrent tri-color mark GC
 *
 * Wraps ugc (https://github.com/bullno1/ugc) with:
 *   - Background collector thread (pthread / Windows thread)
 *   - Conservative stack scanning
 *   - Object tracking for pointer validation
 *   - Write barrier for tri-color invariant
 *
 * Cross-platform: pthread on POSIX (macOS/Linux/BSD), native Windows
 * threads + CRITICAL_SECTION on _WIN32. A portable sleep helper replaces
 * the POSIX-only nanosleep.
 *
 * License: BSD-2-Clause (ugc) + project license
 */

#define UGC_IMPLEMENTATION
#define UGC_USE_TAGGED_POINTER 0
#include "ugc.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>

/* --- Platform threading / mutex abstraction (mirrors runtime.c) --- */
#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <process.h>
typedef CRITICAL_SECTION cpy_mutex_t;
#  define CPY_MUTEX_INIT(ctx) do { InitializeCriticalSection(&(ctx)); } while(0)
#  define CPY_MUTEX_LOCK(ctx)   EnterCriticalSection(ctx)
#  define CPY_MUTEX_UNLOCK(ctx) LeaveCriticalSection(ctx)
typedef unsigned long cpy_thread_t;
#  define CPY_THREAD_START(tid, fn, arg) ((*tid) = (uintptr_t)_beginthreadex(NULL, 0, fn, arg, 0, NULL), (*tid) != 0)
#  define CPY_THREAD_JOIN(tid)  WaitForSingleObject((HANDLE)(tid), INFINITE), CloseHandle((HANDLE)(tid))
#  define CPY_SLEEP_MS(ms)      Sleep(ms)
#else
#  include <unistd.h>
#  include <pthread.h>
#  include <time.h>
typedef pthread_mutex_t cpy_mutex_t;
#  define CPY_MUTEX_INIT(ctx) do { (ctx) = (cpy_mutex_t)PTHREAD_MUTEX_INITIALIZER; } while(0)
#  define CPY_MUTEX_LOCK(ctx)   pthread_mutex_lock(ctx)
#  define CPY_MUTEX_UNLOCK(ctx) pthread_mutex_unlock(ctx)
typedef pthread_t cpy_thread_t;
#  define CPY_THREAD_START(tid, fn, arg) (pthread_create((tid), NULL, fn, arg) == 0)
#  define CPY_THREAD_JOIN(tid)  pthread_join((tid), NULL)
#  define CPY_SLEEP_MS(ms)      do { struct timespec __ts = { (ms) / 1000, ((ms) % 1000) * 1000000L }; nanosleep(&__ts, NULL); } while(0)
#endif

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
static cpy_mutex_t   gc_lock_;
static volatile int  gc_lock_init_once = 0;
static cpy_thread_t  gc_thread;
static volatile int  gc_running   = 0;

static void gc_lock_init(void) {
    if (gc_lock_init_once) return;
    CPY_MUTEX_INIT(gc_lock_);
    gc_lock_init_once = 1;
}

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
    if (lo > hi) { void* t = lo; lo = hi; hi = t; }
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
#if defined(_WIN32)
    /* Windows: use GetCurrentThreadStackLimits (Win 8+) */
    ULONG_PTR lo = 0, hi = 0;
    GetCurrentThreadStackLimits(&lo, &hi);
    scan_stack_range((void*)lo, (void*)hi);
#elif defined(__APPLE__)
    /* macOS: use pthread APIs */
    pthread_t self = pthread_self();
    void* stack_addr  = pthread_get_stackaddr_np(self);
    size_t stack_size = pthread_get_stacksize_np(self);
    /* stack grows downward: [stack_addr - stack_size, stack_addr) */
    void* lo = (char*)stack_addr - stack_size;
    void* hi = stack_addr;
    scan_stack_range(lo, hi);
#elif defined(__linux__)
    /* Linux: read pthread_getattr_np for the current thread (no /proc parsing) */
    pthread_attr_t attr;
    if (pthread_getattr_np(pthread_self(), &attr) == 0) {
        void* stack_addr = NULL;
        size_t stack_size = 0;
        pthread_attr_getstack(&attr, &stack_addr, &stack_size);
        pthread_attr_destroy(&attr);
        scan_stack_range(stack_addr, (char*)stack_addr + stack_size);
    }
#else
    /* Fallback: scan a conservative range around the frame pointer.
     * Not perfect but catches most stack objects. */
    volatile void* frame = __builtin_frame_address(0);
    scan_stack_range((char*)frame - 64 * 1024, (char*)frame + 1024);
#endif
}

/* ── ugc callbacks ──────────────────────────────────────────────── */

/* Array length registry (defined in runtime.c): unregister arrays when their
 * object is collected so stale length entries can never be reused. */
extern void cpyte_array_unregister(void* arr);

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
    cpyte_array_unregister(TO_USER(obj));
    free(obj);
}

/* ── Public API (called from generated code) ────────────────────── */

void gc_init(void) {
    gc_lock_init();
    memset(obj_table, 0, sizeof(obj_table));
    ugc_init(&gc, gc_scan_cb, gc_release_cb);
    gc_alloc_bytes = 0;
    gc_running = 1;
}

void* gc_malloc(size_t size) {
    gc_lock_init();
    CPY_MUTEX_LOCK(&gc_lock_);

    cpyte_obj_t* obj = (cpyte_obj_t*)calloc(1, sizeof(cpyte_obj_t) + size);
    if (!obj) {
        /* Emergency: force a full collect and retry */
        ugc_collect(&gc);
        obj = (cpyte_obj_t*)calloc(1, sizeof(cpyte_obj_t) + size);
        if (!obj) {
            CPY_MUTEX_UNLOCK(&gc_lock_);
            fprintf(stderr, "gc_malloc: out of memory (requested %zu bytes)\n", size);
            abort();
        }
    }

    obj->size = size;
    ugc_register(&gc, &obj->base);
    obj_track(obj);

    gc_alloc_bytes += size;

    CPY_MUTEX_UNLOCK(&gc_lock_);
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

    gc_lock_init();
    CPY_MUTEX_LOCK(&gc_lock_);
    ugc_write_barrier(&gc, UGC_BARRIER_FORWARD, &parent->base, &child->base);
    CPY_MUTEX_UNLOCK(&gc_lock_);
}

void gc_collect(void) {
    gc_lock_init();
    CPY_MUTEX_LOCK(&gc_lock_);
    ugc_collect(&gc);
    gc_alloc_bytes = 0;
    CPY_MUTEX_UNLOCK(&gc_lock_);
}

/* ── Background GC thread ───────────────────────────────────────── */

#ifdef _WIN32
static unsigned int __stdcall gc_thread_fn(void* arg) {
    (void)arg;
#else
static void* gc_thread_fn(void* arg) {
    (void)arg;
#endif

    while (gc_running) {
        /* Step the GC if it has work to do */
        CPY_MUTEX_LOCK(&gc_lock_);
        if (gc.state != UGC_IDLE) {
            ugc_step(&gc);
        } else if (gc_alloc_bytes >= gc_threshold) {
            /* Start a new collection cycle */
            ugc_step(&gc);  /* IDLE -> MARK, scans roots */
            gc_alloc_bytes = 0;
        }
        CPY_MUTEX_UNLOCK(&gc_lock_);

        /* Yield: sleep 1ms between steps */
        CPY_SLEEP_MS(1);
    }

#ifdef _WIN32
    return 0;
#else
    return NULL;
#endif
}

void gc_start_thread(void) {
    gc_lock_init();
    if (gc_thread) return;  /* already started */
    CPY_THREAD_START(&gc_thread, gc_thread_fn, NULL);
}

void gc_stop_thread(void) {
    gc_running = 0;
    if (gc_thread) {
        CPY_THREAD_JOIN(gc_thread);
        gc_thread = (cpy_thread_t)0;
    }
}

void gc_shutdown(void) {
    gc_stop_thread();
    CPY_MUTEX_LOCK(&gc_lock_);
    ugc_release_all(&gc);
    CPY_MUTEX_UNLOCK(&gc_lock_);

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
