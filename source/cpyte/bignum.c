#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

// --- Minimal bignum implementation ---
// Each bignum is a 64-bit limb array (little-endian), dynamically allocated.

typedef struct {
    uint64_t* limbs;
    size_t    len;
    size_t    used;
    int       negative;
} BigNum;

static void _bn_fail(const char* msg) {
    write(2, msg, strlen(msg));
    write(2, "\n", 1);
    exit(1);
}

static BigNum* _bn_alloc(void) {
    BigNum* b = malloc(sizeof(BigNum));
    if (!b) _bn_fail("bigint: alloc failed");
    b->limbs = NULL;
    b->len = 0;
    b->used = 0;
    b->negative = 0;
    return b;
}

static void _bn_reserve(BigNum* b, size_t n) {
    if (n > b->len) {
        size_t new_len = n < 4 ? 4 : n;
        uint64_t* p = realloc(b->limbs, new_len * sizeof(uint64_t));
        if (!p) _bn_fail("bigint: realloc failed");
        b->limbs = p;
        b->len = new_len;
    }
}

static void _bn_trim(BigNum* b) {
    if (b->used == 0) return;
    while (b->used > 1 && b->limbs[b->used - 1] == 0)
        b->used--;
    if (b->used == 1 && b->limbs[0] == 0)
        b->negative = 0;
}

static void _bn_set_uint64(BigNum* b, uint64_t v) {
    _bn_reserve(b, 1);
    b->limbs[0] = v;
    b->used = 1;
    b->negative = 0;
}

static int _bn_cmp_mag(const BigNum* a, const BigNum* b) {
    if (a->used != b->used) return a->used < b->used ? -1 : 1;
    for (size_t i = a->used; i > 0; i--) {
        size_t idx = i - 1;
        if (a->limbs[idx] != b->limbs[idx])
            return a->limbs[idx] < b->limbs[idx] ? -1 : 1;
    }
    return 0;
}

void* bigint_new(void) {
    BigNum* b = _bn_alloc();
    _bn_reserve(b, 1);
    b->limbs[0] = 0;
    b->used = 1;
    b->negative = 0;
    return b;
}

void bigint_free(void* p) {
    if (!p) return;
    BigNum* b = (BigNum*)p;
    free(b->limbs);
    free(b);
}

void* bigint_from_int(int64_t val) {
    BigNum* b = _bn_alloc();
    if (val < 0) {
        _bn_set_uint64(b, (uint64_t)(-(val + 1)) + 1);
        b->negative = 1;
    } else {
        _bn_set_uint64(b, (uint64_t)val);
    }
    return b;
}

void* bigint_from_uint64(uint64_t val) {
    BigNum* b = _bn_alloc();
    _bn_set_uint64(b, val);
    return b;
}

void* bigint_from_str(const char* str) {
    if (!str || str[0] == '\0') return bigint_from_int(0);
    BigNum* b = _bn_alloc();
    const char* p = str;
    int neg = 0;
    if (*p == '-') { neg = 1; p++; }
    else if (*p == '+') { p++; }

    while (*p == '0') p++;

    size_t digits = strlen(p);
    if (digits == 0 || *p == '\0') {
        _bn_reserve(b, 1);
        b->limbs[0] = 0;
        b->used = 1;
        return b;
    }

    _bn_reserve(b, (digits / 19) + 2);
    b->used = 1;
    b->limbs[0] = 0;

    while (*p) {
        uint64_t carry = 0;
        for (size_t i = 0; i < b->used; i++) {
            __uint128_t v = (__uint128_t)b->limbs[i] * 10 + carry;
            b->limbs[i] = (uint64_t)v;
            carry = (uint64_t)(v >> 64);
        }
        if (carry) {
            _bn_reserve(b, b->used + 1);
            b->limbs[b->used++] = carry;
        }
        uint64_t d = (uint64_t)(*p - '0');
        carry = d;
        for (size_t i = 0; i < b->used && carry; i++) {
            __uint128_t v = (__uint128_t)b->limbs[i] + carry;
            b->limbs[i] = (uint64_t)v;
            carry = (uint64_t)(v >> 64);
        }
        if (carry) {
            _bn_reserve(b, b->used + 1);
            b->limbs[b->used++] = carry;
        }
        p++;
    }

    _bn_trim(b);
    b->negative = neg;
    return b;
}

