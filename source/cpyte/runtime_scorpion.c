/*
 * runtime_scorpion.c — Cpyte runtime for Scorpion (RV32 bare-metal)
 *
 * Implements print/input/malloc via Scorpion syscalls and a bump allocator.
 * Compiled with riscv32-unknown-elf-gcc for the RP2350 RISC-V target.
 *
 * Scorpion syscall ABI (see abi/scorpion.h):
 *   a7 = syscall number
 *   a0-a3 = arguments
 *   ecall
 *   return value in a0
 */

/* stdint.h is a compiler-provided freestanding header, so it needs no libc. */
#include <stdint.h>

/* ── Bump allocator (no free) ────────────────────────────────────── */

#define HEAP_SIZE (64 * 1024)
static char heap[HEAP_SIZE];
static volatile unsigned long heap_top;

void *malloc(unsigned long size) {
    unsigned long old;
    unsigned long align = 8;
    unsigned long req = (size + align - 1) & ~(align - 1);

    /* simple bump — no lock needed on single-core */
    if (heap_top + req > HEAP_SIZE) return (void*)0;

    old = heap_top;
    heap_top += req;
    return (void*)(heap + old);
}

void free(void *p) {
    (void)p;  /* no-op */
}

void *calloc(unsigned long count, unsigned long size) {
    unsigned long total = count * size;
    void *p = malloc(total);
    if (p) {
        for (unsigned long i = 0; i < total; i++)
            ((char*)p)[i] = 0;
    }
    return p;
}

void *realloc(void *p, unsigned long size) {
    (void)p;
    (void)size;
    return (void*)0;  /* not supported */
}

/* ── String ops ──────────────────────────────────────────────────── */

unsigned long strlen(const char *s) {
    unsigned long n = 0;
    while (s[n]) n++;
    return n;
}

int strcmp(const char *a, const char *b) {
    while (*a && *a == *b) { a++; b++; }
    return (unsigned char)*a - (unsigned char)*b;
}

/* ── Syscall wrappers (inline asm for RV32) ──────────────────────── */

/*
 * The full Scorpion syscall ABI, mirroring WEW-scorpion/abi/scorpion.h.
 *
 * The userland header guards its wrappers behind __xtensa__ because it binds
 * arguments to `a0`..`a7` by name — but those are also the RISC-V argument
 * registers, so the identical encoding works here on the RV32 target. These
 * are the non-static definitions the cpyte `#scorpion` ABI lowers calls to:
 * every `scorpion_*` symbol below must stay externally visible.
 *
 * Every wrapper funnels through sc_syscall() so there is exactly one ecall
 * encoding in the runtime. It takes uintptr_t for all four argument registers
 * because arguments are mixed: several syscalls take a user pointer (buf, a
 * name, a SEF image, an out-ScorpionLibInfo*) alongside plain integers. Using
 * uintptr_t rather than unsigned keeps a pointer intact if this is ever built
 * for a 64-bit Scorpion instead of today's RV32 (`-mabi=ilp32`); an `unsigned`
 * register variable would silently truncate a pointer on RV64. The kernel
 * reads a0..a3 as machine words either way.
 */

#define SYS_YIELD       0
#define SYS_EXIT        1
#define SYS_BLOCK       2
#define SYS_WAKE        3
#define SYS_SLEEP       4
#define SYS_SEND        5
#define SYS_RECV        6
#define SYS_OPEN        7
#define SYS_READ        8
#define SYS_WRITE       9
#define SYS_CLOSE      10
#define SYS_PUTC       11
#define SYS_SPAWN      12
#define SYS_TERMINATE  13
#define SYS_LOADLIB    14
#define SYS_UNLOADLIB  15
#define SYS_DLSYM      16
#define SYS_LIBINFO    17

/* Loader return codes (abi/scorpion.h). */
#define SCORPION_LOAD_GLOBAL 0x1u
#define SCORPION_LOAD_LOCAL  0x2u
#define SCORPION_VER_ANY 0u

/*
 * Layout of ScorpionLibInfo as the kernel writes it through scorpion_lib_info():
 * seven uint32s then two uint16s, 32 bytes with no padding. The cpyte side
 * builds the same layout (uint32/uint16 lower to i32/i16), so the two agree.
 */
typedef struct {
    unsigned int  base;
    unsigned int  plt_base;
    unsigned int  plt_size;
    unsigned int  export_count;
    unsigned int  import_count;
    unsigned int  ver_count;
    unsigned int  bound_count;
    unsigned short refcount;
    unsigned short flags;
} ScorpionLibInfo;

/*
 * The single ecall encoding: a7 = syscall number, arguments in a0..a3,
 * result in a0. Every scorpion_* wrapper below is a one-liner over this.
 */
