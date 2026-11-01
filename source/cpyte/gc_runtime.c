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

#if !defined(_WIN32) && !defined(_GNU_SOURCE)
#define _GNU_SOURCE 1
#endif
#define UGC_IMPLEMENTATION
#define UGC_USE_TAGGED_POINTER 0
#include <stddef.h>
#include <stdint.h>
#include <stdatomic.h>
extern void gc_trace(const char* what, size_t v);
#include "ugc.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>

/* SIMD support for faster memcpy on modern CPUs */
#if defined(__x86_64__) || defined(_M_X64)
#include <immintrin.h>
#elif defined(__aarch64__) || defined(_M_ARM64)
#include <arm_neon.h>
#endif

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

/* Forward declarations for functions used by TLAB */
static void obj_track(void* hdr);

/* Forward declaration: collection is triggered from gc_malloc (foreground,
 * on the mutator thread) once the allocation threshold is crossed. */
void gc_collect(void);
void* gc_malloc(size_t size);

/* ── Global GC state ────────────────────────────────────────────── */

static ugc_t         gc;
static cpy_mutex_t   gc_lock_;
static volatile int  gc_lock_init_once = 0;
static cpy_thread_t  gc_thread;
static volatile int  gc_running   = 0;
/* Barriers are on the pointer-store hot path.  They only need to enter the
 * collector when a mark/sweep cycle is active, so keep that state atomic and
 * make the overwhelmingly common idle path lock-free. */
static atomic_int     gc_marking = 0;

static void gc_lock_init(void) {
    if (gc_lock_init_once) return;
    CPY_MUTEX_INIT(gc_lock_);
    gc_lock_init_once = 1;
}

/* allocation pressure for triggering collection */
static volatile size_t gc_alloc_bytes = 0;
static size_t          gc_threshold   = 0;  /* 0 = never auto-collect */

void gc_trace(const char* what, size_t v) {
    const char* d = getenv("CPYTE_GC_DEBUG");
    if (d && *d) fprintf(stderr, "[gc] %s %zu\n", what, v);
}

/* ── Reduced Lock Contention (Simplified TLAB) ─────────────────────
 * Full TLABs (Thread-Local Allocation Buffers) reduce lock contention but
 * complicate conservative GC scanning. This simplified implementation:
 * 1. Uses thread-local bump allocation for small objects
 * 2. Falls back to global lock for large objects or TLAB refill
 * 3. TLAB blocks are registered with GC for conservative scanning
 * ─────────────────────────────────────────────────────────────────── */

#define TLAB_SIZE (64 * 1024)  /* 64 KB per TLAB */
#define TLAB_MAX_ALLOC (2 * 1024)  /* Max size for TLAB fast path */
#define TLAB_STRING_SIZE (256 * 1024)

typedef struct {
    char *start;   /* Start of current TLAB */
    char *end;     /* End of current TLAB */
    char *current; /* Current bump pointer */
    char *last;    /* Most recent TLAB allocation */
    size_t last_size; /* Aligned size of the most recent allocation */
    size_t last_len;  /* String length, or SIZE_MAX when not a string */
} tlab_t;

/* Thread-local storage for TLAB.
 *
 * Native TLS (`__declspec(thread)` / `__thread`) is avoided on every POSIX
 * platform: the JIT path compiles this file to LLVM IR and emits a native
 * object via llvmlite's MCJIT, and the compiler lowers native TLS to a
 * `thread_local` LLVM global, which RuntimeDyld refuses to allocate
 * (`LLVM ERROR: allocation of TLS not implemented`; on macOS arm64 it would
 * also require the TLVP relocation ARM64_RELOC_TLVP_LOAD_PAGEOFF12). We back
 * the per-thread TLAB with a pthread key instead, so the emitted object is
 * TLS-free and each thread still gets its own buffer. Windows keeps native
 * TLS (no POSIX threads available). */
