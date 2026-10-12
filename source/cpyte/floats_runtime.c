#include "bignum_protos.h"
#include <mpfr.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <math.h>

typedef struct Cpy_Float {
    mpfr_t value;
    mpfr_prec_t precision;
} Float;

typedef struct Cpy_Frac {
    BigInt numerator;
    BigInt denominator;
} Frac;

static Frac* cpy_frac_from_owned(BigInt numerator, BigInt denominator) {
    if (numerator == NULL || denominator == NULL) {
        bigint_free(numerator);
        bigint_free(denominator);
        return NULL;
    }

    BigInt zero = bigint_from_int(0);
    if (zero == NULL || bigint_cmp(denominator, zero) == 0) {
        bigint_free(zero);
        bigint_free(numerator);
        bigint_free(denominator);
        return NULL;
    }
    int denominator_sign = bigint_cmp(denominator, zero);
    bigint_free(zero);
    if (denominator_sign < 0) {
        BigInt positive_numerator = bigint_neg(numerator);
        BigInt positive_denominator = bigint_neg(denominator);
        bigint_free(numerator);
        bigint_free(denominator);
        numerator = positive_numerator;
        denominator = positive_denominator;
        if (numerator == NULL || denominator == NULL) {
            bigint_free(numerator);
            bigint_free(denominator);
            return NULL;
        }
    }

    zero = bigint_from_int(0);
    if (zero == NULL) {
        bigint_free(numerator);
        bigint_free(denominator);
        return NULL;
    }
    int numerator_is_zero = bigint_cmp(numerator, zero) == 0;
    bigint_free(zero);
    if (numerator_is_zero) {
        BigInt one = bigint_from_int(1);
        if (one == NULL) {
            bigint_free(numerator);
            bigint_free(denominator);
            return NULL;
        }
        bigint_free(denominator);
        denominator = one;
    } else {
        BigInt gcd = bigint_gcd(numerator, denominator);
        if (gcd == NULL) {
            bigint_free(numerator);
            bigint_free(denominator);
            return NULL;
        }
        BigInt reduced_numerator = bigint_div(numerator, gcd);
        BigInt reduced_denominator = bigint_div(denominator, gcd);
        bigint_free(gcd);
        bigint_free(numerator);
        bigint_free(denominator);
        numerator = reduced_numerator;
        denominator = reduced_denominator;
        if (numerator == NULL || denominator == NULL) {
            bigint_free(numerator);
            bigint_free(denominator);
            return NULL;
        }
    }

    Frac *frac = malloc(sizeof(*frac));
    if (frac == NULL) {
        bigint_free(numerator);
        bigint_free(denominator);
        return NULL;
    }
    frac->numerator = numerator;
    frac->denominator = denominator;
    return frac;
}

Frac* cpy_frac_new(BigInt numerator, BigInt denominator) {
    if (numerator == NULL || denominator == NULL) {
        return NULL;
    }
    BigInt numerator_copy = bigint_copy(numerator);
    BigInt denominator_copy = bigint_copy(denominator);
    return cpy_frac_from_owned(numerator_copy, denominator_copy);
}

Frac* cpy_frac_from_ints(int64_t numerator, int64_t denominator) {
    return cpy_frac_from_owned(
        bigint_from_int(numerator), bigint_from_int(denominator));
}

void cpy_frac_free(Frac *frac) {
    if (frac == NULL) {
        return;
    }
    bigint_free(frac->numerator);
    bigint_free(frac->denominator);
    free(frac);
}

Frac* cpy_frac_add(const Frac *left, const Frac *right) {
    if (left == NULL || right == NULL) {
        return NULL;
    }
    BigInt left_product = bigint_mul(left->numerator, right->denominator);
    if (left_product == NULL) {
        return NULL;
    }
    BigInt right_product = bigint_mul(right->numerator, left->denominator);
    if (right_product == NULL) {
        bigint_free(left_product);
        return NULL;
    }
    BigInt numerator = bigint_add(left_product, right_product);
    bigint_free(left_product);
    bigint_free(right_product);
    if (numerator == NULL) {
        return NULL;
    }
    BigInt denominator = bigint_mul(left->denominator, right->denominator);
    if (denominator == NULL) {
        bigint_free(numerator);
        return NULL;
    }
    return cpy_frac_from_owned(numerator, denominator);
}

