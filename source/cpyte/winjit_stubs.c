/* JIT-only symbol stubs for the MinGW/Windows MCJIT path.
 *
 * On this platform, memory/libm/compiler-rt helpers reachable from the merged
 * module (runtime.c/gc_runtime.c/bignum.c + user code) surface in the emitted
 * ELF object as undefined symbols. Some are exported by ucrtbase/msvcrt
 * (resolved by the winjit patcher), but memcpy/memset/memmove/floor (injected
 * via llvm.* intrinsics lowered to libcalls) and the __udivti3/__umodti3
 * compiler-rt (i128 div/mod) have no guaranteed export, so they are defined
 * here and linked straight into the JIT module.
 */

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdarg.h>

/* Fast word-aligned memory routines */

void *memcpy(void *dst, const void *src, size_t n)
{
    unsigned char *d = (unsigned char *)dst;
    const unsigned char *s = (const unsigned char *)src;

    while (n >= sizeof(uint64_t) && (((uintptr_t)d | (uintptr_t)s) % sizeof(uint64_t) == 0))
    {
        *(uint64_t *)d = *(const uint64_t *)s;
        d += sizeof(uint64_t);
        s += sizeof(uint64_t);
        n -= sizeof(uint64_t);
    }

    for (size_t i = 0; i < n; i++)
        d[i] = s[i];

    return dst;
}

void *memmove(void *dst, const void *src, size_t n)
{
    unsigned char *d = (unsigned char *)dst;
    const unsigned char *s = (const unsigned char *)src;

    if (d == s || n == 0)
        return dst;

    if (d < s || d >= s + n)
    {
        return memcpy(dst, src, n);
    }
    else
    {
        size_t i = n;
        while (i >= sizeof(uint64_t) && (((uintptr_t)(d + i) | (uintptr_t)(s + i)) % sizeof(uint64_t) == 0))
        {
            i -= sizeof(uint64_t);
            *(uint64_t *)(d + i) = *(const uint64_t *)(s + i);
        }
        while (i > 0)
        {
            i--;
            d[i] = s[i];
        }
    }
    return dst;
}

void *memset(void *dst, int c, size_t n)
{
    unsigned char *d = (unsigned char *)dst;
    unsigned char uc = (unsigned char)c;

    uint64_t pattern = (uint64_t)uc * 0x0101010101010101ULL;

    while (n >= sizeof(uint64_t) && ((uintptr_t)d % sizeof(uint64_t) == 0))
    {
        *(uint64_t *)d = pattern;
        d += sizeof(uint64_t);
        n -= sizeof(uint64_t);
    }

    for (size_t i = 0; i < n; i++)
        d[i] = uc;

    return dst;
}

/* Zero-export printf-family shims */

struct _iobuf;
typedef struct _iobuf FILE;

extern int __cdecl _vsnprintf(char *_DstBuf, size_t _MaxCount,
                              const char *_Format, va_list _ArgList);
extern size_t __cdecl fwrite(const void *_Str, size_t _Size, size_t _Count,
                             FILE *_File);
extern FILE *__cdecl __acrt_iob_func(unsigned _Ix);

int snprintf(char *b, size_t n, const char *f, ...)
{
    va_list ap;
    va_start(ap, f);
    int r = _vsnprintf(b, n, f, ap);
    va_end(ap);
    return (r < 0) ? 0 : r;
}

int sprintf(char *b, const char *f, ...)
{
    va_list ap;
    va_start(ap, f);
    int r = _vsnprintf(b, (size_t)0x7FFFFFFF, f, ap);
    va_end(ap);
    return (r < 0) ? 0 : r;
}

static int _vfprintf_internal(FILE *ff, const char *f, va_list ap)
{
    char stack_buf[1024];
    va_list ap_copy;
    va_copy(ap_copy, ap);

    int len = _vsnprintf(stack_buf, sizeof(stack_buf), f, ap_copy);
    va_end(ap_copy);

    if (len < 0)
        return 0;

    if ((size_t)len < sizeof(stack_buf))
    {
        fwrite(stack_buf, 1, (size_t)len, ff);
        return len;
    }

    /* Message exceeds stack buffer: dynamically allocate exact size */
    char *heap_buf = (char *)malloc((size_t)len + 1);
    if (!heap_buf)
    {
        fwrite(stack_buf, 1, sizeof(stack_buf) - 1, ff);
        return (int)sizeof(stack_buf) - 1;
    }

    _vsnprintf(heap_buf, (size_t)len + 1, f, ap);
    fwrite(heap_buf, 1, (size_t)len, ff);
    free(heap_buf);

    return len;
}

int printf(const char *f, ...)
{
    va_list ap;
    va_start(ap, f);
    int r = _vfprintf_internal(__acrt_iob_func(1), f, ap);
    va_end(ap);
    return r;
}

int fprintf(FILE *ff, const char *f, ...)
{
    va_list ap;
    va_start(ap, f);
    int r = _vfprintf_internal(ff, f, ap);
    va_end(ap);
    return r;
}

/* Fast floor function using inline assembly / IEEE 754 float math */

extern double fmod(double x, double y);

double floor(double x)
{
    double m = fmod(x, 1.0);
    double r = x - m;
    if (m < 0.0)
        r -= 1.0;
    return r;
}

/* Optimized unsigned 128-bit division and remainder */

static unsigned __int128 divmod128(unsigned __int128 a, unsigned __int128 b,
                                   int want_q)
{
    if (b == 0)
        return 0; /* Division by zero safety guard */
    if (a < b)
        return want_q ? 0 : a;
    if (b == 1)
        return want_q ? a : 0;

    unsigned __int128 q = 0;
    unsigned __int128 r = 0;

    /* Bit scan to skip leading zeroes */
    uint64_t a_hi = (uint64_t)(a >> 64);
    int start_bit = a_hi ? (127 - __builtin_clzll(a_hi)) : (63 - __builtin_clzll((uint64_t)a));

    for (int i = start_bit; i >= 0; i--)
    {
        r = (r << 1) | ((a >> i) & 1);
        if (r >= b)
        {
            r -= b;
            q |= ((unsigned __int128)1 << i);
        }
    }
    return want_q ? q : r;
}

unsigned __int128 __udivti3(unsigned __int128 a, unsigned __int128 b)
{
    return divmod128(a, b, 1);
}

unsigned __int128 __umodti3(unsigned __int128 a, unsigned __int128 b)
{
    return divmod128(a, b, 0);
}
