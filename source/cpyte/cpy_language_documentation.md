# Cpy Language Reference

## Status Notice

**This documentation describes the implemented features of the Cpy language compiler.**

## Overview

Cpy is a compiled programming language that combines Python-like syntax with systems-level capabilities. It features static typing, manual memory management, direct C interoperability, and compilation to native machine code via LLVM.

## Table of Contents

1. [Language Overview](#language-overview)
2. [Lexical Structure](#lexical-structure)
3. [Type System](#type-system)
4. [64-bit Support](#64-bit-support)
5. [Expressions and Operators](#expressions-and-operators)
6. [Statements and Control Flow](#statements-and-control-flow)
7. [Functions](#functions)
8. [Structures and User-Defined Types](#structures-and-user-defined-types)
9. [Memory Management](#memory-management)
10. [C Interoperability](#c-interoperability)
11. [Compilation Model](#compilation-model)
12. [Standard Library](#standard-library)

---

## Language Overview

Cpy combines high-level language syntax with low-level system programming capabilities. The language targets developers who need:

- **Performance**: Native compilation via LLVM with optimization levels
- **Control**: Manual memory management with pointer operations
- **Interoperability**: C library integration
- **Safety**: Static type checking with semantic analysis
- **Productivity**: Python-inspired syntax

### Design Philosophy

The language follows these core principles:

1. **Explicit is better than implicit**: Memory operations and type conversions are explicit
2. **Systems programming first**: Designed for scenarios where C would traditionally be used
3. **Modern syntax**: Uses Python-like patterns while maintaining systems semantics
4. **Zero-cost abstractions**: High-level features compile to efficient machine code

---

## Lexical Structure

### Source Code Representation

Cpy source files use the `.cpy` extension and are represented as UTF-8 encoded text files. The language uses significant whitespace (indentation) for block structure, similar to Python.

### Comments

```cpy
# Single-line comments extend to the end of the line
# There are no multi-line comment delimiters
```

### Identifiers

Identifiers follow these rules:
- Must start with a letter (a-z, A-Z) or underscore (_)
- Subsequent characters can be letters, digits, or underscores
- Case-sensitive
- No length limit

```cpy
variable_name = 42
_private_var = "internal"
CONSTANT_VALUE = 3.14
```

### Keywords

The following reserved words cannot be used as identifiers:

**Control Flow:**
- `if`, `else`, `elif`, `while`, `for`, `in`, `break`, `continue`
- `switch`, `case`, `default`

**Declarations:**
- `def`, `class`, `return`, `struct`

**Access Modifiers:**
- `public`, `private`, `static`, `virtual`, `override`

**Memory and Types:**
- `new`, `sizeof`, `ref`, `int64`, `uint64`

**Literals and Constants:**
- `true`, `false`, `null`

**Operators:**
- `and`, `or`, `not`

**I/O:**
- `print`, `input`

**Modules:**
- `import`

### Literals

#### Numeric Literals

```cpy
# Integer literals
42
-17
0

# 64-bit integer literals
9223372036854775807
-9223372036854775808
18446744073709551615

# Hexadecimal literals (useful for 64-bit values)
0x7FFFFFFFFFFFFFFF  # Maximum int64
0xFFFFFFFFFFFFFFFF  # Maximum uint64

# Floating-point literals
3.14
-0.001
2.0e10  # Scientific notation
```

#### String Literals

```cpy
"Hello, World!"
'Single quotes also work'
"Escaped characters: \n\t\r\\\""
```

#### Boolean and Null Literals

```cpy
is_valid = true
is_error = false
empty_value = null
```

### Operators and Punctuation

#### Arithmetic Operators
- `+` (addition), `-` (subtraction)
- `*` (multiplication), `/` (division)
- `//` (integer division), `%` (modulo)
- `**` (exponentiation)

#### Comparison Operators
- `==` (equality), `!=` (inequality)
- `<` (less than), `>` (greater than)
- `<=` (less than or equal), `>=` (greater than or equal)

#### Logical Operators
- `and` (logical AND), `or` (logical OR), `not` (logical NOT)

#### Bitwise Operators
- `&` (bitwise AND), `|` (bitwise OR), `^` (bitwise XOR)
- `~` (bitwise NOT)
- `<<` (left shift), `>>` (right shift)

#### Assignment Operators
- `=` (simple assignment)
- `+=`, `-=`, `*=`, `/=`, `//=` (compound assignment)

#### Memory Operators
- `*` (dereference), `&` (address-of)
- `--` (decrement)

#### Other Operators
- `->` (return type annotation)
- `.` (member access), `[]` (array indexing)
- `()` (function call), `,` (comma separator)

---

## Type System

Cpy employs a static type system with type inference and explicit type annotations. The type system catches errors at compile time while maintaining flexibility.

### Primitive Types

#### Integer Types

Cpy supports multiple integer types with different bit widths and signedness:

**32-bit Integers:**
```cpy
int x = 42
int negative = -17
```

**64-bit Integers:**
```cpy
int64 large_number = 9223372036854775807
int64 negative_large = -9223372036854775808
```

**Unsigned 64-bit Integers:**
```cpy
uint64 unsigned_large = 18446744073709551615
uint64 hex_value = 0xFFFFFFFFFFFFFFFF
```

**Integer Type Summary:**
- `int`: 32-bit signed integer (range: -2,147,483,648 to 2,147,483,647)
- `int64`: 64-bit signed integer (range: -9,223,372,036,854,775,808 to 9,223,372,036,854,775,807)
- `uint64`: 64-bit unsigned integer (range: 0 to 18,446,744,073,709,551,615)

#### Floating-Point Type
```cpy
float pi = 3.14159
float precision = 0.0001
```

#### String Type
```cpy
str name = "Cpy Programming"
str empty = ""
```

#### Boolean Type
```cpy
bool is_valid = true
bool is_error = false
```

### Pointer Types

Cpy supports pointers for manual memory management:

```cpy
int* ptr           # Pointer to integer
int** ptr_to_ptr   # Pointer to pointer
void* generic_ptr  # Generic pointer
```

**64-bit Address Space:**
- All pointers are 64-bit on 64-bit platforms
- Supports addressing up to 2^64 bytes of memory
- Compatible with modern 64-bit operating systems and hardware

**Additional Pointer Types:**
```cpy
int64* large_ptr   # Pointer to 64-bit integer
uint64* uint64_ptr  # Pointer to unsigned 64-bit integer
```

### Array Types

Arrays are dynamically allocated with explicit size:

```cpy
int[] numbers      # Array of integers
str[] strings      # Array of strings
Point[] points     # Array of Point structures
```

### Type Annotations

Type annotations use the `->` syntax for functions and `:` syntax for variables:

```cpy
public def function_name(param1: int, param2: str) -> int:
    # Function body
    return 0

int x = 42
str name = "example"
```

### Type Inference

The compiler can infer types in many contexts:

```cpy
# Type inferred from literal
x = 42  # Inferred as int
y = 3.14  # Inferred as float

# Large literals automatically inferred as 64-bit
large = 9223372036854775807  # Inferred as int64
hex_val = 0xFFFFFFFFFFFFFFFF  # Inferred as uint64

# Type inferred from expression
result = x + y  # Type inferred based on operation
```

---

## 64-bit Support

Cpy provides 64-bit support for modern computing platforms, enabling developers to work with large datasets, high-precision calculations, and memory-intensive applications.

### Platform Architecture

Cpy supports 64-bit computing environments with the following capabilities:

- **64-bit address space**: Support for up to 2^64 bytes of addressable memory
- **64-bit integer arithmetic**: Native support for 64-bit signed and unsigned integers
- **64-bit pointers**: All pointer types are 64-bit on supported platforms
- **LLVM backend**: Leverages LLVM's 64-bit optimization capabilities

### 64-bit Integer Types

#### Signed 64-bit Integers (int64)

The `int64` type provides a 64-bit signed integer with the following characteristics:

- **Range**: -9,223,372,036,854,775,808 to 9,223,372,036,854,775,807
- **Size**: 8 bytes (64 bits)
- **Use cases**: Large counters, timestamps, file sizes, database keys

```cpy
# 64-bit signed integer usage
int64 file_size = 9223372036854775807
int64 timestamp = 1699999999999
int64 database_id = 1234567890123456789

# Arithmetic operations
int64 result = file_size + 1024
int64 multiplied = timestamp * 2
```

#### Unsigned 64-bit Integers (uint64)

The `uint64` type provides a 64-bit unsigned integer with the following characteristics:

- **Range**: 0 to 18,446,744,073,709,551,615
- **Size**: 8 bytes (64 bits)
- **Use cases**: Memory addresses, bit masks, unsigned counters, hashes

```cpy
# 64-bit unsigned integer usage
uint64 memory_address = 0xFFFFFFFFFFFFFFFF
uint64 bit_mask = 0x123456789ABCDEF0
uint64 hash_value = 18446744073709551615

# Bitwise operations
uint64 masked = memory_address & 0x00000000FFFFFFFF
uint64 shifted = hash_value >> 16
```

### Type Conversions

#### Type Conversions

```cpy
# 32-bit to 64-bit conversion
int x = 42
int64 large_x = x  # Automatic promotion

# 64-bit to 32-bit conversion
int64 large_value = 9223372036854775807
int reduced = (int)large_value  # Explicit cast required
```

#### Type Promotion

The compiler promotes types in mixed expressions:

```cpy
# int to int64 promotion
int small = 42
int64 large = 1000000000000
int64 result = small + large  # small is automatically promoted to int64

# int64 to uint64 conversion
int64 signed_val = -100
uint64 unsigned_val = signed_val  # Implicit conversion
```

#### Overflow

```cpy
# 64-bit arithmetic overflow
int64 max_int64 = 9223372036854775807
int64 overflow_result = max_int64 + 1  # Implementation-defined behavior

# Unsigned overflow
uint64 max_uint64 = 18446744073709551615
uint64 wrapped = max_uint64 + 1  # Wraps to 0
```

### Memory Operations

#### 64-bit Pointers

```cpy
int64* ptr_to_large = new int64
*ptr_to_large = 9223372036854775807

uint64* ptr_to_unsigned = new uint64
*ptr_to_unsigned = 0xFFFFFFFFFFFFFFFF

# Pointer arithmetic
int64* array_start = new int64[1000]
int64* fifth_element = array_start + 4
```

#### 64-bit Array Allocation

```cpy
int64[] large_array = new int64[1000000]
uint64[] huge_array = new uint64[10000000]

# 64-bit array indexing
int64 index = 500000000
int64 value = large_array[index]
```

### Platform-Specific Considerations

#### Data Model

Cpy follows the LP64 data model on 64-bit platforms:

- `int`: 32 bits
- `pointer`: 64 bits
- `int64`: 64 bits
- `uint64`: 64 bits

#### Endianness

Cpy supports both little-endian and big-endian architectures:

```cpy
uint64 value = 0x123456789ABCDEF0
# Compiler handles endianness conversion
```

### Performance Considerations

#### Native 64-bit Operations

Modern 64-bit processors provide native support for 64-bit operations:

```cpy
int64 a = 9223372036854775807
int64 b = 1000000000
int64 result = a + b  # Single instruction on 64-bit CPUs
```

#### Memory Bandwidth

64-bit types improve memory bandwidth utilization:

```cpy
int64[] data = new int64[1000]
# Each memory operation transfers 8 bytes
```

### Use Cases for 64-bit Types

#### File System Operations

```cpy
# Large file handling
int64 file_size = get_file_size("large_file.dat")
int64 position = seek_file(file_handle, 9223372036854775807)
```

#### Database Operations

```cpy
# 64-bit primary keys
int64 user_id = 1234567890123456789
int64 transaction_id = generate_unique_id()
```

#### Cryptographic Operations

```cpy
# 64-bit blocks for cryptographic operations
uint64 block = 0x123456789ABCDEF0
uint64 encrypted = encrypt_block(block)
```

#### High-Precision Timestamps

```cpy
# Nanosecond precision timestamps
int64 timestamp_ns = get_current_time_ns()
int64 elapsed = timestamp_ns - start_time
```

#### Memory Addressing

```cpy
# Direct memory addressing
uint64 address = 0x123456789ABC
uint64* memory_ptr = (uint64*)address
uint64 value = *memory_ptr
```

### Compatibility and Portability

#### Cross-Platform 64-bit Support

Cpy's 64-bit support is consistent across platforms:

- **Linux x86_64**: Full 64-bit support
- **macOS ARM64/x86_64**: Full 64-bit support
- **Windows x64**: Full 64-bit support

#### ABI Compatibility

Cpy maintains ABI compatibility with C 64-bit types:

```cpy
import "c_library.c"
int64 result = c_function_64bit(9223372036854775807)
```

### Best Practices for 64-bit Programming

#### Type Selection Guidelines

1. **Use `int` for general-purpose arithmetic** when values fit in 32-bit range
2. **Use `int64` for large values** that may exceed 32-bit limits
3. **Use `uint64` for unsigned operations** and bit manipulation
4. **Avoid mixing signed/unsigned** without explicit conversions

#### Overflow Prevention

```cpy
public def safe_add_64(a: int64, b: int64) -> int64:
    if a > 0 and b > (9223372036854775807 - a):
        print("Overflow detected")
        return 0
    return a + b
```

#### Memory Alignment

```cpy
struct AlignedData:
    int32 small_value
    int32 padding  # Ensure 64-bit alignment
    int64 large_value
```

### 64-bit Literal Syntax

#### Decimal Literals

```cpy
int64 large_decimal = 9223372036854775807
uint64 unsigned_decimal = 18446744073709551615
```

#### Hexadecimal Literals

```cpy
int64 hex_max = 0x7FFFFFFFFFFFFFFF
uint64 hex_unsigned_max = 0xFFFFFFFFFFFFFFFF
int64 hex_value = 0x123456789ABCDEF0
```

### 64-bit in Standard Library

#### 64-bit I/O Operations

```cpy
# Print 64-bit values
print(9223372036854775807)  # Handles 64-bit integers automatically
print(0xFFFFFFFFFFFFFFFF)  # Handles 64-bit hexadecimal
```

#### 64-bit Math Functions

```cpy
# 64-bit mathematical operations
int64 absolute = abs_64(-9223372036854775807)
int64 minimum = min_64(a, b)
int64 maximum = max_64(a, b)
```

### Debugging 64-bit Code

#### Common Issues

1. **Silent overflow**: 64-bit overflow may not be immediately apparent
2. **Sign extension**: Accidental sign extension in mixed-type operations
3. **Endianness issues**: When working with binary data
4. **Alignment problems**: Misaligned 64-bit accesses on some platforms

#### Debugging Techniques

```cpy
int64 value = 9223372036854775807
print(value)

# Boundary testing
int64 test_max = 9223372036854775807
int64 test_min = -9223372036854775808
print(test_max)
print(test_min)
```

---

## Expressions and Operators

### Operator Precedence

Operators are evaluated in the following order (highest to lowest):

1. **Primary expressions**: literals, identifiers, parenthesized expressions
2. **Postfix operators**: `()`, `[]`, `.`, `->`
3. **Unary operators**: `*`, `&`, `+`, `-`, `~`, `not`
4. **Exponentiation**: `**`
5. **Multiplicative**: `*`, `/`, `//`, `%`
6. **Additive**: `+`, `-`
7. **Shift**: `<<`, `>>`
8. **Relational**: `<`, `>`, `<=`, `>=`
9. **Equality**: `==`, `!=`
10. **Bitwise AND**: `&`
11. **Bitwise XOR**: `^`
12. **Bitwise OR**: `|`
13. **Logical AND**: `and`
14. **Logical OR**: `or`

### Arithmetic Expressions

```cpy
# Basic arithmetic
result = 10 + 5 * 2        # 20 (multiplication has higher precedence)
division = 20 / 4          # 5.0 (float division)
int_div = 20 // 4          # 5 (integer division)
modulo = 17 % 5            # 2
power = 2 ** 10            # 1024

# Compound assignment
x += 5    # x = x + 5
x -= 3    # x = x - 3
x *= 2    # x = x * 2
x /= 4    # x = x / 4
```

### Comparison Expressions

```cpy
# Equality and inequality
if x == y:
    print("equal")
if x != y:
    print("not equal")

# Relational comparisons
if x < y:
    print("less than")
if x <= y:
    print("less than or equal")
if x > y:
    print("greater than")
if x >= y:
    print("greater than or equal")
```

### Logical Expressions

```cpy
# Logical operators
if condition1 and condition2:
    print("both true")

if condition1 or condition2:
    print("at least one true")

if not condition:
    print("condition is false")
```

### Bitwise Expressions

```cpy
# Bitwise operations
a = 10   # 0b1010
b = 12   # 0b1100

result = a & b   # Bitwise AND: 8  (0b1000)
result = a | b   # Bitwise OR:  14 (0b1110)
result = a ^ b   # Bitwise XOR: 6  (0b0110)
result = ~a      # Bitwise NOT: -11
result = a << 2  # Left shift: 40  (0b101000)
result = a >> 1  # Right shift: 5  (0b101)
```

### Memory Operations

```cpy
# Address-of operator
int x = 42
int* ptr = &x    # ptr now holds the address of x

# Dereference operator
print(*ptr)      # Prints 42 (value at address stored in ptr)
*ptr = 99        # Sets x to 99 through pointer

# Pointer-to-pointer
int** pptr = &ptr
print(**pptr)    # Prints 99
```

### String Operations

```cpy
str greeting = "Hello"
str target = "World"
str message = greeting + ", " + target

# String indexing
first_char = message[0]  # 'H'
```

### Sizeof Operator

```cpy
int_size = sizeof(int)
struct_size = sizeof(Point)
```

### New Expression (Memory Allocation)

```cpy
int* ptr = new int
Point* p = new Point

int[] arr = new int[10]
Point[] points = new Point[5]
```

---

## Statements and Control Flow

### Expression Statements

Any expression can be a statement:

```cpy
x = 42
print(x)
function_call(arg1, arg2)
```

### Variable Declarations

```cpy
# Simple declaration
int x
str name

# Declaration with initialization
int age = 25
str city = "New York"

# Multiple declarations
int a, b, c
```

### Assignment Statements

```cpy
# Simple assignment
x = 10

# Assignment with compound operators
x += 5
x *= 2

# Member assignment
point.x = 15
point.y = 20

# Array element assignment
arr[0] = 42
arr[5] = 100
```

### Conditional Statements

#### If-Else Statement

```cpy
if condition:
    print("condition is true")
elif other_condition:
    print("other condition is true")
else:
    print("no condition matched")
```

#### Switch Statement

```cpy
switch value:
    case 1:
        print("one")
    case 2:
        print("two")
    default:
        print("other")
```

### Loop Statements

#### While Loop

```cpy
while condition:
    print("loop body")
    # Update condition to avoid infinite loop
```

#### For Loop

```cpy
for item in collection:
    print(item)
```

### Jump Statements

#### Break Statement

```cpy
while true:
    if condition:
        break
```

#### Continue Statement

```cpy
for i in range(10):
    if i % 2 == 0:
        continue
    print(i)
```

#### Return Statement

```cpy
public def calculate(x: int, y: int) -> int:
    return x + y
```

### Block Statements

Blocks are defined by indentation:

```cpy
public def example():
    int x = 10
    # This is a block
    if x > 5:
        int y = 20
        # Nested block
        print(x + y)
```

---

## Functions

### Function Declaration

Functions are declared using the `def` keyword with optional access modifiers:

```cpy
public def function_name(param1: int, param2: str) -> int:
    # Function body
    return 0
```

### Access Modifiers

```cpy
# Public function (can be called from outside)
public def public_function() -> int:
    return 42

# Private function (internal use only)
private def helper_function() -> int:
    return 0

# Static function (class-level)
static def static_function() -> int:
    return 1
```

### Function Parameters

```cpy
public def process_numbers(a: int, b: int, c: float) -> float:
    return a + b + c

public def greet(name: str = "World") -> str:
    return "Hello, " + name
```

### Return Types

```cpy
public def add(a: int, b: int) -> int:
    return a + b

public def print_message(msg: str):
    print(msg)
```

### Function Calls

```cpy
result = add(10, 20)
result = process_numbers(a=5, b=10, c=2.5)
object.method(arg1, arg2)
```

### Recursive Functions

```cpy
public def factorial(n: int) -> int:
    if n <= 1:
        return 1
    return n * factorial(n - 1)
```

---

## Structures and User-Defined Types

### Structure Definition

Structures are defined using the `struct` keyword:

```cpy
struct Point:
    int x
    int y
```

### Structure Instantiation

```cpy
Point p
p.x = 10
p.y = 20
```

### Nested Structures

```cpy
struct Line:
    Point start
    Point end

Line line
line.start.x = 0
line.start.y = 0
line.end.x = 100
line.end.y = 100
```

### Generic Structures

```cpy
struct Pair<T, U>:
    T first
    U second

Pair<int, str> pair
pair.first = 42
pair.second = "answer"
```

### Structure Methods

```cpy
struct Point:
    int x
    int y
    
    public def add(other: Point) -> Point:
        Point result
        result.x = self.x + other.x
        result.y = self.y + other.y
        return result
```

### Self-Referential Structures

```cpy
struct ListNode:
    int value
    ListNode* next
```

---

## Memory Management

### Stack Allocation

Regular variables are allocated on the stack:

```cpy
int x = 42        # Stack allocation
Point p           # Stack allocation
p.x = 10
p.y = 20
```

### Heap Allocation

```cpy
int* ptr = new int
*ptr = 42

int[] arr = new int[10]
arr[0] = 1
arr[9] = 10
```

### Memory Operations

```cpy
int x = 42
int* ptr = &x

print(*ptr)
*ptr = 99

int* arr = new int[10]
int* second = arr + 1
```

### Memory Safety Considerations

Cpy provides manual memory management:

- **Memory leaks**: Free allocated memory when no longer needed
- **Dangling pointers**: Avoid using pointers to freed memory
- **Buffer overflows**: Ensure array accesses are within bounds
- **Null pointer dereferences**: Check pointers before dereferencing

---

## C Interoperability

### Importing Cpy Module Files

Cpy files can import other `.cpy` files:

```cpy
import "math_utils.cpy"
import "data_structs.cpy"
```

Only functions marked with the `public` keyword are exported. Imported functions and structs are inlined into the same LLVM module.

```cpy
# math_utils.cpy
public def add(a int, b int) -> int:
    return a + b

private def helper():
    pass
```

```cpy
# main.cpy
import "math_utils.cpy"

def main():
    int result = add(3, 4)    # calls public function from math_utils.cpy
    print(result)
```

### Importing C Libraries

Cpy can import C libraries directly:

```cpy
import "math_library.c"
import "string_operations.c"
```

### Calling C Functions

```cpy
result = c_function(arg1, arg2)
```

### C Type Mapping

Cpy types map to C types as follows:

- `int` → `int` (32-bit signed)
- `int64` → `long long` or `int64_t`
- `uint64` → `unsigned long long` or `uint64_t`
- `float` → `double`
- `str` → `char*`
- `int*` → `int*`
- `int64*` → `long long*` or `int64_t*`
- `uint64*` → `unsigned long long*` or `uint64_t*`
- `void*` → `void*`

### Example: C Library Integration

```cpy
import "test_import.c"

def main():
    int x
    x = my_add(3, 4)    # Call C function
    print(x)
    x = my_double(10)   # Call another C function
    print(x)
```

---

## Compilation Model

### Compilation Pipeline

Cpy source code goes through several compilation stages:

1. **Lexical Analysis**: Source code is tokenized
2. **Syntax Analysis**: Tokens are parsed into an Abstract Syntax Tree (AST)
3. **Semantic Analysis**: Type checking and scope validation
4. **LLVM IR Generation**: AST is converted to LLVM Intermediate Representation
5. **Optimization**: LLVM optimization passes improve performance
6. **Code Generation**: Native machine code is generated

### Compiler Invocation

The Cpy compiler can be invoked with different optimization levels:

```bash
# Compile with default optimization
./program source.cpy

# Compile with specific optimization level
./program source.cpy -O2
```

### JIT Compilation

Cpy supports Just-In-Time compilation for rapid development:

```python
# Python example of JIT execution
from source.compiling import run_jit

run_jit(module, opt_level=3)
```

### AOT Compilation

Ahead-Of-Time compilation produces optimized native binaries:

```python
# Python example of AOT compilation
from source.compiling import run_aot

run_aot(module, output="program.o", opt_level=3)
```

### Optimization Levels

- `-O0`: No optimization
- `-O1`: Basic optimization
- `-O2`: Standard optimization
- `-O3`: Aggressive optimization

---

## Standard Library

### I/O Operations

#### Print Function

```cpy
print(42)
print(3.14)
print("Hello")
print(x + y)
```

#### Input Function

```cpy
int value = input()
```

### Mathematical Operations

The language supports basic mathematical operations through operators.

### String Operations

```cpy
str result = "Hello" + " " + "World"
int len = strlen(string_ptr)
```

---

## Advanced Features

### Access Control

```cpy
public public_var = 42
private private_var = 10
```

### Static Members

```cpy
static class_variable = 100
```

### Virtual Functions

```cpy
virtual override def function_name() -> int:
    return 0
```

### References

```cpy
def process(ref value: int):
    value = 42
```

---

## Best Practices

### Memory Management

1. **Always initialize pointers**
2. **Free allocated memory**
3. **Check for null**
4. **Use stack allocation when possible**

### Type Safety

1. **Use type annotations**
2. **Enable strict type checking**
3. **Avoid implicit conversions**

### Performance

1. **Prefer stack allocation**
2. **Use appropriate optimization levels**
3. **Minimize pointer indirection**
4. **Leverage 64-bit operations**
5. **Consider alignment**

### Code Organization

1. **Use meaningful names**
2. **Keep functions small**
3. **Organize structures logically**
4. **Document complex logic**

---

## Example Programs

### Hello World

```cpy
public def main() -> int:
    print("Hello, World!")
    return 0
```

### Factorial Calculation

```cpy
public def factorial(n: int) -> int:
    if n <= 1:
        return 1
    return n * factorial(n - 1)

public def main() -> int:
    int result = factorial(5)
    print(result)  # 120
    return 0
```

### Linked List Implementation

```cpy
struct ListNode:
    int value
    ListNode* next

public def create_list(n: int) -> ListNode*:
    ListNode* head = 0
    ListNode* current = 0
    int i = 0
    
    while i < n:
        ListNode* node = new ListNode
        node.value = i * 10
        node.next = 0
        
        if head == 0:
            head = node
        else:
            current.next = node
        
        current = node
        i = i + 1
    
    return head

public def sum_list(head: ListNode*) -> int:
    int total = 0
    ListNode* current = head
    
    while current != 0:
        total = total + current.value
        current = current.next
    
    return total

public def main() -> int:
    ListNode* list = create_list(5)
    int total = sum_list(list)
    print(total)
    return 0
```

### Matrix Operations

```cpy
struct Matrix:
    int[][] data
    int rows
    int cols

public def create_matrix(rows: int, cols: int) -> Matrix:
    Matrix m
    m.rows = rows
    m.cols = cols
    m.data = new int[rows][cols]
    return m

public def matrix_multiply(a: Matrix, b: Matrix) -> Matrix:
    if a.cols != b.rows:
        return 0
    
    Matrix result = create_matrix(a.rows, b.cols)
    
    int i = 0
    while i < a.rows:
        int j = 0
        while j < b.cols:
            int sum = 0
            int k = 0
            while k < a.cols:
                sum = sum + a.data[i][k] * b.data[k][j]
                k = k + 1
            result.data[i][j] = sum
            j = j + 1
        i = i + 1
    
    return result
```

### 64-bit File Processing

```cpy
public def process_large_file(filename: str) -> int64:
    int64 total_bytes = 0
    int64 buffer_size = 65536
    int64[] buffer = new int64[buffer_size / 8]
    
    int64 chunk = 0
    while chunk < 1000000:
        total_bytes = total_bytes + buffer_size
        chunk = chunk + 1
    
    return total_bytes

public def main() -> int:
    int64 file_size = process_large_file("large_file.dat")
    print(file_size)
    return 0
```

### 64-bit Cryptographic Hash Example

```cpy
public def simple_hash_64(data: str) -> uint64:
    uint64 hash = 0xFFFFFFFFFFFFFFFF
    int64 length = strlen(data)
    int64 i = 0
    
    while i < length:
        uint64 byte = (uint64)data[i]
        hash = hash ^ byte
        hash = hash * 0x100000001B3
        i = i + 1
    
    return hash

public def main() -> int:
    str message = "Hello, World!"
    uint64 hash_value = simple_hash_64(message)
    print(hash_value)
    return 0
```

### 64-bit Timestamp Processing

```cpy
public def timestamp_to_days(timestamp_ns: int64) -> int64:
    int64 ns_per_day = 86400000000000
    return timestamp_ns / ns_per_day

public def main() -> int:
    int64 current_time = 1699999999999999999
    int64 days = timestamp_to_days(current_time)
    print(days)
    return 0
```

---

## Language Limitations and Future Directions

### Current Limitations

1. **Limited standard library**: Basic I/O and mathematical operations
2. **Manual memory management**: No garbage collection
3. **No exception handling**: Error handling through return codes
4. **Limited generics**: Basic generic type support
5. **No modules system**: All code in single file or C imports
6. **No 64-bit operations in standard library**: 64-bit types are supported but utility functions are limited

### Potential Future Enhancements

1. **Enhanced standard library**: More comprehensive built-in functions
2. **Optional garbage collection**: Hybrid memory management
3. **Exception handling**: Structured error handling mechanism
4. **Advanced generics**: More sophisticated generic programming
5. **Module system**: Better code organization and reuse
6. **Concurrency primitives**: Threads, async/await, etc.
7. **Standard library containers**: Lists, dictionaries, sets, etc.

---

## Conclusion

Cpy balances high-level language syntax with low-level system control. Its Python-inspired syntax makes it accessible to developers familiar with modern scripting languages, while its C-like capabilities provide the performance and control needed for systems programming.

The language is suited for:

- **Systems programming**: Operating systems, drivers, embedded systems
- **Performance-critical applications**: Game engines, scientific computing
- **C library integration**: Wrapping existing C libraries with modern syntax
- **Education**: Teaching systems programming concepts with approachable syntax

---

## Appendix: Complete Grammar Reference

### Formal Grammar (Simplified)

```
program        ::= {statement | struct_def}
declaration    ::= type IDENTIFIER ["=" expression]
function       ::= ACCESS_MODIFIER? "def" IDENTIFIER "(" parameters ")" ["->" type] block
struct_def     ::= "struct" IDENTIFIER [generic_params] ":" {member_decl}
block          ::= NEWLINE INDENT statement DEDENT
statement      ::= expression_stmt | if_stmt | while_stmt | for_stmt | return_stmt | break_stmt | continue_stmt | print_stmt | import_stmt | declaration | function
expression_stmt ::= expression
if_stmt        ::= "if" expression block {"elif" expression block} ["else" block]
while_stmt     ::= "while" expression block
for_stmt       ::= "for" IDENTIFIER "in" expression block
return_stmt    ::= "return" [expression]
expression     ::= assignment
assignment     ::= logical_or (ASSIGN_OP assignment)?
logical_or     ::= logical_and {"or" logical_and}
logical_and    ::= equality {"and" equality}
equality       ::= relational {("==" | "!=") relational}
relational     ::= shift {("<" | ">" | "<=" | ">=") shift}
shift          ::= additive {("<<" | ">>") additive}
additive       ::= multiplicative {("+" | "-") multiplicative}
multiplicative ::= power {("*" | "/" | "//" | "%") power}
power          ::= unary ["**" power]
unary          ::= ("+" | "-" | "not" | "~" | "*" | "&") unary | postfix
postfix        ::= primary {("()" | "[]" | "." IDENTIFIER)}
primary        ::= NUMBER | STRING | IDENTIFIER | "(" expression ")" | "new" type | "sizeof" "(" type ")"
type           ::= "int" | "int64" | "uint64" | "float" | "str" | "bool" | "void" | type "*" | type "[]"
```

### Token Specifications

```python
NUMBER         ::= [0-9]+ ("." [0-9]+)? ([eE][+-]?[0-9]+)?
HEX_NUMBER     ::= "0x" [0-9A-Fa-f]+
STRING         ::= '"' ([^"\\] | "\\" .)* '"'
IDENTIFIER     ::= [a-zA-Z_] [a-zA-Z0-9_]*
```