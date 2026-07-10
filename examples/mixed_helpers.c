#include <stdint.h>

int64_t c_add_int64(int64_t a, int64_t b) {
    return a + b;
}

uint64_t c_bit_ops(uint64_t a, uint64_t b) {
    return (a & b) | (~a & ~b);
}

uint64_t c_checksum(uint64_t value) {
    uint64_t hash = value;
    hash = (hash ^ (hash >> 30)) * 0xBF58476D1CE4E5B9ULL;
    hash = (hash ^ (hash >> 27)) * 0x94D049BB133111EBULL;
    hash = hash ^ (hash >> 31);
    return hash;
}