static uintptr_t sc_syscall(uintptr_t num, uintptr_t p0, uintptr_t p1,
                            uintptr_t p2, uintptr_t p3) {
    register uintptr_t a0 asm("a0") = p0;
    register uintptr_t a1 asm("a1") = p1;
    register uintptr_t a2 asm("a2") = p2;
    register uintptr_t a3 asm("a3") = p3;
    register uintptr_t a7 asm("a7") = num;
    __asm__ volatile ("ecall" : "+r"(a0) : "r"(a1), "r"(a2), "r"(a3), "r"(a7) : "memory");
    return a0;
}

void scorpion_yield(void) {
    (void)sc_syscall(SYS_YIELD, 0, 0, 0, 0);
}

void scorpion_exit(void) {
    (void)sc_syscall(SYS_EXIT, 0, 0, 0, 0);
    for (;;) {}
}

void scorpion_block(void) {
    (void)sc_syscall(SYS_BLOCK, 0, 0, 0, 0);
}

void scorpion_sleep(unsigned ticks) {
    (void)sc_syscall(SYS_SLEEP, ticks, 0, 0, 0);
}

void scorpion_wake(unsigned pid) {
    (void)sc_syscall(SYS_WAKE, pid, 0, 0, 0);
}

int scorpion_send(unsigned pid, unsigned type, const void *data, unsigned len) {
    return (int)sc_syscall(SYS_SEND, pid, type, (uintptr_t)data, len);
}

int scorpion_recv(unsigned *type, void *buf, unsigned len, unsigned *sender_pid) {
    return (int)sc_syscall(SYS_RECV, (uintptr_t)type, (uintptr_t)buf, len,
                           (uintptr_t)sender_pid);
}

void scorpion_putc(const char *s, unsigned len) {
    (void)sc_syscall(SYS_PUTC, (uintptr_t)s, len, 0, 0);
}

int scorpion_open(const char *name, unsigned mode) {
    return (int)sc_syscall(SYS_OPEN, (uintptr_t)name, mode, 0, 0);
}

int scorpion_read(int fd, void *buf, unsigned size) {
    return (int)sc_syscall(SYS_READ, (uintptr_t)fd, (uintptr_t)buf, size, 0);
}

int scorpion_write(int fd, const void *buf, unsigned size) {
    return (int)sc_syscall(SYS_WRITE, (uintptr_t)fd, (uintptr_t)buf, size, 0);
}

int scorpion_close(int fd) {
    return (int)sc_syscall(SYS_CLOSE, (uintptr_t)fd, 0, 0, 0);
}

int scorpion_spawn(const void *sef_data, unsigned size, unsigned priority) {
    return (int)sc_syscall(SYS_SPAWN, (uintptr_t)sef_data, size, priority, 0);
}

int scorpion_terminate(unsigned pid) {
    return (int)sc_syscall(SYS_TERMINATE, pid, 0, 0, 0);
}

int scorpion_loadlib(const void *sef_data, unsigned size, unsigned flags) {
    return (int)sc_syscall(SYS_LOADLIB, (uintptr_t)sef_data, size, flags, 0);
}

int scorpion_loadlib_global(const void *sef_data, unsigned size) {
    return scorpion_loadlib(sef_data, size, SCORPION_LOAD_GLOBAL);
}

int scorpion_loadlib_local(const void *sef_data, unsigned size) {
    return scorpion_loadlib(sef_data, size, SCORPION_LOAD_LOCAL);
}

int scorpion_unloadlib(unsigned handle, unsigned flags) {
    return (int)sc_syscall(SYS_UNLOADLIB, handle, flags, 0, 0);
}

void *scorpion_sym(const char *name, unsigned name_len) {
    /* a2 = 0 asks the loader for any version. */
    return (void *)sc_syscall(SYS_DLSYM, (uintptr_t)name, name_len, 0, 0);
}

/* FNV-1a over the version string: the value scorpion_sym_ver() asks the
 * loader to match against a version's recorded hash. */
int scorpion_ver_hash(const char *v, unsigned len) {
    unsigned h = 2166136261u;
    for (unsigned i = 0; i < len; i++) {
        h ^= (unsigned char)v[i];
        h *= 16777619u;
    }
    return (int)h;
}

void *scorpion_sym_ver(const char *name, unsigned name_len,
                       const char *version, unsigned version_len) {
    uintptr_t hash = (version == 0 || version_len == 0)
                         ? (uintptr_t)SCORPION_VER_ANY
                         : (uintptr_t)(unsigned)scorpion_ver_hash(version, version_len);
    return (void *)sc_syscall(SYS_DLSYM, (uintptr_t)name, name_len, hash, 0);
}

int scorpion_lib_info(unsigned handle, unsigned global, ScorpionLibInfo *out) {
    return (int)sc_syscall(SYS_LIBINFO, handle, global, (uintptr_t)out, 0);
}

/* ── Integer-to-string conversion ────────────────────────────────── */