Frac* cpy_frac_sub(const Frac *left, const Frac *right) {
    if (left == NULL || right == NULL) {
        return NULL;
    }
    BigInt left_product = bigint_mul(left->numerator, right->denominator);
    if (left_product == NULL) {
        return NULL;
    }
    BigInt right_product = bigint_mul(right->numerator, left->denominator);
    if (right_product == NULL) {
        bigint_free(left_product);
        return NULL;
    }
    BigInt numerator = bigint_sub(left_product, right_product);
    bigint_free(left_product);
    bigint_free(right_product);
    if (numerator == NULL) {
        return NULL;
    }
    BigInt denominator = bigint_mul(left->denominator, right->denominator);
    if (denominator == NULL) {
        bigint_free(numerator);
        return NULL;
    }
    return cpy_frac_from_owned(numerator, denominator);
}

Frac* cpy_frac_mul(const Frac *left, const Frac *right) {
    if (left == NULL || right == NULL) {
        return NULL;
    }
    BigInt numerator = bigint_mul(left->numerator, right->numerator);
    if (numerator == NULL) {
        return NULL;
    }
    BigInt denominator = bigint_mul(left->denominator, right->denominator);
    if (denominator == NULL) {
        bigint_free(numerator);
        return NULL;
    }
    return cpy_frac_from_owned(numerator, denominator);
}

Frac* cpy_frac_div(const Frac *left, const Frac *right) {
    if (left == NULL || right == NULL) {
        return NULL;
    }
    BigInt numerator = bigint_mul(left->numerator, right->denominator);
    if (numerator == NULL) {
        return NULL;
    }
    BigInt denominator = bigint_mul(left->denominator, right->numerator);
    if (denominator == NULL) {
        bigint_free(numerator);
        return NULL;
    }
    return cpy_frac_from_owned(numerator, denominator);
}

Frac* cpy_frac_neg(const Frac *value) {
    if (value == NULL) {
        return NULL;
    }
    return cpy_frac_from_owned(
        bigint_neg(value->numerator), bigint_copy(value->denominator));
}

int cpy_frac_cmp(const Frac *left, const Frac *right, int *comparison) {
    if (comparison == NULL) {
        return 0;
    }
    *comparison = 0;
    if (left == NULL || right == NULL) {
        return 0;
    }
    BigInt left_scaled = bigint_mul(left->numerator, right->denominator);
    if (left_scaled == NULL) {
        return 0;
    }
    BigInt right_scaled = bigint_mul(right->numerator, left->denominator);
    if (right_scaled == NULL) {
        bigint_free(left_scaled);
        return 0;
    }
    *comparison = bigint_cmp(left_scaled, right_scaled);
    bigint_free(left_scaled);
    bigint_free(right_scaled);
    return 1;
}

char* cpy_frac_to_str(const Frac *frac) {
    if (frac == NULL) {
        return NULL;
    }
    char *numerator = bigint_to_str(frac->numerator);
    char *denominator = bigint_to_str(frac->denominator);
    if (numerator == NULL || denominator == NULL) {
        free(numerator);
        free(denominator);
        return NULL;
    }

    size_t numerator_size = strlen(numerator);
    size_t denominator_size = strlen(denominator);
    if (denominator_size > SIZE_MAX - 2 ||
            numerator_size > SIZE_MAX - denominator_size - 2) {
        free(numerator);
        free(denominator);
        return NULL;
    }
    size_t size = numerator_size + denominator_size + 2;
    char *result = malloc(size);
    if (result != NULL) {
        snprintf(result, size, "%s/%s", numerator, denominator);
    }
    free(numerator);
    free(denominator);
    return result;
}

void cpy_frac_print(const Frac *frac) {
    char *text = cpy_frac_to_str(frac);
    if (text == NULL) {
        fputs("(null)\n", stdout);
        return;
    }
    puts(text);
    free(text);
}

Float* cpy_create_float(long precision) {
    if (precision < MPFR_PREC_MIN || precision > MPFR_PREC_MAX) {
        return NULL;
    }

    Float *new_float = calloc(1, sizeof(Float));
    if (new_float == NULL) {
        return NULL;
    }
    new_float->precision = (mpfr_prec_t)precision;
    mpfr_init2(new_float->value, new_float->precision);
    mpfr_set_zero(new_float->value, 0);
    return new_float;
}

