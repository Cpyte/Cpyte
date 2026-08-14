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

#define SYS_PUTC  11
#define SYS_READ   8
#define SYS_EXIT   1

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

static void scorpion_putc(const char *s, unsigned len) {
    register const char *a0 asm("a0") = s;
    register unsigned a1 asm("a1") = len;
    register unsigned a7 asm("a7") = SYS_PUTC;
    __asm__ volatile ("ecall" : : "r"(a0), "r"(a1), "r"(a7) : "memory");
}

static int scorpion_read(int fd, void *buf, unsigned size) {
    register int a0 asm("a0") = fd;
    register void *a1 asm("a1") = buf;
    register unsigned a2 asm("a2") = size;
    register unsigned a7 asm("a7") = SYS_READ;
    __asm__ volatile ("ecall" : "+r"(a0) : "r"(a1), "r"(a2), "r"(a7) : "memory");
    return a0;
}

static void scorpion_exit(void) {
    register unsigned a7 asm("a7") = SYS_EXIT;
    __asm__ volatile ("ecall" : : "r"(a7) : "memory");
    for (;;) {}
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
