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
#include <stdalign.h>


#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  include <pthread.h>
#  include <unistd.h>
#endif

/* === HYBRID LINEAR GC-MALLOC ALLOCATOR SYSTEM === */

/* General purpose malloc allocator - fast wrapper */
static inline void* cpyte_general_malloc(size_t size) {
    if (size == 0) size = 1;
    void *ptr = malloc(size);
    return ptr;
}

static inline void* cpyte_general_calloc(size_t nmemb, size_t size) {
    if (nmemb == 0 || size == 0) return malloc(1);
    return calloc(nmemb, size);
}

static inline void* cpyte_general_realloc(void *ptr, size_t size) {
    if (size == 0) {
        free(ptr);
        return malloc(1);
    }
    return realloc(ptr, size);
}

static inline void cpyte_general_free(void *ptr) {
    if (ptr) free(ptr);
}

/* Stack/Linear allocator for scope-limited allocations */
typedef struct {
    unsigned char *memory;
    size_t capacity;
    size_t offset;
    _Atomic int lock;
} LinearScopeAllocator;

typedef struct {
    size_t previous_offset;
    size_t allocation_offset;
    uint32_t magic;
} LinearHeader;

#define LINEAR_MAGIC 0x4C494E52 /* 'LINR' */

static inline size_t linear_align_up(size_t x, size_t align) {
    return (x + align - 1) & ~(align - 1);
}

/* pthread_t is an arithmetic/pointer type on every supported platform, so a
 * direct compare avoids a call on the allocation hot path. */
#ifndef _WIN32
#  if defined(__APPLE__) || defined(__linux__)
#    define CPY_SAME_THREAD(a, b) ((a) == (b))
#  else
#    define CPY_SAME_THREAD(a, b) pthread_equal((a), (b))
#  endif
#endif

#ifdef _MSC_VER
#  define CPY_INLINE static __forceinline
#elif defined(__GNUC__) || defined(__clang__)
#  define CPY_INLINE static __inline__ __attribute__((always_inline))
#else
#  define CPY_INLINE static inline
#endif

#ifndef _WIN32
/* pthread_self() is a library call (~2.5ns) and the allocation hot path used
 * to pay it on every bump.  A thread's stack is a private mapping, so a stack
 * address inside the mutator's recorded range can only belong to that thread:
 * testing the current frame against the range is a few integer ops, no call.
 * Other threads (and fibers on borrowed stacks) fall through to the tid
 * compare / pthread key, which stays correct. */
static uintptr_t gc_mutator_stack_lo   = 0;
static uintptr_t gc_mutator_stack_size = 0;

static void gc_record_mutator_stack(void) {
#if defined(__APPLE__)
    pthread_t self = pthread_self();
    uintptr_t hi = (uintptr_t)pthread_get_stackaddr_np(self);
    size_t    sz = pthread_get_stacksize_np(self);
    gc_mutator_stack_lo   = hi - sz;
    gc_mutator_stack_size = sz;
#elif defined(__linux__)
    pthread_attr_t attr;
    if (pthread_getattr_np(pthread_self(), &attr) == 0) {
        void*  lo = NULL;
        size_t sz = 0;
        if (pthread_attr_getstack(&attr, &lo, &sz) == 0) {
            gc_mutator_stack_lo   = (uintptr_t)lo;
            gc_mutator_stack_size = sz;
        }
        pthread_attr_destroy(&attr);
    }
#endif
}

CPY_INLINE int cpy_on_mutator_stack(const void* probe) {
    return gc_mutator_stack_lo != 0 &&
           (uintptr_t)probe - gc_mutator_stack_lo < gc_mutator_stack_size;
}
#endif

#ifdef _WIN32
__declspec(thread) static LinearScopeAllocator g_linear_scope = {NULL, 0, 0, 0};
#  define LINEAR_SCOPE_GET() (&g_linear_scope)
#else
static pthread_key_t linear_scope_key;
static pthread_once_t linear_scope_key_once = PTHREAD_ONCE_INIT;
static void linear_scope_key_create(void) { pthread_key_create(&linear_scope_key, NULL); }

/* Scope entry/exit run on every function call, so the mutator thread's state
 * is cached (see tlab_get) instead of paying a pthread key lookup each time. */