static mpfr_prec_t cpy_float_result_precision(
        const Float *left, const Float *right) {
    return left->precision > right->precision
        ? left->precision
        : right->precision;
}

Float* cpy_float_from_string(const char *value, long precision) {
    if (value == NULL) {
        return NULL;
    }
    Float *result = cpy_create_float(precision);
    if (result == NULL) {
        return NULL;
    }
    char *end = NULL;
    mpfr_strtofr(result->value, value, &end, 10, MPFR_RNDN);
    if (end == value || end == NULL || *end != '\0') {
        cpy_destroy_float(result);
        return NULL;
    }
    return result;
}

Float* cpy_float_from_int(int64_t value, long precision) {
    Float *result = cpy_create_float(precision);
    if (result != NULL) {
        mpfr_set_sj(result->value, value, MPFR_RNDN);
    }
    return result;
}

Float* cpy_float_from_double(double value, long precision) {
    Float *result = cpy_create_float(precision);
    if (result != NULL) {
        mpfr_set_d(result->value, value, MPFR_RNDN);
    }
    return result;
}

Float* cpy_float_add(const Float *left, const Float *right) {
    if (left == NULL || right == NULL) {
        return NULL;
    }
    Float *result = cpy_create_float(
        cpy_float_result_precision(left, right));
    if (result != NULL) {
        mpfr_add(result->value, left->value, right->value, MPFR_RNDN);
    }
    return result;
}

Float* cpy_float_sub(const Float *left, const Float *right) {
    if (left == NULL || right == NULL) {
        return NULL;
    }
    Float *result = cpy_create_float(
        cpy_float_result_precision(left, right));
    if (result != NULL) {
        mpfr_sub(result->value, left->value, right->value, MPFR_RNDN);
    }
    return result;
}

Float* cpy_float_mul(const Float *left, const Float *right) {
    if (left == NULL || right == NULL) {
        return NULL;
    }
    Float *result = cpy_create_float(
        cpy_float_result_precision(left, right));
    if (result != NULL) {
        mpfr_mul(result->value, left->value, right->value, MPFR_RNDN);
    }
    return result;
}

Float* cpy_float_div(const Float *left, const Float *right) {
    if (left == NULL || right == NULL) {
        return NULL;
    }
    Float *result = cpy_create_float(
        cpy_float_result_precision(left, right));
    if (result != NULL) {
        mpfr_div(result->value, left->value, right->value, MPFR_RNDN);
    }
    return result;
}

Float* cpy_float_mod(const Float *left, const Float *right) {
    if (left == NULL || right == NULL) {
        return NULL;
    }
    Float *result = cpy_create_float(
        cpy_float_result_precision(left, right));
    if (result != NULL) {
        mpfr_fmod(result->value, left->value, right->value, MPFR_RNDN);
    }
    return result;
}

Float* cpy_float_pow(const Float *base, const Float *exponent) {
    if (base == NULL || exponent == NULL) {
        return NULL;
    }
    Float *result = cpy_create_float(
        cpy_float_result_precision(base, exponent));
    if (result != NULL) {
        mpfr_pow(result->value, base->value, exponent->value, MPFR_RNDN);
    }
    return result;
}

Float* cpy_float_neg(const Float *value) {
    if (value == NULL) {
        return NULL;
    }
    Float *result = cpy_create_float(value->precision);
    if (result != NULL) {
        mpfr_neg(result->value, value->value, MPFR_RNDN);
    }
    return result;
}

Float* cpy_float_abs(const Float *value) {
    if (value == NULL) {
        return NULL;
    }
    Float *result = cpy_create_float(value->precision);
    if (result != NULL) {
        mpfr_abs(result->value, value->value, MPFR_RNDN);
    }
    return result;
}

Float* cpy_float_sqrt(const Float *value) {
    if (value == NULL) {
        return NULL;
    }
    Float *result = cpy_create_float(value->precision);
    if (result != NULL) {
        mpfr_sqrt(result->value, value->value, MPFR_RNDN);
    }
    return result;
}

static Float* cpy_float_round_integral(const Float *value, int operation) {
    if (value == NULL) {
        return NULL;
    }
    Float *result = cpy_create_float(value->precision);
    if (result == NULL) {
        return NULL;
    }
    switch (operation) {
        case 0:
            mpfr_floor(result->value, value->value);
            break;
        case 1:
            mpfr_ceil(result->value, value->value);
            break;
        default:
            mpfr_trunc(result->value, value->value);
            break;
    }
    return result;
}