#ifdef _WIN32
__declspec(thread) static tlab_t tlab = {NULL, NULL, NULL, NULL, 0, SIZE_MAX};
#  define TLAB_GET() (&tlab)
#else
static pthread_key_t  tlab_key;
static pthread_once_t tlab_key_once = PTHREAD_ONCE_INIT;
static void tlab_key_create(void) { pthread_key_create(&tlab_key, NULL); }
static tlab_t* tlab_get(void) {
    pthread_once(&tlab_key_once, tlab_key_create);
    tlab_t* tl = (tlab_t*)pthread_getspecific(tlab_key);
    if (tl == NULL) {
        tl = (tlab_t*)calloc(1, sizeof(tlab_t));
        pthread_setspecific(tlab_key, tl);
    }
    return tl;
}
#  define TLAB_GET() tlab_get()
#endif

static void* tlab_alloc(size_t size, size_t min_block_size) {
    tlab_t *tl = TLAB_GET();
    if (size > SIZE_MAX - (sizeof(void*) - 1)) {
        return NULL;
    }
    /* Round up to pointer alignment */
    size = (size + sizeof(void*) - 1) & ~(sizeof(void*) - 1);

    if (tl->start == NULL || (size_t)(tl->end - tl->current) < size) {
        /* TLAB exhausted or not initialized: allocate new block from global heap */
        size_t block_size = (size > min_block_size) ? size : min_block_size;

        gc_lock_init();
        CPY_MUTEX_LOCK(&gc_lock_);

        /* Allocate the TLAB block through the normal GC path */
        cpyte_obj_t* hdr = (cpyte_obj_t*)calloc(1, sizeof(cpyte_obj_t) + block_size);
        if (hdr == NULL) {
            CPY_MUTEX_UNLOCK(&gc_lock_);
            return NULL;
        }
        hdr->size = block_size;
        ugc_register(&gc, &hdr->base);
        obj_track(hdr);

        CPY_MUTEX_UNLOCK(&gc_lock_);

        tl->start = PAYLOAD(hdr);
        tl->end = tl->start + block_size;
        tl->current = tl->start;
    }

    /* Bump-pointer allocation (lock-free) */
    void *ptr = tl->current;
    tl->current += size;
    tl->last = (char *)ptr;
    tl->last_size = size;
    tl->last_len = SIZE_MAX;
    return ptr;
}

/* ── Object hash table (conservative scan validation) ───────────── */

#define OBJ_TABLE_BITS 12
#define OBJ_TABLE_SIZE (1 << OBJ_TABLE_BITS)

typedef struct obj_entry_s {
    cpyte_obj_t*        hdr;    /* block header */
    size_t              size;   /* payload size (for interior-pointer containment) */
    uintptr_t            begin;  /* payload start, as an integer for safe range tests */
    uintptr_t            end;    /* exclusive end, saturated on overflow */
    struct obj_entry_s* next;   /* hash bucket chain */
    struct obj_entry_s* all;    /* containment list chain */
    struct obj_entry_s* all_prev;
} obj_entry_t;

static obj_entry_t* obj_table[OBJ_TABLE_SIZE];
static obj_entry_t* obj_list = NULL;  /* all tracked blocks, for containment search */
static obj_entry_t* obj_last_lookup = NULL;

static unsigned long hash_ptr(const void* p) {
    unsigned long v = (unsigned long)p;
    v = (v >> 4) * 2654435761UL;   /* Knuth multiplicative hash */
    return v & (OBJ_TABLE_SIZE - 1);
}

static void obj_track(void* hdr) {
    cpyte_obj_t* obj = (cpyte_obj_t*)hdr;
    unsigned long h = hash_ptr(hdr);
    obj_entry_t* e = (obj_entry_t*)malloc(sizeof(obj_entry_t));
    if (e == NULL) {
        fprintf(stderr, "gc: out of memory tracking object\n");
        abort();
    }
    e->hdr = (cpyte_obj_t*)hdr;
    e->size = obj->size;
    e->begin = (uintptr_t)PAYLOAD(obj);
    e->end = e->begin;
    if (e->size > UINTPTR_MAX - e->begin) {
        e->end = UINTPTR_MAX;
    } else {
        e->end += e->size;
    }
    e->next = obj_table[h];
    obj_table[h] = e;
    e->all = obj_list;
    e->all_prev = NULL;
    if (obj_list != NULL) obj_list->all_prev = e;
    obj_list = e;
    obj_last_lookup = NULL;
}