static LinearScopeAllocator* linear_scope_main = NULL;
static pthread_t            linear_scope_main_tid;
static int                  linear_scope_main_valid = 0;

static LinearScopeAllocator* linear_scope_get(void) {
    char probe = 0;
    if (linear_scope_main_valid &&
            (cpy_on_mutator_stack(&probe) ||
             CPY_SAME_THREAD(pthread_self(), linear_scope_main_tid))) {
        return linear_scope_main;
    }
    pthread_once(&linear_scope_key_once, linear_scope_key_create);
    LinearScopeAllocator* ls = (LinearScopeAllocator*)pthread_getspecific(linear_scope_key);
    if (ls == NULL) {
        ls = (LinearScopeAllocator*)calloc(1, sizeof(LinearScopeAllocator));
        if (ls != NULL) pthread_setspecific(linear_scope_key, ls);
    }
    return ls;
}
#  define LINEAR_SCOPE_GET() linear_scope_get()
#endif

static int linear_scope_init_if_needed(LinearScopeAllocator *ls, size_t capacity) {
    if (ls->memory) return 1;
    if (capacity == 0) capacity = 256 * 1024; /* 256KB default */
    ls->memory = (unsigned char*)malloc(capacity);
    if (!ls->memory) return 0;
    ls->capacity = capacity;
    ls->offset = 0;
    atomic_store(&ls->lock, 0);
    return 1;
}

void* cpyte_linear_alloc(size_t size) {
    LinearScopeAllocator *ls = LINEAR_SCOPE_GET();
    if (!linear_scope_init_if_needed(ls, 256 * 1024)) {
        return cpyte_general_malloc(size);
    }
    const size_t alignment = alignof(max_align_t);
    size_t header_pos = ls->offset;
    uintptr_t base = (uintptr_t)ls->memory;
    uintptr_t raw = base + header_pos + sizeof(LinearHeader);
    size_t padding = (size_t)(-(uintptr_t)raw) & (alignment - 1);
    size_t data_pos = header_pos + sizeof(LinearHeader) + padding;
    if (data_pos > ls->capacity || size > ls->capacity - data_pos) {
        /* Grow if needed - use general malloc as fallback for overflow */
        return cpyte_general_malloc(size);
    }
    LinearHeader *header = (LinearHeader *)(ls->memory + data_pos - sizeof(LinearHeader));
    header->previous_offset = ls->offset;
    header->allocation_offset = data_pos;
    header->magic = LINEAR_MAGIC;
    ls->offset = data_pos + size;
    return ls->memory + data_pos;
}

void cpyte_linear_free_last(void *ptr) {
    if (!ptr) return;
    LinearScopeAllocator *ls = LINEAR_SCOPE_GET();
    if (!ls->memory) return;
    uintptr_t base = (uintptr_t)ls->memory;
    uintptr_t address = (uintptr_t)ptr;
    if (address < base + sizeof(LinearHeader) || address >= base + ls->offset) return;
    LinearHeader *header = (LinearHeader *)(address - sizeof(LinearHeader));
    if (header->magic != LINEAR_MAGIC || header->allocation_offset != address - base) return;
    ls->offset = header->previous_offset;
}

void cpyte_scope_push(void) {
    /* Push a scope marker - save current offset */
    LinearScopeAllocator *ls = LINEAR_SCOPE_GET();
    if (!linear_scope_init_if_needed(ls, 256 * 1024)) return;
    /* Store marker by allocating a small marker */
    /* We'll use a simple approach - just ensure allocator is ready */
}

void cpyte_scope_pop(void) {
    /* Pop scope - reset to previous marker */
    /* For simplicity and safety, we can track with a stack of markers */
    /* This is a simplified version */
}

#ifndef _WIN32
static pthread_key_t scope_marker_key;
static pthread_once_t scope_marker_key_once = PTHREAD_ONCE_INIT;
typedef struct { size_t markers[64]; int depth; } ScopeMarkerState;
static void scope_marker_key_create(void) { pthread_key_create(&scope_marker_key, NULL); }

static ScopeMarkerState* scope_main = NULL;
static pthread_t         scope_main_tid;
static int               scope_main_valid = 0;

