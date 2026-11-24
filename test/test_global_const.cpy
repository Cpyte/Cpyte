# Global constants must be visible inside function bodies.
# Regression: top-level statements are normally lowered into `main` *after*
# function bodies are emitted, so `int (X) = ...` referenced from a function
# used to fail codegen with `Undefined variable 'X'`.

uint32 (M) = 0xFFFFFFFF
int (BASE) = 40
int (DERIVED) = BASE + 2

def twice(x int) -> int:
    return x * 2

# Runtime-valued constant: lowered to a module global by the const pre-pass.
int (RT) = twice(21)

def add(x int) -> int:
    return x + DERIVED

def get_rt() -> int:
    return RT

def main():
    print(add(0))      # 42
    print(M)           # 4294967295
    print(DERIVED)     # 42
    print(get_rt())    # 42