static void obj_untrack(void* hdr) {
    unsigned long h = hash_ptr(hdr);
    obj_entry_t** pp = &obj_table[h];
    while (*pp) {
        if ((*pp)->hdr == hdr) {
            obj_entry_t* tmp = *pp;
            *pp = tmp->next;
            /* also remove from the containment list */
            if (tmp->all_prev != NULL) tmp->all_prev->all = tmp->all;
            else obj_list = tmp->all;
            if (tmp->all != NULL) tmp->all->all_prev = tmp->all_prev;
            if (obj_last_lookup == tmp) obj_last_lookup = NULL;
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
        if (e->hdr == hdr) return 1;
        e = e->next;
    }
    return 0;
}

/* Find the tracked block that contains a caller-visible (payload-interior)
 * pointer. Conservative scans may produce pointers that land anywhere inside
 * an allocation, so an exact header match is not enough. */
static cpyte_obj_t* obj_find_containing(const void* user) {
    uintptr_t u = (uintptr_t)user;
    obj_entry_t* e;
    if (user == NULL) return NULL;
    e = obj_last_lookup;
    if (e != NULL && u >= e->begin && u < e->end) {
        return e->hdr;
    }
    for (e = obj_list; e; e = e->all) {
        if (e->size > 0 && u >= e->begin && u < e->end) {
            obj_last_lookup = e;
            return e->hdr;
        }
    }
    return NULL;
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

        /* check if candidate points into a tracked GC object */
        cpyte_obj_t* obj = obj_find_containing(candidate);
        if (obj) {
            ugc_visit(&gc, &obj->base);
        }
    }
}

