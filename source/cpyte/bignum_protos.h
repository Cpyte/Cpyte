#ifndef _CPYTE_BIGNUM_PROTOS_H
#define _CPYTE_BIGNUM_PROTOS_H

#include <stdint.h>
#include <gmp.h>
#include <mpfr.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void* BigInt;
typedef struct Cpy_Frac Frac;
typedef struct Cpy_Float Float;

/* Fraction constructors copy their inputs and return reduced, positive-
 * denominator values. Arithmetic returns a new owned value; invalid operands
 * and zero denominators return NULL. Free values with cpy_frac_free and
 * strings from cpy_frac_to_str with free(). cpy_frac_new expects valid live
 * BigInt handles. cpy_frac_cmp returns nonzero on success and writes the
 * ordering (-1/0/1) through comparison; NULL operands or allocation failure
 * return zero without a comparison result. */
Frac* cpy_frac_new(BigInt numerator, BigInt denominator);
Frac* cpy_frac_from_ints(int64_t numerator, int64_t denominator);
void cpy_frac_free(Frac* value);
Frac* cpy_frac_add(const Frac* left, const Frac* right);
Frac* cpy_frac_sub(const Frac* left, const Frac* right);
Frac* cpy_frac_mul(const Frac* left, const Frac* right);
Frac* cpy_frac_div(const Frac* left, const Frac* right);
Frac* cpy_frac_neg(const Frac* value);
int cpy_frac_cmp(const Frac* left, const Frac* right, int* comparison);
char* cpy_frac_to_str(const Frac* value);
void cpy_frac_print(const Frac* value);

/* MPFR-backed float handles are opaque and must be released with the matching
 * destructor after creation. Returned strings are owned by the caller and
 * must be released with free(). */
Float* cpy_create_float(long precision);
Float* cpy_float_from_string(const char* value, long precision);
Float* cpy_float_from_int(int64_t value, long precision);
Float* cpy_float_from_double(double value, long precision);
void cpy_destroy_float(Float* value);
Float* cpy_float_add(const Float* left, const Float* right);
Float* cpy_float_sub(const Float* left, const Float* right);
Float* cpy_float_mul(const Float* left, const Float* right);
Float* cpy_float_div(const Float* left, const Float* right);
Float* cpy_float_mod(const Float* left, const Float* right);
Float* cpy_float_pow(const Float* base, const Float* exponent);
Float* cpy_float_neg(const Float* value);
Float* cpy_float_abs(const Float* value);
Float* cpy_float_sqrt(const Float* value);
Float* cpy_float_floor(const Float* value);
Float* cpy_float_ceil(const Float* value);
Float* cpy_float_trunc(const Float* value);
/* Returns -1/0/1, or 2 if either operand is NaN; NULL sorts below non-NULL. */
int cpy_float_cmp(const Float* left, const Float* right);
double cpy_float_to_double(const Float* value);
char* cpy_float_to_string(const Float* value, int significant_digits);
int cpy_float_is_nan(const Float* value);
int cpy_float_is_infinite(const Float* value);
int cpy_float_is_zero(const Float* value);
/* Finite floats convert to their exact binary rational; non-finite values
 * return NULL. Fraction-to-float rounds to the requested precision. */
Frac* cpy_float_tofrac(const Float* value);
Float* cpy_frac_tofloat(const Frac* value, long precision);

BigInt bigint_from_int(int64_t v);
BigInt bigint_from_uint64(uint64_t v);
BigInt bigint_from_str(const char* s);
BigInt bigint_from_mpz(mpz_srcptr value);
/* Returns 1 on success after rounding to result's precision; 0 on invalid
 * arguments. */
int bigint_to_mpfr(mpfr_ptr result, BigInt value);
/* Return fresh owned values; bigint_gcd is non-negative and treats NULL as 0. */
BigInt bigint_copy(BigInt value);
BigInt bigint_abs(BigInt value);
BigInt bigint_gcd(BigInt left, BigInt right);
void bigint_free(BigInt z);
int bigint_cmp(BigInt a, BigInt b);
BigInt bigint_add(BigInt a, BigInt b);
BigInt bigint_sub(BigInt a, BigInt b);
BigInt bigint_mul(BigInt a, BigInt b);
BigInt bigint_div(BigInt a, BigInt b);
BigInt bigint_floor_div(BigInt a, BigInt b);
BigInt bigint_mod(BigInt a, BigInt b);
BigInt bigint_neg(BigInt a);
BigInt bigint_pow(BigInt a, BigInt b);
void bigint_print(BigInt z);
char* bigint_to_str(BigInt z);
int64_t bigint_to_int64(BigInt z);
uint64_t bigint_to_uint64(BigInt z);

BigInt ubigint_add(BigInt a, BigInt b);
BigInt ubigint_sub(BigInt a, BigInt b);
BigInt ubigint_mul(BigInt a, BigInt b);
BigInt ubigint_div(BigInt a, BigInt b);
BigInt ubigint_mod(BigInt a, BigInt b);
BigInt ubigint_and(BigInt a, BigInt b);
BigInt ubigint_or(BigInt a, BigInt b);
BigInt ubigint_xor(BigInt a, BigInt b);
BigInt ubigint_shl(BigInt a, BigInt b);
BigInt ubigint_shr(BigInt a, BigInt b);
int ubigint_cmp(BigInt a, BigInt b);

#ifdef __cplusplus
}
#endif

#endif