Float* cpy_float_floor(const Float *value) {
    return cpy_float_round_integral(value, 0);
}

Float* cpy_float_ceil(const Float *value) {
    return cpy_float_round_integral(value, 1);
}

Float* cpy_float_trunc(const Float *value) {
    return cpy_float_round_integral(value, 2);
}

int cpy_float_cmp(const Float *left, const Float *right) {
    if (left == NULL || right == NULL) {
        return left == right ? 0 : (left == NULL ? -1 : 1);
    }
    if (mpfr_unordered_p(left->value, right->value)) {
        return 2;
    }
    int result = mpfr_cmp(left->value, right->value);
    return result < 0 ? -1 : result > 0 ? 1 : 0;
}

double cpy_float_to_double(const Float *value) {
    return value == NULL ? NAN : mpfr_get_d(value->value, MPFR_RNDN);
}

char* cpy_float_to_string(const Float *value, int significant_digits) {
    if (value == NULL || significant_digits < 0) {
        return NULL;
    }
    char *text = NULL;
    int result;
    if (significant_digits == 0) {
        result = mpfr_asprintf(&text, "%Rg", value->value);
    } else {
        result = mpfr_asprintf(
            &text, "%.*Rg", significant_digits, value->value);
    }
    if (result < 0 || text == NULL) {
        if (text != NULL) {
            mpfr_free_str(text);
        }
        return NULL;
    }

    size_t length = strlen(text);
    if (length == SIZE_MAX) {
        mpfr_free_str(text);
        return NULL;
    }
    char *copy = malloc(length + 1);
    if (copy != NULL) {
        memcpy(copy, text, length + 1);
    }
    mpfr_free_str(text);
    return copy;
}

int cpy_float_is_nan(const Float *value) {
    return value != NULL && mpfr_nan_p(value->value);
}

int cpy_float_is_infinite(const Float *value) {
    return value != NULL && mpfr_inf_p(value->value);
}

int cpy_float_is_zero(const Float *value) {
    return value != NULL && mpfr_zero_p(value->value);
}

void cpy_destroy_float(Float *value) {
    if (value == NULL) {
        return;
    }
    mpfr_clear(value->value);
    free(value);
}

Frac* cpy_float_tofrac(const Float *value) {
    if (value == NULL || mpfr_inf_p(value->value) || mpfr_nan_p(value->value)) {
        return NULL;
    }
    if (mpfr_zero_p(value->value)) {
        return cpy_frac_from_ints(0, 1);
    }

    mpz_t significand;
    mpz_init(significand);
    mpfr_exp_t exp = mpfr_get_z_2exp(significand, value->value);

    mpz_t numerator, denominator;
    mpz_inits(numerator, denominator, NULL);

    if (exp >= 0) {
        mpz_mul_2exp(numerator, significand, (mp_bitcnt_t)exp);
        mpz_set_ui(denominator, 1);
    } else {
        mpz_set(numerator, significand);
        mpz_set_ui(denominator, 1);
        mp_bitcnt_t shift = (mp_bitcnt_t)(-(exp + 1)) + 1;
        mpz_mul_2exp(denominator, denominator, shift);
    }

    BigInt bignumerator = bigint_from_mpz(numerator);
    BigInt bigdenominator = bigint_from_mpz(denominator);
    mpz_clears(significand, numerator, denominator, NULL);

    return cpy_frac_from_owned(bignumerator, bigdenominator);
}

Float* cpy_frac_tofloat(const Frac *value, long precision) {
    if (value == NULL) {
        return NULL;
    }

    Float *result = cpy_create_float(precision);
    if (result == NULL) {
        return NULL;
    }

    if (!bigint_to_mpfr(result->value, value->numerator)) {
        cpy_destroy_float(result);
        return NULL;
    }

    mpfr_t denominator;
    mpfr_init2(denominator, result->precision);
    if (!bigint_to_mpfr(denominator, value->denominator)) {
        mpfr_clear(denominator);
        cpy_destroy_float(result);
        return NULL;
    }

    mpfr_div(result->value, result->value, denominator, MPFR_RNDN);
    mpfr_clear(denominator);

    return result;
}