static char *int_to_str(int n, char *buf) {
    unsigned long len = 0;
    unsigned u;
    if (n < 0) {
        buf[len++] = '-';
        u = (unsigned)(-(n + 1)) + 1;
    } else {
        u = (unsigned)n;
    }

    /* generate digits in reverse */
    char tmp[12];
    int i = 0;
    if (u == 0) tmp[i++] = '0';
    while (u > 0) {
        tmp[i++] = '0' + (u % 10);
        u /= 10;
    }
    while (i > 0) buf[len++] = tmp[--i];
    buf[len] = '\0';
    return buf;
}

/* ── Public API (called from generated code) ─────────────────────── */

void print_int(int n) {
    char buf[32];
    int_to_str(n, buf);
    scorpion_putc(buf, strlen(buf));
}

void print_int64(long long n) {
    char buf[64];
    unsigned long long u;
    unsigned long len = 0;
    if (n < 0) {
        buf[len++] = '-';
        u = (unsigned long long)(-(n + 1)) + 1;
    } else {
        u = (unsigned long long)n;
    }
    char tmp[24];
    int i = 0;
    if (u == 0) tmp[i++] = '0';
    while (u > 0) {
        tmp[i++] = '0' + (u % 10);
        u /= 10;
    }
    while (i > 0) buf[len++] = tmp[--i];
    buf[len] = '\0';
    scorpion_putc(buf, len);
}

void print_uint64(unsigned long long n) {
    char buf[64];
    unsigned long len = 0;
    char tmp[24];
    int i = 0;
    if (n == 0) tmp[i++] = '0';
    while (n > 0) {
        tmp[i++] = '0' + (n % 10);
        n /= 10;
    }
    while (i > 0) buf[len++] = tmp[--i];
    buf[len] = '\0';
    scorpion_putc(buf, len);
}

void print_double(double d) {
    /* simple: print integer part only */
    int n = (int)d;
    print_int(n);
}

void print_hex(long long n) {
    char buf[64];
    unsigned long len = 0;
    unsigned long long u = (unsigned long long)n;
    const char *hex = "0123456789abcdef";
    buf[len++] = '0';
    buf[len++] = 'x';
    char tmp[20];
    int i = 0;
    if (u == 0) tmp[i++] = '0';
    while (u > 0) {
        tmp[i++] = hex[u & 0xf];
        u >>= 4;
    }
    while (i > 0) buf[len++] = tmp[--i];
    buf[len] = '\0';
    scorpion_putc(buf, len);
}

void print_str(const char *s) {
    if (!s) {
        scorpion_putc("(null)", 6);
        return;
    }
    scorpion_putc(s, strlen(s));
}

static char *ull_to_str_alloc(unsigned long long u, int neg) {
    char tmp[24];
    int i = 0;
    if (u == 0) tmp[i++] = '0';
    while (u > 0) {
        tmp[i++] = '0' + (u % 10);
        u /= 10;
    }
    char *buf = (char*)malloc(i + (neg ? 1 : 0) + 1);
    if (!buf) return (char*)0;
    int len = 0;
    if (neg) buf[len++] = '-';
    while (i > 0) buf[len++] = tmp[--i];
    buf[len] = '\0';
    return buf;
}

char *str_of_int64(long long n) {
    unsigned long long u;
    int neg = 0;
    if (n < 0) {
        neg = 1;
        u = (unsigned long long)(-(n + 1)) + 1;
    } else {
        u = (unsigned long long)n;
    }
    return ull_to_str_alloc(u, neg);
}

char *str_of_uint64(unsigned long long n) {
    return ull_to_str_alloc(n, 0);
}

char *str_of_ptr(long long n) {
    unsigned long long u = (unsigned long long)n;
    const char *hex = "0123456789abcdef";
    char tmp[20];
    int i = 0;
    if (u == 0) tmp[i++] = '0';
    while (u > 0) {
        tmp[i++] = hex[u & 0xf];
        u >>= 4;
    }
    char *buf = (char*)malloc(i + 3);
    if (!buf) return (char*)0;
    int len = 0;
    buf[len++] = '0';
    buf[len++] = 'x';
    while (i > 0) buf[len++] = tmp[--i];
    buf[len] = '\0';
    return buf;
}

char *str_of_double(double d) {
    /* integer part only, matching print_double */
    int n = (int)d;
    return ull_to_str_alloc((unsigned long long)(n < 0 ? -(long long)n : n), n < 0);
}

int input_int(void) {
    /* scorpion stdin via SYS_READ from console fd
       For now, return 0 as a stub. */
    (void)scorpion_read;
    return 0;
}

/* memcpy used by generated code for string concat */
void *memcpy(void *dst, const void *src, unsigned long n) {
    char *d = (char*)dst;
    const char *s = (const char*)src;
    for (unsigned long i = 0; i < n; i++) d[i] = s[i];
    return dst;
}

void *memset(void *dst, int c, unsigned long n) {
    char *d = (char*)dst;
    for (unsigned long i = 0; i < n; i++) d[i] = (char)c;
    return dst;
}