void* bigint_add(void* a, void* b) {
    BigNum* x = (BigNum*)a;
    if (!x) return b ? bigint_add(b, a) : bigint_from_int(0);
    if (!b) return a;
    BigNum* y = (BigNum*)b;
    BigNum* r = _bn_alloc();

    if (x->negative == y->negative) {
        size_t max_used = x->used > y->used ? x->used : y->used;
        _bn_reserve(r, max_used + 1);
        uint64_t carry = 0;
        size_t i = 0;
        for (; i < x->used && i < y->used; i++) {
            __uint128_t v = (__uint128_t)x->limbs[i] + y->limbs[i] + carry;
            r->limbs[i] = (uint64_t)v;
            carry = (uint64_t)(v >> 64);
        }
        for (; i < x->used; i++) {
            __uint128_t v = (__uint128_t)x->limbs[i] + carry;
            r->limbs[i] = (uint64_t)v;
            carry = (uint64_t)(v >> 64);
        }
        for (; i < y->used; i++) {
            __uint128_t v = (__uint128_t)y->limbs[i] + carry;
            r->limbs[i] = (uint64_t)v;
            carry = (uint64_t)(v >> 64);
        }
        if (carry)
            r->limbs[i++] = carry;
        r->used = i;
        r->negative = x->negative;
    } else {
        int cmp = _bn_cmp_mag(x, y);
        const BigNum* bigger = (cmp >= 0) ? x : y;
        const BigNum* smaller = (cmp >= 0) ? y : x;
        _bn_reserve(r, bigger->used);
        uint64_t borrow = 0;
        size_t i = 0;
        for (; i < smaller->used; i++) {
            __uint128_t v = (__uint128_t)bigger->limbs[i] - smaller->limbs[i] - borrow;
            r->limbs[i] = (uint64_t)v;
            borrow = (uint64_t)((v >> 64) & 1);
        }
        for (; i < bigger->used; i++) {
            __uint128_t v = (__uint128_t)bigger->limbs[i] - borrow;
            r->limbs[i] = (uint64_t)v;
            borrow = (uint64_t)((v >> 64) & 1);
        }
        r->used = bigger->used;
        _bn_trim(r);
        r->negative = (bigger == x) ? x->negative : y->negative;
    }
    return r;
}

void* bigint_neg(void* a) {
    if (!a) return bigint_from_int(0);
    BigNum* x = (BigNum*)a;
    BigNum* r = _bn_alloc();
    _bn_reserve(r, x->used);
    memcpy(r->limbs, x->limbs, x->used * sizeof(uint64_t));
    r->used = x->used;
    r->negative = !x->negative;
    if (r->used == 1 && r->limbs[0] == 0) r->negative = 0;
    return r;
}

void* bigint_sub(void* a, void* b) {
    if (!a) return b ? bigint_neg(b) : bigint_from_int(0);
    if (!b) return a;
    BigNum* y = (BigNum*)b;
    int orig_neg = y->negative;
    y->negative = !y->negative;
    void* r = bigint_add(a, b);
    y->negative = orig_neg;
    return r;
}

void* bigint_mul(void* a, void* b) {
    if (!a || !b) return bigint_from_int(0);
    BigNum* x = (BigNum*)a;
    BigNum* y = (BigNum*)b;
    BigNum* r = _bn_alloc();

    size_t n = x->used + y->used;
    _bn_reserve(r, n);
    memset(r->limbs, 0, n * sizeof(uint64_t));
    r->used = n;

    for (size_t i = 0; i < x->used; i++) {
        uint64_t carry = 0;
        for (size_t j = 0; j < y->used; j++) {
            __uint128_t v = (__uint128_t)x->limbs[i] * y->limbs[j]
                          + r->limbs[i + j] + carry;
            r->limbs[i + j] = (uint64_t)v;
            carry = (uint64_t)(v >> 64);
        }
        if (carry)
            r->limbs[i + y->used] += carry;
    }
    _bn_trim(r);
    r->negative = x->negative ^ y->negative;
    if (r->used == 1 && r->limbs[0] == 0) r->negative = 0;
    return r;
}