CPY_INLINE ScopeMarkerState* scope_marker_get(void) {
    char probe = 0;
    if (scope_main_valid &&
            (cpy_on_mutator_stack(&probe) ||
             CPY_SAME_THREAD(pthread_self(), scope_main_tid))) {
        return scope_main;
    }
    pthread_once(&scope_marker_key_once, scope_marker_key_create);
    ScopeMarkerState* st = (ScopeMarkerState*)pthread_getspecific(scope_marker_key);
    if (st == NULL) {
        st = (ScopeMarkerState*)calloc(1, sizeof(ScopeMarkerState));
        if (st != NULL) pthread_setspecific(scope_marker_key, st);
    }
    return st;
}
#define SCOPE_MARKERS_GET() scope_marker_get()->markers
#define SCOPE_DEPTH_GET() scope_marker_get()->depth
#define SCOPE_DEPTH_SET(v) scope_marker_get()->depth = (v)
#else
static __declspec(thread) size_t g_scope_markers[64];
static __declspec(thread) int g_scope_marker_depth = 0;
#define SCOPE_MARKERS_GET() g_scope_markers
#define SCOPE_DEPTH_GET() g_scope_marker_depth
#define SCOPE_DEPTH_SET(v) g_scope_marker_depth = (v)
#endif

void cpyte_scope_enter(void) {
    LinearScopeAllocator *ls = LINEAR_SCOPE_GET();
    if (ls == NULL) return;
    /* No need to materialize the linear buffer here: an unused allocator has
     * offset 0, and cpyte_linear_alloc initializes it on first use. */
#ifdef _WIN32
    if (g_scope_marker_depth < 64) {
        g_scope_markers[g_scope_marker_depth++] = ls->offset;
    }
#else
    ScopeMarkerState* st = scope_marker_get();
    if (st != NULL && st->depth < 64) {
        st->markers[st->depth++] = ls->offset;
    }
#endif
}

void cpyte_scope_exit(void) {
    LinearScopeAllocator *ls = LINEAR_SCOPE_GET();
    if (ls == NULL || !ls->memory) return;
#ifdef _WIN32
    if (g_scope_marker_depth > 0) {
        g_scope_marker_depth--;
        ls->offset = g_scope_markers[g_scope_marker_depth];
    } else {
        ls->offset = 0;
    }
#else
    ScopeMarkerState* st = scope_marker_get();
    if (st == NULL) return;
    if (st->depth > 0) {
        st->depth--;
        ls->offset = st->markers[st->depth];
    } else {
        ls->offset = 0;
    }
#endif
}

void cpyte_linear_reset(void) {
    LinearScopeAllocator *ls = LINEAR_SCOPE_GET();
    if (ls) ls->offset = 0;
}

/* Multi-threaded TLAB allocation - enhanced */
#ifndef _WIN32
static pthread_key_t tlab_fast_key;
static pthread_once_t tlab_fast_key_once = PTHREAD_ONCE_INIT;
typedef struct { char* buffer; size_t size; size_t offset; } TLBFast;
static void tlab_fast_key_create(void) { pthread_key_create(&tlab_fast_key, NULL); }
static TLBFast* tlab_fast_get(void) {
    pthread_once(&tlab_fast_key_once, tlab_fast_key_create);
    TLBFast* t = (TLBFast*)pthread_getspecific(tlab_fast_key);
    if (t == NULL) {
        t = (TLBFast*)calloc(1, sizeof(TLBFast));
        pthread_setspecific(tlab_fast_key, t);
    }
    return t;
}
#else
static __declspec(thread) char* g_tlab_buffer = NULL;
static __declspec(thread) size_t g_tlab_size = 0;
static __declspec(thread) size_t g_tlab_offset = 0;
#endif

void* cpyte_tlab_alloc_fast(size_t size) {
    const size_t align = alignof(max_align_t);
    size_t aligned = linear_align_up(size, align);
    TLBFast* tfast = tlab_fast_get();
    if (tfast->buffer && tfast->offset + aligned <= tfast->size) {
        void *ptr = tfast->buffer + tfast->offset;
        tfast->offset += aligned;
        return ptr;
    }
    /* Allocate new TLAB */
    size_t new_size = aligned > 64 * 1024 ? aligned : 64 * 1024;
    tfast->buffer = (char*)malloc(new_size);
    if (!tfast->buffer) return cpyte_general_malloc(size);
    tfast->size = new_size;
    tfast->offset = aligned;
    return tfast->buffer;
}