static void scan_stack(void) {
#if defined(_WIN32)
    /* Windows: use GetCurrentThreadStackLimits (Win 8+). The reserved range
     * also covers UNCOMMITTED pages and the guard page at the committed edge;
     * reading those faults, so only walk MEM_COMMIT pages (skipping the
     * PAGE_GUARD boundary) via VirtualQuery. */
    ULONG_PTR lo = 0, hi = 0;
    GetCurrentThreadStackLimits(&lo, &hi);
    char* p = (char*)lo;
    while (p < (char*)hi) {
        MEMORY_BASIC_INFORMATION mbi;
        if (VirtualQuery(p, &mbi, sizeof(mbi)) == 0) break;
        char* reg_end = (char*)mbi.BaseAddress + mbi.RegionSize;
        if (mbi.State == MEM_COMMIT && !(mbi.Protect & PAGE_GUARD)) {
            char* begin = p > (char*)mbi.BaseAddress ? p : (char*)mbi.BaseAddress;
            char* end = (char*)hi < reg_end ? (char*)hi : reg_end;
            if (begin < end) scan_stack_range(begin, end);
        }
        if (reg_end <= p) break;  /* paranoia: no forward progress */
        p = reg_end;
    }
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

/* Array length registry (defined in runtime.c). */
extern void cpyte_array_unregister(void* arr);
typedef void *(*cpyte_array_alloc_hook_t)(size_t elem_size, size_t count);
typedef void *(*cpyte_array_reserve_hook_t)(void *arr, size_t elem_size,
                                            size_t increment);
extern void cpyte_array_set_gc_hooks(cpyte_array_alloc_hook_t alloc_hook,
                                     cpyte_array_reserve_hook_t reserve_hook);

typedef struct {
    size_t length;
    size_t capacity;
    unsigned char data[];
} cpyte_gc_array_header_t;

/* GC arrays live inside normal GC objects.  The user-visible pointer still
 * points just past the length/capacity header, preserving the existing ABI,
 * while the surrounding cpyte_obj_t makes the entire allocation visible to
 * conservative scanning and automatic collection. */
void* gc_array_alloc(size_t elem_size, size_t count) {
    if (count != 0 && elem_size >
            (SIZE_MAX - sizeof(cpyte_gc_array_header_t)) / count) {
        return NULL;
    }
    size_t total = sizeof(cpyte_gc_array_header_t) + elem_size * count;
    cpyte_gc_array_header_t *header =
        (cpyte_gc_array_header_t*)gc_malloc(total);
    if (header == NULL) return NULL;
    header->length = count;
    header->capacity = count;
    return header->data;
}

/* GC growth allocates a replacement object instead of calling realloc, which
 * would move memory outside the collector's object set.  The old object stays
 * tracked until it becomes unreachable and is then reclaimed normally. */
void* gc_array_reserve(void *arr, size_t elem_size, size_t increment) {
    if (arr == NULL || elem_size == 0) return arr;
    cpyte_gc_array_header_t *header =
        (cpyte_gc_array_header_t*)((char*)arr - sizeof(cpyte_gc_array_header_t));
    if (increment > SIZE_MAX - header->length) return arr;
    size_t want = header->length + increment;
    if (want <= header->capacity) return arr;

    size_t ncap = header->capacity;
    if (ncap < 4) ncap = 4;
    while (ncap < want) {
        if (ncap > SIZE_MAX / 2) {
            ncap = want;
            break;
        }
        ncap *= 2;
    }
    if (ncap > (SIZE_MAX - sizeof(cpyte_gc_array_header_t)) / elem_size)
        return arr;
    if (header->length != 0 && header->length > SIZE_MAX / elem_size)
        return arr;
    size_t total = sizeof(cpyte_gc_array_header_t) + elem_size * ncap;
    cpyte_gc_array_header_t *nu =
        (cpyte_gc_array_header_t*)gc_malloc(total);
    if (nu == NULL) return arr;
    nu->length = header->length;
    nu->capacity = ncap;
    if (header->length != 0) {
        memcpy(nu->data, header->data, header->length * elem_size);
    }
    return nu->data;
}

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

        cpyte_obj_t* child = obj_find_containing(candidate);
        if (child) {
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
    cpyte_array_set_gc_hooks(gc_array_alloc, gc_array_reserve);
    gc_alloc_bytes = 0;
    gc_running = 1;
    {
        const char* t = getenv("CPYTE_GC_THRESHOLD");
        if (t && *t) {
            size_t v = (size_t)strtoul(t, NULL, 10);
            if (v > 0) gc_threshold = v;
        }
    }

    /* Initialize TLAB for the current thread */
    TLAB_GET()->start = NULL;
    TLAB_GET()->end = NULL;
    TLAB_GET()->current = NULL;
    TLAB_GET()->last = NULL;
    TLAB_GET()->last_size = 0;
    TLAB_GET()->last_len = SIZE_MAX;
}

void* gc_malloc(size_t size) {
    /* Try TLAB first for small allocations (fast path, lock-free) */
    if (size <= TLAB_MAX_ALLOC) {
        void *ptr = tlab_alloc(size, TLAB_SIZE);
        if (ptr != NULL) {
            gc_alloc_bytes += size;
            if (gc_threshold != 0 && gc_alloc_bytes >= gc_threshold) {
                gc_trace("collect-trigger", gc_alloc_bytes);
                gc_collect();
            }
            return ptr;
        }
    }

    /* Fallback to global allocation for large objects or TLAB failure */
    gc_lock_init();
    CPY_MUTEX_LOCK(&gc_lock_);

    if (size > SIZE_MAX - sizeof(cpyte_obj_t)) {
        fprintf(stderr, "gc_malloc: allocation size overflow (%zu bytes)\n", size);
        abort();
    }
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

    if (gc_threshold != 0 && gc_alloc_bytes >= gc_threshold) {
        /* The object has not hit the mutator stack yet (it is about to be
         * returned in a register), so grey it explicitly to survive the
         * foreground collection. */
        gc_lock_init();
        CPY_MUTEX_LOCK(&gc_lock_);
        ugc_visit(&gc, &obj->base);
        CPY_MUTEX_UNLOCK(&gc_lock_);
        gc_collect();
    }

    return TO_USER(obj);
}

/*
 * Allocate a buffer whose caller initializes every byte. String concatenation
 * uses this path so larger results can share a thread-local arena instead of
 * requiring a tracked GC object and global-lock acquisition per result.
 *
 * SPEED OPTIMIZATIONS:
 * - Likely/unlikely branch prediction hints
 * - Fast path for very small allocations
 * - Reduced branching
 */
void* gc_malloc_uninit(size_t size) {
    // Fast path: very small allocations are the most common
    if (__builtin_expect(size <= TLAB_MAX_ALLOC, 1)) {
        void* ptr = tlab_alloc(size, TLAB_SIZE);
        if (__builtin_expect(ptr != NULL, 1)) {
            gc_alloc_bytes += size;
            // Only check threshold on larger accumulated allocations
            if (__builtin_expect(gc_threshold != 0 && gc_alloc_bytes >= gc_threshold, 0)) {
                gc_collect();
            }
            return ptr;
        }
    }
    
    // Zero-size allocation
    if (__builtin_expect(size == 0, 0)) {
        size = 1;
    }

    // Medium-sized allocations via TLAB
    if (__builtin_expect(size <= TLAB_STRING_SIZE, 1)) {
        void* ptr = tlab_alloc(size, TLAB_STRING_SIZE);
        if (__builtin_expect(ptr != NULL, 1)) {
            gc_alloc_bytes += size;
            if (__builtin_expect(gc_threshold != 0 && gc_alloc_bytes >= gc_threshold, 0)) {
                gc_collect();
            }
            return ptr;
        }
    }
    
    // Fallback to general GC
    return gc_malloc(size);
}

/* Write barrier: call on every pointer store between heap objects.
 * parent_ptr and child_ptr are the user-visible payload pointers. */
void gc_write_barrier(void* parent_ptr, void* child_ptr) {
    if (!parent_ptr || !child_ptr) return;

    /* In the idle state the tri-color invariant is not being maintained, so
     * no object lookup or mutex acquisition is necessary. */
    if (!atomic_load_explicit(&gc_marking, memory_order_acquire)) return;

    gc_lock_init();
    CPY_MUTEX_LOCK(&gc_lock_);
    if (!atomic_load_explicit(&gc_marking, memory_order_relaxed)) {
        CPY_MUTEX_UNLOCK(&gc_lock_);
        return;
    }
    /* The compiler passes the address of the destination slot, not
     * necessarily the beginning of its object.  Resolve interior pointers so
     * array elements and struct fields participate in the barrier too. */
    cpyte_obj_t* parent = obj_find_containing(parent_ptr);
    cpyte_obj_t* child  = obj_find_containing(child_ptr);
    if (parent != NULL && child != NULL) {
        ugc_write_barrier(&gc, UGC_BARRIER_FORWARD,
                          &parent->base, &child->base);
    }
    CPY_MUTEX_UNLOCK(&gc_lock_);
}

void gc_collect(void) {
    gc_lock_init();
    CPY_MUTEX_LOCK(&gc_lock_);
    atomic_store_explicit(&gc_marking, 1, memory_order_release);
    /* Protect the current TLAB block: its interior objects are live because
     * the mutator is executing; interior-pointer scan alone cannot see a block
     * that is only referenced through a bump-pointer interior alias. */
    if (TLAB_GET()->start != NULL && TLAB_GET()->current != TLAB_GET()->start) {
        cpyte_obj_t* hdr = (cpyte_obj_t*)((char*)TLAB_GET()->start - sizeof(cpyte_obj_t));
        if (obj_is_tracked(hdr)) {
            ugc_visit(&gc, &hdr->base);
        }
    }
    ugc_collect(&gc);
    gc_alloc_bytes = 0;
    atomic_store_explicit(&gc_marking, 0, memory_order_release);
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
        /* Step the GC if it has work to do. Collection cycles are started
         * synchronously on the mutator (in gc_malloc / gc_collect) so that the
         * conservative root scan runs on the thread that actually owns the
         * live JIT roots; the background thread must never begin a cycle,
         * because its own stack holds none of the mutator's roots. */
        CPY_MUTEX_LOCK(&gc_lock_);
        if (gc.state != UGC_IDLE) {
            atomic_store_explicit(&gc_marking, 1, memory_order_release);
            ugc_step(&gc);
            if (gc.state == UGC_IDLE) {
                atomic_store_explicit(&gc_marking, 0, memory_order_release);
            }
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
    if (gc_threshold == 0) return;  /* auto-collect off: no background stepper */
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
    cpyte_array_set_gc_hooks(NULL, NULL);
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

    /* Reset TLAB for current thread */
    TLAB_GET()->start = NULL;
    TLAB_GET()->end = NULL;
    TLAB_GET()->current = NULL;
    TLAB_GET()->last = NULL;
    TLAB_GET()->last_size = 0;
    TLAB_GET()->last_len = SIZE_MAX;
}


// GC version of optimized string concat
// SPEED OPTIMIZATIONS:
// - Ultra-small string optimization (<= 16 bytes) uses inline assembly rep movsb
// - Small string optimization (<= 31 bytes) uses inline copy
// - Prefetch for larger strings
// - Branch prediction hints
// - SIMD memcpy for very large strings (> 512 bytes)
char *
str_concat_const_gc(const char *left, const char *right, size_t right_len) {
    size_t left_len = strlen(left);
    size_t total_len = left_len + right_len;
    
    // Ultra-small string optimization: inline assembly rep movsb
    // This is the fastest path for tiny strings
    if (__builtin_expect(total_len <= 16, 1)) {
        char *result = gc_malloc_uninit(total_len + 1);
        if (!result) return NULL;
        
        fast_memcpy_small(result, left, left_len);
        fast_memcpy_small(result + left_len, right, right_len);
        result[total_len] = '\0';
        return result;
    }
    
    // Small string optimization: inline copy for small strings
    // Avoids memcpy overhead for the common case
    if (__builtin_expect(total_len <= 31, 1)) {
        char *result = gc_malloc_uninit(total_len + 1);
        if (!result) return NULL;
        
        // Inline copy for small strings - compiler will unroll and vectorize
        for (size_t i = 0; i < left_len; i++) {
            result[i] = left[i];
        }
        for (size_t i = 0; i < right_len; i++) {
            result[left_len + i] = right[i];
        }
        result[total_len] = '\0';
        return result;
    }
    
    // Larger strings: use memcpy with prefetch hints
    char *result = gc_malloc_uninit(total_len + 1);
    if (!result) return NULL;
    
    // Prefetch source data for larger strings
    if (total_len > 256) {
        __builtin_prefetch(left, 0, 3);
        __builtin_prefetch(right, 0, 3);
    }
    
    // Use SIMD-optimized copy for very large strings
    if (total_len > 512) {
        fast_memcpy_large(result, left, left_len);
        fast_memcpy_large(result + left_len, right, right_len);
    } else {
        memcpy(result, left, left_len);
        memcpy(result + left_len, right, right_len);
    }
    result[total_len] = '\0';
    return result;
}

/*
 * Append a compile-time string to a compiler-proven unique local.  The
 * compiler still uses the ordinary allocating semantics when uniqueness is
 * not proven; this helper only mutates a string when it is the most recent
 * initialized TLAB allocation and there is room directly after it.
 *
 * TLAB allocations are pointer-aligned.  Growing the last allocation in
 * place therefore only advances the bump pointer; no realloc or custom
 * memory-copy loop is needed.  The fallback remains GC-managed and uses the
 * same native memcpy operations as str_concat_const_gc.
 *
 * SPEED OPTIMIZATIONS:
 * - TLAB in-place growth (no allocation when possible)
 * - Small string optimization
 * - SIMD memcpy for large strings
 */
char *
str_concat_grow_const_gc(const char *left, const char *right, size_t right_len) {
    tlab_t *tl = TLAB_GET();
    size_t left_len;

    if (tl->last == (char *)left && tl->last_len != SIZE_MAX) {
        left_len = tl->last_len;
    } else {
        left_len = strlen(left);
    }

    if (right_len > SIZE_MAX - 1 || left_len > SIZE_MAX - right_len - 1) {
        return NULL;
    }
    size_t total_len = left_len + right_len;
    size_t needed = total_len + 1;

    // FAST PATH: In-place TLAB growth (avoid allocation entirely)
    if (tl->last == (char *)left && tl->last_len == left_len &&
            tl->current == tl->last + tl->last_size &&
            needed >= tl->last_size &&
            needed <= SIZE_MAX - (sizeof(void*) - 1)) {
        size_t rounded = (needed + sizeof(void*) - 1) &
                         ~(sizeof(void*) - 1);
        size_t available = (size_t)(tl->end - tl->current);
        if (rounded >= tl->last_size && rounded - tl->last_size <= available) {
            size_t growth = rounded - tl->last_size;
            
            // Use SIMD for large appends
            if (right_len > 512) {
                fast_memcpy_large((char *)left + left_len, right, right_len);
            } else {
                memcpy((char *)left + left_len, right, right_len);
            }
            ((char *)left)[total_len] = '\0';
            tl->current += growth;
            tl->last_size = rounded;
            tl->last_len = total_len;
            gc_alloc_bytes += growth;
            if (gc_threshold != 0 && gc_alloc_bytes >= gc_threshold) {
                gc_collect();
            }
            return (char *)left;
        }
    }

    // FALLBACK: Allocate new string
    char *result = gc_malloc_uninit(needed);
    if (!result) return NULL;
    
    // Small string optimization
    if (__builtin_expect(total_len <= 31, 1)) {
        for (size_t i = 0; i < left_len; i++) {
            result[i] = left[i];
        }
        for (size_t i = 0; i < right_len; i++) {
            result[left_len + i] = right[i];
        }
    } else {
        // Use SIMD for large strings
        if (total_len > 512) {
            fast_memcpy_large(result, left, left_len);
            fast_memcpy_large(result + left_len, right, right_len);
        } else {
            memcpy(result, left, left_len);
            memcpy(result + left_len, right, right_len);
        }
    }
    result[total_len] = '\0';
    tl = TLAB_GET();
    if (tl->last == result) {
        tl->last_len = total_len;
    }
    return result;
}


char *
str_concat_const_both_gc(const char *left, const char *right, size_t left_len, size_t right_len) {
    size_t total_len = left_len + right_len;
    
    // Small string optimization: inline copy for very small strings
    // Both lengths known, so we can be very aggressive
    if (__builtin_expect(total_len <= 31, 1)) {
        char *result = gc_malloc_uninit(total_len + 1);
        if (!result) return NULL;
        
        // Inline copy - compiler will optimize heavily
        for (size_t i = 0; i < left_len; i++) {
            result[i] = left[i];
        }
        for (size_t i = 0; i < right_len; i++) {
            result[left_len + i] = right[i];
        }
        result[total_len] = '\0';
        return result;
    }
    
    // Medium to large strings: use optimized memcpy
    char *result = gc_malloc_uninit(total_len + 1);
    if (!result) return NULL;
    
    // Prefetch for larger strings
    if (total_len > 256) {
        __builtin_prefetch(left, 0, 3);
        __builtin_prefetch(right, 0, 3);
    }
    
    // Use standard memcpy - modern compilers will use SIMD automatically
    // For very large strings, memcpy is already highly optimized
    memcpy(result, left, left_len);
    memcpy(result + left_len, right, right_len);
    result[total_len] = '\0';
    return result;
}

/* Inline assembly for ultra-fast small string copy (<= 16 bytes)
 * This avoids all function call overhead for the most common case
 */
static inline void fast_memcpy_small(char *dst, const char *src, size_t n) {
#if defined(__x86_64__) && !defined(_MSC_VER)
    // Use inline assembly for x86_64 GCC/Clang
    __asm__ volatile (
        "rep movsb"
        : "+D"(dst), "+S"(src), "+c"(n)
        : : "memory", "cc"
    );
#elif defined(_MSC_VER) && defined(_M_X64)
    // MSVC x64 doesn't support inline asm, use intrinsic
    __movsb((unsigned char*)dst, (const unsigned char*)src, n);
#else
    // Fallback for other platforms
    memcpy(dst, src, n);
#endif
}

/* SIMD-optimized fast copy for large strings (> 512 bytes)
 * This bypasses the standard memcpy for better control on large buffers
 */
static inline void fast_memcpy_large(char *dst, const char *src, size_t n) {
#if defined(__x86_64__) || defined(_M_X64)
    // AVX2 32-byte copies for x86_64
    size_t i = 0;
    const __m256i *src256 = (const __m256i *)src;
    __m256i *dst256 = (__m256i *)dst;
    
    // Align to 32-byte boundary
    while (i + 32 <= n && ((uintptr_t)(src + i) & 31)) {
        dst[i] = src[i];
        i++;
    }
    
    // 32-byte unrolled copy
    while (i + 128 <= n) {
        __m256i v0 = _mm256_loadu_si256(src256 + 0);
        __m256i v1 = _mm256_loadu_si256(src256 + 1);
        __m256i v2 = _mm256_loadu_si256(src256 + 2);
        __m256i v3 = _mm256_loadu_si256(src256 + 3);
        _mm256_storeu_si256(dst256 + 0, v0);
        _mm256_storeu_si256(dst256 + 1, v1);
        _mm256_storeu_si256(dst256 + 2, v2);
        _mm256_storeu_si256(dst256 + 3, v3);
        src256 += 4;
        dst256 += 4;
        i += 128;
    }
    
    // Finish remaining bytes
    while (i + 32 <= n) {
        __m256i v = _mm256_loadu_si256(src256);
        _mm256_storeu_si256(dst256, v);
        src256++;
        dst256++;
        i += 32;
    }
    
    // Tail
    while (i < n) {
        dst[i] = src[i];
        i++;
    }
#elif defined(__aarch64__) || defined(_M_ARM64)
    // NEON 16-byte copies for ARM64
    size_t i = 0;
    const uint8x16_t *src16 = (const uint8x16_t *)src;
    uint8x16_t *dst16 = (uint8x16_t *)dst;
    
    while (i + 64 <= n) {
        uint8x16_t v0 = vld1q_u8((const uint8_t *)src16 + 0);
        uint8x16_t v1 = vld1q_u8((const uint8_t *)src16 + 1);
        uint8x16_t v2 = vld1q_u8((const uint8_t *)src16 + 2);
        uint8x16_t v3 = vld1q_u8((const uint8_t *)src16 + 3);
        vst1q_u8((uint8_t *)dst16 + 0, v0);
        vst1q_u8((uint8_t *)dst16 + 1, v1);
        vst1q_u8((uint8_t *)dst16 + 2, v2);
        vst1q_u8((uint8_t *)dst16 + 3, v3);
        src16 += 4;
        dst16 += 4;
        i += 64;
    }
    
    while (i + 16 <= n) {
        uint8x16_t v = vld1q_u8((const uint8_t *)src16);
        vst1q_u8((uint8_t *)dst16, v);
        src16++;
        dst16++;
        i += 16;
    }
    
    while (i < n) {
        dst[i] = src[i];
        i++;
    }
#else
    // Fallback to standard memcpy
    memcpy(dst, src, n);
#endif
}