void* bigint_div(void* a, void* b) {
    if (!a || !b) return bigint_from_int(0);
    BigNum* x = (BigNum*)a;
    BigNum* y = (BigNum*)b;
    if (y->used == 1 && y->limbs[0] == 0)
        return bigint_from_int(0);
    BigNum* r = _bn_alloc();

    if (_bn_cmp_mag(y, x) > 0) {
        _bn_reserve(r, 1);
        r->limbs[0] = 0;
        r->used = 1;
        r->negative = 0;
        return r;
    }

    // Schoolbook division for single-limb divisor (fast path)
    if (y->used == 1) {
        uint64_t div = y->limbs[0];
        _bn_reserve(r, x->used);
        r->used = x->used;
        uint64_t rem = 0;
        for (size_t i = x->used; i > 0; i--) {
            __uint128_t v = ((__uint128_t)rem << 64) | x->limbs[i - 1];
            r->limbs[i - 1] = (uint64_t)(v / div);
            rem = (uint64_t)(v % div);
        }
        _bn_trim(r);
        r->negative = x->negative ^ y->negative;
        if (r->used == 1 && r->limbs[0] == 0) r->negative = 0;
        return r;
    }

    // General case: long division (Knuth Algorithm D)
    // Normalize divisor by shifting
    int norm_shift = __builtin_clzll(y->limbs[y->used - 1]);
    BigNum* u = _bn_alloc(); // normalized dividend
    BigNum* v = _bn_alloc(); // normalized divisor
    _bn_reserve(u, x->used + 1);
    _bn_reserve(v, y->used);

    if (norm_shift > 0) {
        uint64_t carry = 0;
        for (size_t i = 0; i < y->used; i++) {
            uint64_t val = (carry) | (y->limbs[i] << norm_shift);
            v->limbs[i] = val;
            carry = y->limbs[i] >> (64 - norm_shift);
        }
        v->used = y->used;
        
        carry = 0;
        for (size_t i = 0; i < x->used; i++) {
            uint64_t val = (carry) | (x->limbs[i] << norm_shift);
            u->limbs[i] = val;
            carry = x->limbs[i] >> (64 - norm_shift);
        }
        u->limbs[x->used] = carry;
        u->used = x->used + (carry ? 1 : 0);
    } else {
        memcpy(v->limbs, y->limbs, y->used * sizeof(uint64_t));
        v->used = y->used;
        memcpy(u->limbs, x->limbs, x->used * sizeof(uint64_t));
        u->used = x->used;
    }

    size_t q_len = u->used - v->used + 1;
    _bn_reserve(r, q_len);
    memset(r->limbs, 0, q_len * sizeof(uint64_t));
    r->used = q_len;

    uint64_t v_top = v->limbs[v->used - 1];
    
    for (size_t j = q_len; j > 0; j--) {
        size_t idx = j - 1;
        size_t u_idx = idx + v->used;

        // Estimate quotient digit
        __uint128_t u_hi;
        if (u_idx >= u->used) {
            if (u->used > 0) {
                u_hi = ((__uint128_t)u->limbs[u->used - 1] << 64) |
                       (u->used > 1 ? u->limbs[u->used - 2] : 0);
            } else {
                u_hi = 0;
            }
        } else {
            u_hi = ((__uint128_t)u->limbs[u_idx] << 64) |
                    (u_idx > 0 ? u->limbs[u_idx - 1] : 0);
        }
        __uint128_t q_est = u_hi / v_top;
        if (q_est > 0xFFFFFFFFFFFFFFFFULL)
            q_est = 0xFFFFFFFFFFFFFFFFULL;

        // Subtract q_est * v from u
        uint64_t borrow = 0;
        for (size_t i = 0; i < v->used; i++) {
            __uint128_t prod = q_est * v->limbs[i];
            __uint128_t sub = (__uint128_t)u->limbs[idx + i] - (uint64_t)prod - borrow;
            u->limbs[idx + i] = (uint64_t)sub;
            borrow = (uint64_t)(prod >> 64) - (uint64_t)(sub >> 64);
        }
        __uint128_t final_sub = (__uint128_t)u->limbs[u_idx] - borrow;
        u->limbs[u_idx] = (uint64_t)final_sub;

        if ((uint64_t)(final_sub >> 64) != 0) {
            // Quotient was too large, add back
            q_est--;
            uint64_t carry = 0;
            for (size_t i = 0; i < v->used; i++) {
                __uint128_t sum = (__uint128_t)u->limbs[idx + i] + v->limbs[i] + carry;
                u->limbs[idx + i] = (uint64_t)sum;
                carry = (uint64_t)(sum >> 64);
            }
            u->limbs[u_idx] += carry;
        }

        r->limbs[idx] = (uint64_t)q_est;
    }

    _bn_trim(r);
    bigint_free(u);
    bigint_free(v);

    r->negative = x->negative ^ y->negative;
    if (r->used == 1 && r->limbs[0] == 0) r->negative = 0;
    return r;
}