void cpyte_tlab_reset(void) {
    TLBFast* tfast = tlab_fast_get();
    if (tfast) tfast->offset = 0;
}

/* Hybrid allocation: choose best allocator based on size and context */
void* cpyte_hybrid_alloc(size_t size) {
    /* Small objects - use linear/scope allocator for fast allocation */
    if (size <= 1024) {
        void *ptr = cpyte_linear_alloc(size);
        if (ptr) return ptr;
    }
    /* Medium objects - use TLAB fast path */
    if (size <= 4096) {
        void *ptr = cpyte_tlab_alloc_fast(size);
        if (ptr) return ptr;
    }
    /* Large objects or fallback - use general malloc (will be GC managed if needed) */
    return cpyte_general_malloc(size);
}

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

typedef struct cpyte_obj_s {
    ugc_header_t base;
    size_t       size;   /* payload size for conservative scanning */
    size_t       used;   /* bytes handed out so far (TLAB blocks: bump end) */
    struct cpyte_obj_s* next;
    struct cpyte_obj_s* all;
    struct cpyte_obj_s* all_prev;
    struct cpyte_page_link_s* pages;
} cpyte_obj_t;

/* Page-indexed lookup entry: maps one 4K page of a block's payload back to the
 * block header.  Conservative scanning resolves a candidate pointer in O(1)
 * instead of walking every live block. */
struct cpyte_page_link_s {
    cpyte_obj_t* blk;
    struct cpyte_page_link_s* next;
};
typedef struct cpyte_page_link_s cpyte_page_link_t;

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
 * make the overwhelmingly common idle path lock-free.  This flag is *not*
 * static: generated code reads it directly and only calls gc_write_barrier
 * when it is set, so an idle store costs one load + branch. */
atomic_int     gc_marking = 0;

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

#define TLAB_SIZE (128 * 1024)  /* 128 KB per TLAB */
#define TLAB_MAX_ALLOC (16 * 1024)  /* Max size for TLAB fast path */
#define TLAB_STRING_SIZE (1024 * 1024)

