# Narrow unsigned words (uint8/16/32) are real integers for bitwise ops.
# Regression: semantic used to reject `uint32 ^ uint32` with
# `bitwise operator CARET requires integer operands`.

uint32 (A) = 0xF0F0F0F0
uint32 (B) = 0x0F0F0F0F

def check():
    print(A ^ B)          # 4294967295
    print(A & B)          # 0
    print(A | B)          # 4294967295
    uint32 inv = ~A       # 252645135
    print(inv)
    print(B % 7)          # 1
    print(B << 1)         # 505290270
    print(A >> 4)         # 252645135
    uint16 s = 0xFFFF
    uint16 t = 0x00FF
    uint16 x = s ^ t      # 65280
    print(x)

def main():
    check()