void* bigint_mod(void* a, void* b) {
    if (!a || !b) return bigint_from_int(0);
    BigNum* x = (BigNum*)a;
    void* q = bigint_div(a, b);
    BigNum* qt = (BigNum*)q;
    void* prod = bigint_mul(q, b);
    BigNum* pt = (BigNum*)prod;
    pt->negative = 0;
    qt->negative = 0;

    BigNum* r = _bn_alloc();
    int cmp = _bn_cmp_mag(x, pt);
    const BigNum* bigger = (cmp >= 0) ? (BigNum*)a : pt;
    const BigNum* smaller = (cmp >= 0) ? pt : (BigNum*)a;
    _bn_reserve(r, bigger->used);
    uint64_t borrow = 0;
    size_t i = 0;
    for (; i < smaller->used; i++) {
        __uint128_t v = (__uint128_t)bigger->limbs[i] - smaller->limbs[i] - borrow;
        r->limbs[i] = (uint64_t)v;
        borrow = (uint64_t)((v >> 64) & 1);
    }
    for (; i < bigger->used; i++) {
        __uint128_t v = (__uint128_t)bigger->limbs[i] - borrow;
        r->limbs[i] = (uint64_t)v;
        borrow = (uint64_t)((v >> 64) & 1);
    }
    r->used = bigger->used;
    _bn_trim(r);
    r->negative = 0;

    bigint_free(q);
    bigint_free(prod);
    return r;
}

int bigint_cmp(void* a, void* b) {
    BigNum* x = (BigNum*)a;
    BigNum* y = (BigNum*)b;
    if (!x && !y) return 0;
    if (!x) return y->negative ? 1 : -1;
    if (!y) return x->negative ? -1 : 1;
    if (x->negative != y->negative)
        return x->negative ? -1 : 1;
    int cmp = _bn_cmp_mag(x, y);
    return x->negative ? -cmp : cmp;
}

void bigint_print(void* p) {
    BigNum* b = (BigNum*)p;
    if (!b) { printf("(null)"); return; }
    if (b->negative) putchar('-');
    if (b->used == 1 && b->limbs[0] == 0) {
        putchar('0');
        return;
    }
    size_t max_digits = b->used * 20 + 1;
    char* buf = malloc(max_digits);
    if (!buf) _bn_fail("bigint_print: alloc failed");

    BigNum tmp;
    tmp.limbs = malloc(b->used * sizeof(uint64_t));
    memcpy(tmp.limbs, b->limbs, b->used * sizeof(uint64_t));
    tmp.used = b->used;
    tmp.len = b->used;
    tmp.negative = 0;

    size_t pos = max_digits;
    buf[--pos] = '\0';
    while (!(tmp.used == 1 && tmp.limbs[0] == 0)) {
        uint64_t carry = 0;
        for (size_t i = tmp.used; i > 0; i--) {
            __uint128_t v = (__uint128_t)carry << 64 | tmp.limbs[i - 1];
            tmp.limbs[i - 1] = (uint64_t)(v / 10);
            carry = (uint64_t)(v % 10);
        }
        buf[--pos] = (char)('0' + carry);
        _bn_trim(&tmp);
    }
    free(tmp.limbs);
    printf("%s", buf + pos);
    free(buf);
}
