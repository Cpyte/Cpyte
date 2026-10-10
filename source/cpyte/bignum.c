#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <gmp.h>

// Bignum backed by GNU GMP (mpz_t). Each cpyte `big` value is a heap-allocated
// __mpz_struct returned as an opaque void*. `ubig` reuses the same
// representation (a non-negative mpz), so add/mul/div/mod/pow are shared; the
// ubigint_* functions below are where unsigned semantics genuinely differ
// (underflow-checked subtraction, magnitude comparison, and bitwise ops over
// the full magnitude, which signed `big` deliberately does not support).
//
// The bigint_*/ubigint_* ABI is identical to the former limb-array
// implementation: every arithmetic function returns a freshly allocated value;
// operands are never mutated; NULL operands behave like zero (with NULL-and-x
// addition returning x, matching the original guards).

static void _bn_fail(const char* msg) {
    fputs(msg, stderr);
    fputc('\n', stderr);
    exit(1);
}

// Allocate a fresh, initialized mpz. __mpz_struct is the storage of mpz_t;
// heap-allocating it and returning the pointer keeps the opaque i8* ABI the
// compiler uses for every big/ubig value.
static mpz_ptr _z_alloc(void) {
    __mpz_struct* z = malloc(sizeof(__mpz_struct));
    if (!z) _bn_fail("bigint: alloc failed");
    mpz_init(z);
    return z;
}

static mpz_ptr _z_from_u64(uint64_t u) {
    mpz_ptr z = _z_alloc();
    // Word order -1 (least-significant first), one word of u's 64 bits.
    mpz_import(z, 1, -1, sizeof(u), 0, 0, &u);
    return z;
}

void* bigint_new(void) { return _z_alloc(); }

void bigint_free(void* p) {
    if (!p) return;
    mpz_clear((mpz_ptr)p);
    free(p);
}

void* bigint_from_int(int64_t val) {
    if (val >= 0) return _z_from_u64((uint64_t)val);
    // Magnitude of INT64_MIN is representable in uint64.
    mpz_ptr z = _z_from_u64((uint64_t)(-(val + 1)) + 1);
    mpz_neg(z, z);
    return z;
}

void* bigint_from_uint64(uint64_t val) { return _z_from_u64(val); }

void* bigint_from_str(const char* str) {
    mpz_ptr z = _z_alloc();
    if (!str || str[0] == '\0') {
        mpz_set_ui(z, 0);
        return z;
    }
    if (mpz_set_str(z, str, 10) == -1) mpz_set_ui(z, 0);
    return z;
}

void* bigint_add(void* a, void* b) {
    if (!a || !b) {
        mpz_srcptr src = a ? (mpz_srcptr)a : (mpz_srcptr)b;
        mpz_ptr r = _z_alloc();
        if (src) mpz_set(r, src);
        return r;
    }
    mpz_ptr r = _z_alloc();
    mpz_add(r, (mpz_srcptr)a, (mpz_srcptr)b);
    return r;
}

void* bigint_sub(void* a, void* b) {
    if (!a && !b) return bigint_new();
    if (!a) {
        mpz_ptr r = _z_alloc();
        mpz_neg(r, (mpz_srcptr)b);
        return r;
    }
    if (!b) {
        mpz_ptr r = _z_alloc();
        mpz_set(r, (mpz_srcptr)a);
        return r;
    }
    mpz_ptr r = _z_alloc();
    mpz_sub(r, (mpz_srcptr)a, (mpz_srcptr)b);
    return r;
}

void* bigint_mul(void* a, void* b) {
    if (!a || !b) return bigint_new();
    mpz_ptr r = _z_alloc();
    mpz_mul(r, (mpz_srcptr)a, (mpz_srcptr)b);
    return r;
}

// bigint_div is C-style truncation toward zero (the `%`/`/` semantics); the
// `//` floor-division operator uses bigint_floor_div below.
void* bigint_div(void* a, void* b) {
    if (!a || !b) return bigint_new();
    if (mpz_sgn((mpz_srcptr)b) == 0) return bigint_new();
    mpz_ptr r = _z_alloc();
    mpz_tdiv_q(r, (mpz_srcptr)a, (mpz_srcptr)b);
    return r;
}

void* bigint_floor_div(void* a, void* b) {
    if (!a || !b) return bigint_new();
    if (mpz_sgn((mpz_srcptr)b) == 0) return bigint_new();
    mpz_ptr r = _z_alloc();
    mpz_fdiv_q(r, (mpz_srcptr)a, (mpz_srcptr)b);
    return r;
}

// Magnitude mod: the original returned |a| mod |b| (always non-negative),
// independent of either operand's sign. GMP's tdiv_r/fdiv_r would attach the
// dividend/divisor sign, so strip signs first.
void* bigint_mod(void* a, void* b) {
    if (!a || !b) return bigint_new();
    if (mpz_sgn((mpz_srcptr)b) == 0) return bigint_new();
    mpz_ptr r = _z_alloc();
    mpz_t aa, bb;
    mpz_init(aa);
    mpz_init(bb);
    mpz_abs(aa, (mpz_srcptr)a);
    mpz_abs(bb, (mpz_srcptr)b);
    mpz_tdiv_r(r, aa, bb);
    mpz_clear(aa);
    mpz_clear(bb);
    return r;
}