typedef struct {
    char *start;   /* Start of current TLAB */
    char *end;     /* End of current TLAB */
    char *current; /* Current bump pointer */
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
__declspec(thread) static tlab_t tlab = {NULL, NULL, NULL};
#  define TLAB_GET() (&tlab)
#else
static pthread_key_t  tlab_key;
static pthread_once_t tlab_key_once = PTHREAD_ONCE_INIT;
static void tlab_key_create(void) { pthread_key_create(&tlab_key, NULL); }

/* pthread_once + pthread_getspecific cost ~5ns and were evaluated several
 * times per allocation, dominating the bump-allocation fast path.  The thread
 * that calls gc_init (the mutator) gets its buffer cached; the hot path
 * recognizes it by stack range (no call), falling back to a tid compare and
 * then the key for other threads.  A cached tid is safe because that thread is
 * alive for as long as it can call this. */
static tlab_t*   tlab_main = NULL;
static pthread_t tlab_main_tid;
static int       tlab_main_valid = 0;

CPY_INLINE tlab_t* tlab_get(void) {
    char probe = 0;
    if (tlab_main_valid &&
            (cpy_on_mutator_stack(&probe) ||
             CPY_SAME_THREAD(pthread_self(), tlab_main_tid))) {
        return tlab_main;
    }
    pthread_once(&tlab_key_once, tlab_key_create);
    tlab_t* tl = (tlab_t*)pthread_getspecific(tlab_key);
    if (tl == NULL) {
        tl = (tlab_t*)calloc(1, sizeof(tlab_t));
        if (tl != NULL) pthread_setspecific(tlab_key, tl);
    }
    return tl;
}
#  define TLAB_GET() tlab_get()
#endif

/* Slow path: grab a fresh block from the global heap and bump once out of it. */
static void* tlab_refill(size_t size, size_t min_block_size) {
    size_t block_size = (size > min_block_size) ? size : min_block_size;

    gc_lock_init();
    CPY_MUTEX_LOCK(&gc_lock_);

    /* Allocate the TLAB block through the normal GC path */
    cpyte_obj_t* hdr = (cpyte_obj_t*)malloc(sizeof(cpyte_obj_t) + block_size);
    if (hdr == NULL) {
        CPY_MUTEX_UNLOCK(&gc_lock_);
        return NULL;
    }
    memset(hdr, 0, sizeof(cpyte_obj_t));
    hdr->size = block_size;
    hdr->used = size;
    ugc_register(&gc, &hdr->base);
    obj_track(hdr);

    CPY_MUTEX_UNLOCK(&gc_lock_);

    tlab_t* tl = TLAB_GET();
    if (tl == NULL) return NULL;
    /* Freeze the retired block's scan extent: the bump fast path only keeps
     * the *current* block's `used` up to date. */
    if (tl->start != NULL) {
        ((cpyte_obj_t*)(tl->start - sizeof(cpyte_obj_t)))->used =
            (size_t)(tl->current - tl->start);
    }
    tl->start = PAYLOAD(hdr);
    tl->end = tl->start + block_size;
    tl->current = tl->start + size;
    return tl->start;
}

CPY_INLINE void* tlab_alloc(size_t size, size_t min_block_size) {
    if (size > SIZE_MAX - (sizeof(void*) - 1)) {
        return NULL;
    }
    /* Round up to pointer alignment */
    size = (size + sizeof(void*) - 1) & ~(sizeof(void*) - 1);

    tlab_t* tl = TLAB_GET();
    if (tl != NULL) {
        char* cur = tl->current;
        if (cur != NULL && (size_t)(tl->end - cur) >= size) {
            /* Bump-pointer allocation (lock-free) */
            char* next = cur + size;
            tl->current = next;
            /* Keep the scan extent of the enclosing block current.  Only
             * needed when collection can run concurrently with the mutator
             * (a non-zero threshold starts the background stepper); with
             * auto-collect off the extent is refreshed by gc_collect itself. */
            if (gc_threshold != 0) {
                ((cpyte_obj_t*)(tl->start - sizeof(cpyte_obj_t)))->used =
                    (size_t)(next - tl->start);
            }
            return cur;
        }
    }
    return tlab_refill(size, min_block_size);
}

/* ── Object hash table (conservative scan validation) ───────────── */

#define OBJ_TABLE_BITS 12
#define OBJ_TABLE_SIZE (1 << OBJ_TABLE_BITS)

static cpyte_obj_t* obj_table[OBJ_TABLE_SIZE];
static cpyte_obj_t* obj_list = NULL;  /* all tracked blocks, for containment search */
static cpyte_obj_t* obj_last_lookup = NULL;

/* Page table: (address >> OBJ_PAGE_SHIFT) -> blocks whose payload covers that
 * page.  Blocks never overlap, so a bucket chain is tiny (1-2 entries). */
#define OBJ_PAGE_SHIFT 12
#define OBJ_PAGE_BITS 14
#define OBJ_PAGE_TABLE_SIZE (1u << OBJ_PAGE_BITS)
#define OBJ_PAGE_MASK (OBJ_PAGE_TABLE_SIZE - 1)

static cpyte_page_link_t* obj_page_table[OBJ_PAGE_TABLE_SIZE];
static int obj_page_overflow = 0;

static inline unsigned long hash_page(uintptr_t page) {
    return (unsigned long)(((page >> 3) * 2654435761UL) & OBJ_PAGE_MASK);
}

static void obj_page_link(cpyte_obj_t* obj) {
    uintptr_t begin = (uintptr_t)PAYLOAD(obj);
    size_t first = begin >> OBJ_PAGE_SHIFT;
    size_t last = (begin + obj->size - 1) >> OBJ_PAGE_SHIFT;
    size_t n = last - first + 1;
    cpyte_page_link_t* links = (cpyte_page_link_t*)malloc(n * sizeof(*links));
    obj->pages = links;
    if (links == NULL) {
        /* Fall back to the linear list for every lookup. */
        obj_page_overflow = 1;
        return;
    }
    for (size_t i = 0; i < n; i++) {
        unsigned long h = hash_page(first + i);
        links[i].blk = obj;
        links[i].next = obj_page_table[h];
        obj_page_table[h] = &links[i];
    }
}

static void obj_page_unlink(cpyte_obj_t* obj) {
    cpyte_page_link_t* links = obj->pages;
    if (links == NULL) return;
    uintptr_t begin = (uintptr_t)PAYLOAD(obj);
    size_t first = begin >> OBJ_PAGE_SHIFT;
    size_t n = ((begin + obj->size - 1) >> OBJ_PAGE_SHIFT) - first + 1;
    for (size_t i = 0; i < n; i++) {
        unsigned long h = hash_page(first + i);
        cpyte_page_link_t** pp = &obj_page_table[h];
        while (*pp) {
            if (*pp == &links[i]) {
                *pp = links[i].next;
                break;
            }
            pp = &(*pp)->next;
        }
    }
    free(links);
    obj->pages = NULL;
}

static unsigned long hash_ptr(const void* p) {
    unsigned long v = (unsigned long)p;
    v = (v >> 4) * 2654435761UL;   /* Knuth multiplicative hash */
    return v & (OBJ_TABLE_SIZE - 1);
}

static void obj_track(void* hdr) {
    cpyte_obj_t* obj = (cpyte_obj_t*)hdr;
    unsigned long h = hash_ptr(hdr);
    obj->next = obj_table[h];
    obj_table[h] = obj;
    obj->all = obj_list;
    obj->all_prev = NULL;
    if (obj_list != NULL) obj_list->all_prev = obj;
    obj_list = obj;
    obj_last_lookup = NULL;
    obj_page_link(obj);
}

static void obj_untrack(void* hdr) {
    cpyte_obj_t* obj = (cpyte_obj_t*)hdr;
    unsigned long h = hash_ptr(hdr);
    cpyte_obj_t** pp = &obj_table[h];
    while (*pp) {
        if (*pp == obj) {
            *pp = obj->next;
            /* also remove from the containment list */
            if (obj->all_prev != NULL) obj->all_prev->all = obj->all;
            else obj_list = obj->all;
            if (obj->all != NULL) obj->all->all_prev = obj->all_prev;
            if (obj_last_lookup == obj) obj_last_lookup = NULL;
            obj_page_unlink(obj);
            return;
        }
        pp = &(*pp)->next;
    }
}

static int obj_is_tracked(const void* hdr) {
    unsigned long h = hash_ptr(hdr);
    cpyte_obj_t* e = obj_table[h];
    while (e) {
        if (e == hdr) return 1;
        e = e->next;
    }
    return 0;
}

/* Find the tracked block that contains a caller-visible (payload-interior)
 * pointer. Conservative scans may produce pointers that land anywhere inside
 * an allocation, so an exact header match is not enough. */
static cpyte_obj_t* obj_find_containing(const void* user) {
    uintptr_t u = (uintptr_t)user;
    cpyte_obj_t* e;
    if (user == NULL) return NULL;
    e = obj_last_lookup;
    if (e != NULL) {
        uintptr_t begin = (uintptr_t)PAYLOAD(e);
        uintptr_t end = begin + e->size;
        if (u >= begin && u < end) return e;
    }
    {
        unsigned long h = hash_page(u >> OBJ_PAGE_SHIFT);
        for (cpyte_page_link_t* l = obj_page_table[h]; l; l = l->next) {
            e = l->blk;
            uintptr_t begin = (uintptr_t)PAYLOAD(e);
            if (u >= begin && u < begin + e->size) {
                obj_last_lookup = e;
                return e;
            }
        }
    }
    if (!obj_page_overflow) return NULL;
    /* Rare fallback: a block could not build its page entries. */
    for (e = obj_list; e; e = e->all) {
        if (e->size > 0) {
            uintptr_t begin = (uintptr_t)PAYLOAD(e);
            uintptr_t end = begin + e->size;
            if (u >= begin && u < end) {
                obj_last_lookup = e;
                return e;
            }
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
    /* Only the live part of the stack can hold roots.  The full reserved
     * stack (8MB on macOS, similar elsewhere) is mostly untouched; scanning it
     * for every collection dominated marking time.  Start at this frame
     * instead: the stack grows down, so every live caller frame sits at a
     * higher address and callee frames are gone by the time we get here. */
    volatile char marker = 0;
    char* live_lo = (char*)((uintptr_t)&marker & ~(uintptr_t)(sizeof(void*) - 1));
#if defined(_WIN32)
    /* Windows: use GetCurrentThreadStackLimits (Win 8+). The reserved range
     * also covers UNCOMMITTED pages and the guard page at the committed edge;
     * reading those faults, so only walk MEM_COMMIT pages (skipping the
     * PAGE_GUARD boundary) via VirtualQuery. */
    ULONG_PTR lo = 0, hi = 0;
    GetCurrentThreadStackLimits(&lo, &hi);
    char* p = (char*)lo;
    if (p < live_lo) p = live_lo;
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
    char* lo = (char*)stack_addr - stack_size;
    if (lo < live_lo) lo = live_lo;
    scan_stack_range(lo, stack_addr);
#elif defined(__linux__)
    /* Linux: read pthread_getattr_np for the current thread (no /proc parsing) */
    pthread_attr_t attr;
    if (pthread_getattr_np(pthread_self(), &attr) == 0) {
        void* stack_addr = NULL;
        size_t stack_size = 0;
        pthread_attr_getstack(&attr, &stack_addr, &stack_size);
        pthread_attr_destroy(&attr);
        char* hi = (char*)stack_addr + stack_size;
        char* lo = (char*)stack_addr;
        if (lo < live_lo) lo = live_lo;
        scan_stack_range(lo, hi);
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

    /* Object scan: conservative scan of the payload.  `used` is the number of
     * bytes handed out so far; a TLAB block reserves 128KB (or 1MB for string
     * buffers) but typically only its prefix is live and initialized. */
    cpyte_obj_t* obj = (cpyte_obj_t*)hdr;
    char*   payload = PAYLOAD(obj);
    size_t  size    = obj->used;

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
    memset(obj_page_table, 0, sizeof(obj_page_table));
    obj_page_overflow = 0;
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
#ifndef _WIN32
    /* Cache this (mutator) thread's buffer so allocations skip the pthread
     * key lookup.  The stack range lets the hot path recognize the mutator
     * without a pthread_self() call. */
    gc_record_mutator_stack();
    tlab_main = TLAB_GET();
    if (tlab_main != NULL) {
        tlab_main_tid = pthread_self();
        tlab_main_valid = 1;
    }
    linear_scope_main = linear_scope_get();
    if (linear_scope_main != NULL) {
        linear_scope_main_tid = pthread_self();
        linear_scope_main_valid = 1;
    }
    scope_main = scope_marker_get();
    if (scope_main != NULL) {
        scope_main_tid = pthread_self();
        scope_main_valid = 1;
    }
#endif
}

#if defined(__GNUC__) || defined(__clang__)
#  define CPY_COLD __attribute__((noinline, cold))
#else
#  define CPY_COLD
#endif

/* Large-object / TLAB-refill fallback.  Kept out of gc_malloc so the common
 * bump-allocation path compiles to a leaf function with no register spills. */
static CPY_COLD void* gc_malloc_large(size_t size) {
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
    obj->used = size;
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

void* gc_malloc(size_t size) {
    /* Try TLAB first for small allocations (fast path, lock-free) */
    if (size <= TLAB_MAX_ALLOC) {
        void *ptr = tlab_alloc(size, TLAB_SIZE);
        if (ptr != NULL) {
            if (gc_threshold != 0) {
                gc_alloc_bytes += size;
                if (gc_alloc_bytes >= gc_threshold) {
                    gc_trace("collect-trigger", gc_alloc_bytes);
                    gc_collect();
                }
            }
            return ptr;
        }
    }
    return gc_malloc_large(size);
}

/*
 * Allocate a buffer whose caller initializes every byte. String concatenation
 * uses this path so larger results can share a thread-local arena instead of
 * requiring a tracked GC object and global-lock acquisition per result.
 */
void* gc_malloc_uninit(size_t size) {
    if (size == 0) {
        size = 1;
    }

    if (size <= TLAB_STRING_SIZE) {
        size_t block_size = size <= TLAB_MAX_ALLOC
            ? TLAB_SIZE
            : TLAB_STRING_SIZE;
        void* ptr = tlab_alloc(size, block_size);
        if (ptr != NULL) {
            if (gc_threshold != 0) {
                gc_alloc_bytes += size;
                if (gc_alloc_bytes >= gc_threshold) {
                    gc_collect();
                }
            }
            return ptr;
        }
    }
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
    tlab_t* tl = TLAB_GET();
    if (tl != NULL && tl->start != NULL && tl->current != tl->start) {
        cpyte_obj_t* hdr = (cpyte_obj_t*)((char*)tl->start - sizeof(cpyte_obj_t));
        /* The bump fast path skips this store when auto-collect is off, so
         * bring the scan extent up to date before walking the payload. */
        hdr->used = (size_t)(tl->current - tl->start);
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

    /* clear hash table entries */
    for (int i = 0; i < OBJ_TABLE_SIZE; i++) {
        obj_table[i] = NULL;
    }
    for (unsigned i = 0; i < OBJ_PAGE_TABLE_SIZE; i++) {
        obj_page_table[i] = NULL;
    }
    obj_page_overflow = 0;
    obj_list = NULL;
    obj_last_lookup = NULL;
    /* Reset TLAB for current thread */
    TLAB_GET()->start = NULL;
    TLAB_GET()->end = NULL;
    TLAB_GET()->current = NULL;
}


// GC version of optimized string concat
char *
str_concat_const_gc(const char *left, const char *right, size_t right_len) {
    size_t left_len = strlen(left);
    size_t total_len = left_len + right_len;
    char *result = malloc(total_len + 1);
    if (!result) return NULL;
    memcpy(result, left, left_len);
    memcpy(result + left_len, right, right_len);
    result[total_len] = '\0';
    return result;
}


char *
str_concat_const_both_gc(const char *left, const char *right, size_t left_len, size_t right_len) {
    size_t total_len = left_len + right_len;
    char *result = malloc(total_len + 1);
    if (!result) return NULL;
    memcpy(result, left, left_len);
    memcpy(result + left_len, right, right_len);
    result[total_len] = '\0';
    return result;
}

#define CPY_STRBUF_MAGIC 0x43505953U

typedef struct {
    uint32_t magic;
    uint32_t capacity;
    uint32_t length;
    uint32_t flags;
} cpy_strbuf_t;

char *
cpy_str_grow_gc(char *s, const char *suffix, size_t suffix_len) {
    if (!suffix) suffix = "";
    if (s && ((cpy_strbuf_t*)s - 1)->magic == CPY_STRBUF_MAGIC) {
        cpy_strbuf_t *buf = ((cpy_strbuf_t*)s - 1);
        size_t need = (size_t)buf->length + suffix_len + 1;
        if (need <= (size_t)buf->capacity) {
            memcpy(s + buf->length, suffix, suffix_len);
            buf->length += (uint32_t)suffix_len;
            s[buf->length] = ' ';
            return s;
        }
        size_t new_cap = (size_t)buf->capacity * 2;
        if (new_cap < need + 64) new_cap = need + 64;
        cpy_strbuf_t *new_buf = (cpy_strbuf_t*)malloc(sizeof(cpy_strbuf_t) + new_cap);
        if (!new_buf) return s;
        new_buf->magic = CPY_STRBUF_MAGIC;
        new_buf->capacity = (uint32_t)new_cap;
        new_buf->length = buf->length + (uint32_t)suffix_len;
        new_buf->flags = 0;
        char *new_s = (char*)(new_buf + 1);
        memcpy(new_s, s, buf->length);
        memcpy(new_s + buf->length, suffix, suffix_len);
        new_s[new_buf->length] = ' ';
        return new_s;
    }
    size_t left_len = s ? strlen(s) : 0;
    size_t cap = left_len + suffix_len + 64;
    if (cap < 128) cap = 128;
    cpy_strbuf_t *buf = (cpy_strbuf_t*)malloc(sizeof(cpy_strbuf_t) + cap);
    if (!buf) return s;
    buf->magic = CPY_STRBUF_MAGIC;
    buf->capacity = (uint32_t)cap;
    buf->length = (uint32_t)(left_len + suffix_len);
    buf->flags = 0;
    char *new_s = (char*)(buf + 1);
    if (left_len > 0) memcpy(new_s, s, left_len);
    if (suffix_len > 0) memcpy(new_s + left_len, suffix, suffix_len);
    new_s[buf->length] = ' ';
    return new_s;
}

/* Include addition */
#include "gc_runtime_addition.c"