void* bigint_neg(void* a) {
    if (!a) return bigint_new();
    mpz_ptr r = _z_alloc();
    mpz_neg(r, (mpz_srcptr)a);
    return r;
}

int bigint_cmp(void* a, void* b) {
    mpz_t zero;
    mpz_init(zero);
    mpz_srcptr x = a ? (mpz_srcptr)a : zero;
    mpz_srcptr y = b ? (mpz_srcptr)b : zero;
    int r = mpz_cmp(x, y);
    mpz_clear(zero);
    return r;
}

void bigint_print(void* p) {
    if (!p) {
        printf("(null)\n");
        return;
    }
    char* s = mpz_get_str(NULL, 10, (mpz_srcptr)p);
    if (!s) return;
    printf("%s\n", s);
    free(s);
}

char* bigint_to_str(void* p) {
    if (!p) return (char*)0;
    char* g = mpz_get_str(NULL, 10, (mpz_srcptr)p);
    if (!g) return (char*)0;
    // GMP's output buffer uses the GMP allocator; copy into a plain malloc
    // buffer so callers (str concatenation/print) free() it exactly like the
    // old implementation.
    size_t n = strlen(g);
    char* out = malloc(n + 1);
    if (!out) {
        free(g);
        return (char*)0;
    }
    memcpy(out, g, n + 1);
    free(g);
    return out;
}

// Square-and-multiply over a *big* exponent (GMP has no mpz^mpz for arbitrary
// exponents). The natural sign propagation of mpz_mul reproduces the original:
// negative base with odd exponent -> negative result.
void* bigint_pow(void* a, void* b) {
    if (!a || !b) return bigint_new();
    if (mpz_sgn((mpz_srcptr)b) < 0) return bigint_new();
    mpz_ptr r = _z_alloc();
    mpz_set_ui(r, 1);
    mpz_t e, cb;
    mpz_init_set(e, (mpz_srcptr)b);
    mpz_init_set(cb, (mpz_srcptr)a);
    while (mpz_sgn(e) > 0) {
        if (mpz_tstbit(e, 0)) mpz_mul(r, r, cb);
        mpz_tdiv_q_2exp(e, e, 1);
        mpz_mul(cb, cb, cb);
    }
    mpz_clear(e);
    mpz_clear(cb);
    return r;
}

// --- Unsigned big (ubig) support ---
// add/mul/div/mod/pow on two non-negative operands are mathematically
// identical to the signed versions, so bigint_* is reused for those.

void* ubigint_sub(void* a, void* b) {
    if (!a) return bigint_new();
    if (!b) {
        mpz_ptr r = _z_alloc();
        mpz_set(r, (mpz_srcptr)a);
        return r;
    }
    mpz_ptr r = _z_alloc();
    mpz_sub(r, (mpz_srcptr)a, (mpz_srcptr)b);
    if (mpz_sgn(r) < 0) {
        mpz_clear(r);
        free(r);
        _bn_fail("ubig underflow: result of subtraction is negative");
    }
    return r;
}

int ubigint_cmp(void* a, void* b) { return bigint_cmp(a, b); }

void* ubigint_and(void* a, void* b) {
    if (!a || !b) return bigint_new();
    mpz_ptr r = _z_alloc();
    mpz_and(r, (mpz_srcptr)a, (mpz_srcptr)b);
    return r;
}

void* ubigint_or(void* a, void* b) {
    mpz_srcptr src = a ? (mpz_srcptr)a : (mpz_srcptr)b;
    mpz_ptr r = _z_alloc();
    if (src) mpz_set(r, src);
    if (a && b) mpz_ior(r, (mpz_srcptr)a, (mpz_srcptr)b);
    return r;
}

void* ubigint_xor(void* a, void* b) {
    mpz_srcptr src = a ? (mpz_srcptr)a : (mpz_srcptr)b;
    mpz_ptr r = _z_alloc();
    if (src) mpz_set(r, src);
    if (a && b) mpz_xor(r, (mpz_srcptr)a, (mpz_srcptr)b);
    return r;
}

// Left shift. GMP's mpz_get_ui returns the low 64 bits of the count, exactly
// the low-limb truncation the original used; shifting never underflows.
void* ubigint_shl(void* a, void* count) {
    if (!a) return bigint_new();
    if (!count) {
        mpz_ptr r = _z_alloc();
        mpz_set(r, (mpz_srcptr)a);
        return r;
    }
    unsigned long amt = mpz_get_ui((mpz_srcptr)count);
    mpz_ptr r = _z_alloc();
    mpz_mul_2exp(r, (mpz_srcptr)a, amt);
    return r;
}

// Right shift is a logical shift for a non-negative operand; GMP's
// mpz_fdiv_q_2exp == truncation for non-negative values and yields zero for
// counts >= the bit width, matching the original limb_shift guard.
void* ubigint_shr(void* a, void* count) {
    if (!a) return bigint_new();
    if (!count) {
        mpz_ptr r = _z_alloc();
        mpz_set(r, (mpz_srcptr)a);
        return r;
    }
    unsigned long amt = mpz_get_ui((mpz_srcptr)count);
    mpz_ptr r = _z_alloc();
    mpz_fdiv_q_2exp(r, (mpz_srcptr)a, amt);
    return r;
}