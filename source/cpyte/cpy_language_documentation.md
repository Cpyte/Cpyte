# Cpyte Language Reference

This reference describes the language accepted by the Cpyte compiler in this
repository. Cpyte is a statically analyzed, LLVM-backed compiled language with
Python-like indentation and explicit low-level facilities. It is not a Python
implementation: similar-looking syntax does not imply Python-compatible
semantics or standard-library behavior.

The parser accepts more syntax than every backend can necessarily lower.
Sections below distinguish syntax from semantic restrictions where that
distinction matters. Compiler diagnostics and tests for the active target are
authoritative for backend-specific details.

## Contents

- [Quick start](#quick-start)
- [Source form and lexical rules](#source-form-and-lexical-rules)
- [Types and declarations](#types-and-declarations)
- [Expressions and operators](#expressions-and-operators)
- [Statements and control flow](#statements-and-control-flow)
- [Functions and calls](#functions-and-calls)
- [Arrays and built-ins](#arrays-and-built-ins)
- [Structs, classes, protocols, and enums](#structs-classes-protocols-and-enums)
- [Pointers, allocation, and ownership operations](#pointers-allocation-and-ownership-operations)
- [Errors and deferred statements](#errors-and-deferred-statements)
- [Imports and native interoperation](#imports-and-native-interoperation)
- [Compiler and command-line use](#compiler-and-command-line-use)
- [Unsupported or restricted constructs](#unsupported-or-restricted-constructs)
- [Grammar summary](#grammar-summary)

## Quick start

```cpy
def main() -> int:
    print("Hello, Cpyte")
    return 0
```

Save as `hello.cpy` and run `cpy hello.cpy` to compile and run it using the
default JIT mode. Use `cpy --aot hello.cpy` to request a native executable.
`cpy --help` prints the options available in the installed compiler.

Blocks are introduced by a colon, newline, and increased indentation. Four
spaces are the default indentation convention. Tabs can be normalized using
the compiler's `--tab-size` option; mixing tabs and spaces within indentation
is rejected.

## Source form and lexical rules

### Comments, names, and line continuation

`#` begins a comment that extends to the end of the line. There is no
multiline-comment delimiter. Identifiers start with a letter or `_`, then
contain letters, digits, or `_`; names are case-sensitive. A backslash at
the end of a physical line continues the logical line.

### Keywords

Core reserved words include:

```text
and as assert borrow break case ccode class continue dataclass def default
defer del elif else enum except false for if import in input input_big
input_str interface int64 let llvm move mut new not null or override private
print protocol public raise ref return sealed sizeof static struct switch
true try type uint64 unsafe virtual while
```

`True` and `False` are also recognized as boolean literals. The userspace
extension system can register additional keywords and operators, so the list
may be extended in a configured compiler environment. `ref` is reserved, but
the documented borrow expression forms are `borrow value` and `borrow mut
value`; do not infer Python or C++ reference semantics from the keyword.

### Literals and strings

- Integer literals are decimal or hexadecimal (`0x` / `0X` followed by at
  least one hexadecimal digit).
- Decimal floating-point literals may have a fractional part and/or an
  exponent, for example `1.25`, `2e6`, and `1.5e-3`.
- `true`, `false`, `True`, `False`, and `null` are literals.
- Single- and double-quoted strings are supported. Recognized escapes include
  newline, tab, carriage return, backslash, quotes, NUL, `\xNN`, and
  four-hex-digit `\uNNNN` escapes.
- Raw strings use `r"..."` or `r'...'`; raw f-strings use `fr"..."`,
  `rf"..."`, `fr'...'`, or `rf'...'`.
- F-strings interpolate expressions in braces. Double braces spell literal
  braces: `f"{{value}}"`.
- Supported string values can be concatenated with `+`; use `str(value)` to
  request string conversion where supported.

Strings are single-line in the lexer; triple-quoted multiline strings are not
part of the current lexical syntax.

```cpy
name = "Ada"
raw_path = r"C:\tmp\file"
greeting = f"hello, {name}"
```

## Types and declarations

### Built-in type names

The parser recognizes these built-in names:

| Type | Notes |
|---|---|
| `int`, `int64`, `uint64` | Integer types; the fixed-width types are useful at native interfaces. |
| `float`, `double` | Distinct floating-point types; `float` is not an alias for `double`. |
| `bool` | Boolean type; literals are `true`/`false` (also `True`/`False`). |
| `str`, `string` | String type names. |
| `char` | Character/C interoperation type. |
| `void` | No-value return type; use `void*` for a pointer to void. |
| `big`, `ubig` | Signed and unsigned arbitrary-precision integer runtime types. |
| `dynamic` | Dynamically represented value; available only where the selected compilation mode supports the userspace runtime. |
| `size_t`, `ssize_t`, `ptrdiff_t`, `intptr_t`, `uintptr_t` | Platform/C-oriented integer type names. |
| `signed`, `unsigned`, `short`, `long` | Accepted type spellings for C-oriented declarations; exact layout follows compiler mapping and target. |

User-defined struct, class, protocol, enum, and alias names can also be used as
types. A spelling being accepted by the parser does not guarantee that each
target backend supports every operation on that type.

### Type forms

Type syntax supports pointer and reference suffixes, arrays, and generic
arguments:

```cpy
int* pointer
int** pointer_to_pointer
int& reference_type
int[] growable_array
int[8] fixed_array
Box<int> box
```

`T[]` denotes a registered/growable array form; `T[N]` is a fixed-size array
type with a numeric literal size. Pointer (`*`, `**`), reference (`&`), and
array suffixes can be composed where the compiler supports the resulting
operation. Generic type arguments use angle brackets, such as `Box<int>`.

### Variable declarations and inference

Use a type followed by a name, optionally initialized:

```cpy
int count = 3
str title = "reference"
int result
```

`auto` requests inference from an initializer and therefore requires one:

```cpy
auto answer = 42
```

Variables can also be explicitly declared `dynamic`. In ordinary semantic
analysis, reassigning a variable a value of an incompatible type can promote
that variable to a runtime-typed dynamic value; operations on it then follow
dynamic/runtime rules rather than the original static type.

`let name = expression` is accepted as an assignment/declaration-style
statement form, but does not make Cpyte dynamically typed or immutable.
Typed constants use a type and a parenthesized name:

```cpy
int (MAX_ITEMS) = 100
```

Constants are immutable. Functions may also declare a parameter as a
constant-view parameter using `(parameter)` in the parameter list; this makes
the parameter read-only within that function.

### Type aliases

```cpy
type Index = uint64
```

An alias names a type; it does not introduce a new runtime representation.
Top-level functions and type declarations are predeclared during semantic
analysis, so supported functions/types can be referenced before their textual
definitions. This does not imply that local variables are forward-declared.

### Conversions

The compiler supports C-style casts `(Type) expression` for built-in or
declared types, including pointer/array suffixes, and recognizes `sizeof(Type)`.
The `as` expression is a separate class downcast operation (see
[class casts](#structs-classes-protocols-and-enums)), not a general-purpose
numeric cast. Conversion availability and overflow behavior depend on the
source and destination types; do not assume implicit conversions preserve
range or precision.

## Expressions and operators

### Atoms and postfix expressions

Expressions include numeric and string literals, names, booleans, `null`,
parenthesized expressions, list literals (`[a, b]`), calls, indexing, member
access (`value.field`), and the operators below. Calls may be chained with
indexing and member access. A trailing comma is accepted in a list literal.

### Operator precedence

From highest to lowest (operators on the same row share precedence):

| Operators | Meaning |
|---|---|
| `(...)`, `[...]`, `.`, `as` | Grouping, call, indexing, member access, class cast |
| unary `+`, `-`, `not`, `~`, `*`, `**`, `&`, `--` | Unary operations; pointer dereference/address forms are type-dependent |
| `**` | Exponentiation |
| `*`, `/`, `//`, `%` | Multiplicative |
| `+`, `-` | Additive |
| `<<`, `>>` | Shifts |
| `<`, `<=`, `>`, `>=`, `==`, `!=`, `in` | Comparisons and membership |
| `&` | Bitwise AND |
| `^` | Bitwise XOR |
| `|` | Bitwise OR |
| `and` | Logical AND |
| `or` | Logical OR |
| `value if condition else other` | Conditional expression |

Parentheses should be used when an expression's grouping might be unclear.
The conditional expression has the form `value if condition else other`;
chained conditional expressions associate to the right. `in` is a membership
operation for supported array/list values, not a general protocol-based
iteration hook.

### Assignment

Assignment targets are names, fields, array indexes, and dereferenced values.
Supported assignment spellings are:

```cpy
value = 1
value += 2
value -= 2
value *= 3
value /= 2
value //= 2
```

The parser tokenizes `:=`, but it is not a standard assignment statement.
There is no general tuple unpacking or chained-assignment form.

### Pointer operations and allocation expressions

Address-of (`&value`), dereference (`*pointer`), `new Type`, `new Type[count]`,
and `sizeof(Type)` are parsed expressions. Their valid uses depend on type
checking, target support, and runtime configuration; see
[pointers and allocation](#pointers-allocation-and-ownership-operations).

## Statements and control flow

Every compound statement uses a colon and an indented suite. A one-line suite
is not the standard form.

### Declarations and simple statements

The parser supports variable declarations, assignment, expression statements,
`print`, `return`, `assert`, `break`, `continue`, `del`, `defer`, and
declarations of functions, classes, structs, enums, aliases, and imports.
`print expression` and `print(expression, ...)` are accepted. `print` is a
statement, not a general expression returning a value.

### Branching

```cpy
if count > 0:
    print("positive")
elif count == 0:
    print("zero")
else:
    print("negative")
```

`switch expression:` accepts `case expression:` and `default:` arms. Each
arm may contain a single same-line statement or an indented body. Do not rely
on implicit fallthrough; use explicit control flow within a case.

### Loops

```cpy
while count > 0:
    count -= 1

for item in items:
    print(item)
```

`for` binds one loop variable to a string, array, or supported dynamic
iterable. Iterating a string yields `char` values; iterating an array yields
its element type. `break` and `continue` are loop-control statements.
`range(...)` is available for counted loops; see
[built-ins](#arrays-and-built-ins).

### Assertions and return

`assert condition` checks a condition. An optional assertion message is
accepted by the parser as an expression after a comma.

`return` may be used without a value in a void-returning function, or with an
expression matching the function's return type. The compiler checks
return-type compatibility.

## Functions and calls

### Function definitions

```cpy
def add(left: int, right: int) -> int:
    return left + right
```

The parser also accepts the type-before-name parameter form (`int left`) and
omitted parameter types (which default to `int`). Return annotations use
`-> Type`; an omitted annotation is inferred/treated as `auto` by semantic
analysis. Prefer explicit parameter and return types in public APIs.

Generic parameter names may follow the function name, for example
`def identity<T>(value: T) -> T:`. Generic syntax is implemented through the
compiler's generic instantiation machinery and may be subject to type and
backend restrictions; it is not Python-style runtime generics.

### Defaults, keyword arguments, and calls

```cpy
def repeat(value: int, times: int = 2) -> int:
    return value * times

answer = repeat(value=4)
other = repeat(4, 3)
```

Once a parameter has a default, every following parameter must also have a
default. Calls support positional and named arguments; an argument cannot be
supplied twice. Variadic parameters (`*args`, `**kwargs`) are not supported.

### Method modifiers and decorators

The parser recognizes method/function modifiers including `public`,
`private`, `static`, `virtual`, and `override` in modifier-prefixed
declarations. Both `public def name(...):` and the modifier-prefixed
`public name(...):` form are parsed. Their effect depends on declaration
context and backend.

Cpyte decorators use a wrapper function whose return type is `decorated`.
`code()` invokes the original function, and `result` is the wrapper's result
slot:

```cpy
def add_offset(offset int) -> decorated:
    code()
    result = result + offset

@add_offset(10)
def value() -> int:
    return 42

public def main() -> int:
    print(value())
    return 0
```

This program prints `52`.

In a decorator factory, `args` exposes the decorated function's arguments as
dynamic values, `func_name` is its name, and assigning `skip = true` skips the
original call and returns the default value. A decorator factory returning
`decorated` must call `code()` somewhere in its body; it may conditionally skip
that call by setting `skip`. `code()`, `result`, `args`, `func_name`, and
`skip` have this special meaning only in decorator factories, not in ordinary
functions. Multiple decorators do not follow Python's chaining rule: each
invokes the original function through the trampoline, and the last decorator's
result wins.

```cpy
def guard(pad int) -> decorated:
    if pad >= 0:
        code()
        int first = args[0]
        int second = args[1]
        result = result + first + second
        print(func_name)
    else:
        skip = true

@guard(5)
def add(a int, b int) -> int:
    return a * 10 + b

@guard(-1)
def skipped() -> int:
    return 999

public def main() -> int:
    print(add(2, 3))
    print(skipped())
    return 0
```

This prints `add`, `28`, and `0`, in that order. `args[index]` is dynamic;
declare a local with the required static type to unbox an argument.

### Input and conversion built-ins

The language recognizes `input()`, `input_str()`, and `input_big()` as
input-expression forms returning `int`, `str`, and `big`, respectively.
Conversion/runtime names include `int`, `float`, `double`, and `str`; each
conversion takes exactly one argument. `free` is available when GC is
disabled. Exact supported conversions are type- and runtime-dependent.

## Arrays and built-ins

### Array creation and indexing

`new T[count]` allocates an array-sized region. A list literal such as
`[1, 2, 3]` creates a runtime list/array value. Indexing uses
`array[index]`. Fixed-size arrays use the `T[N]` type form and do not have the
registered growable-array behavior.

Array lifetime depends on how the array was created and whether automatic GC
is enabled. Raw C buffers and fixed-size arrays should not be assumed to have
the same metadata or helpers as registered arrays.

### Array and object helpers

| Built-in | Supported form and behavior |
|---|---|
| `len(value)` | One argument. Returns the registered element count for supported arrays, the string length for `str`, or invokes a class `__len__` method when supported. Fixed-size arrays and raw C buffers do not have registered array lengths. |
| `append(array, value)` | Two arguments. Mutates a growable/registered array lvalue and returns the array value. It does not grow fixed-size arrays or raw C buffers. |
| `range(stop)`, `range(start, stop)`, `range(start, stop, step)` | Produces an `int64[]` sequence for `for`; a user-defined function named `range` shadows this built-in. |
| `str_split(string[, separator])` | Accepts one or two arguments and produces a `dynamic[]`; a user-defined function named `str_split` shadows this built-in. |
| `has(name)` | Tests whether a name/value is present under the supported static/dynamic rules. |
| `has(object, "field")` | Tests whether a statically known struct/class has a field; a runtime string field name is also supported where fields can be resolved. |
| `get_attr(object, "field")` | Reads a statically typed struct/class field by name. A dynamic field-name string is only valid when the candidate fields have compatible types. |
| `isinstance(object, Class)` | Tests whether a class object's runtime type is the named class or a descendant, subject to sealed/virtual hierarchy rules. |

`has` and `get_attr` are compiler-special forms rather than unrestricted
reflection over arbitrary values.

## Structs, classes, protocols, and enums

### Structs

Struct bodies contain typed field declarations; methods are not declared in a
struct body.

```cpy
struct Point:
    int x
    int y

Point* point = new Point
point.x = 2
```

Structs can declare generic parameters, for example `struct Box<T>:`. Fields
need explicit types; a field type cannot be inferred from an initializer
because struct fields are declarations, not initialized statements.

### Classes, inheritance, and methods

```cpy
class Animal:
    str name

    def speak() -> str:
        return this.name

class Dog(Animal):
    def speak() -> str:
        return "woof"
```

Class methods receive an implicit `this` parameter. A class may have one
declared base class. An override must match the base method's parameter and
return signature (except constructors, `__init__`). `super.method(...)` calls
the nearest base implementation from within a class method.

`new ClassName(...)` allocates the class and invokes its constructor with the
provided arguments when one is available. `new ClassName` supplies no explicit
constructor arguments; the compiler checks whether the available constructor
can be called that way. Class fields require explicit types.
Use constructors to initialize class fields; field initializer expressions are
not a supported field-initialization mechanism.

Classes may be marked `sealed`; sealed class hierarchies are monomorphic and
do not provide runtime subtype information. Ordinary virtual class hierarchies
use runtime class information for supported casts and `isinstance`.

### Dataclasses

`dataclass class Name:` synthesizes missing `__init__`, `__eq__`, and `__str__`
methods from the class fields. A user-defined method with one of those names
prevents that corresponding synthesized method. `sealed dataclass class Name:`
combines the modifiers.

### Properties

Properties are class-only blocks with a required `get` accessor and optional
`set` accessor:

```cpy
class Counter:
    int value

    property count:
        get:
            return this.value
        set:
            this.value = value
```

The getter's return type determines the property type. The setter uses a
parameter named `value`. A property name cannot conflict with a class field or
method.

### Protocols and interfaces

`protocol Name:` and `interface Name:` declare the same protocol-like form.
The body can declare typed fields, methods, and properties; a protocol may
name one base protocol. This syntax does not imply a general structural
conformance or dynamic-dispatch system beyond what semantic analysis and the
selected backend support.

### Enums

```cpy
enum Color:
    Red
    Green = 4
    Blue
```

Each member may omit its value or provide an expression. The parser does not
accept methods or typed fields inside an enum body. Use the compiler's
supported enum operations rather than assuming a Python `Enum` object model.
Members are qualified by the enum name (`Color.Red`). Implicit values start at
zero and increment; an explicit constant value sets the next implicit value
to one more than that value.

### Class type tests and casts

`isinstance(value, ClassName)` requires a class object or pointer and a class
name. `value as ClassName` is a checked class downcast; its result has pointer
type `ClassName*` and is null when the runtime type is incompatible. Upcasts
within a class hierarchy are statically successful. A sealed hierarchy has
no runtime subtype check.

### Operator and special-method hooks

Semantic analysis maps binary operators to these class methods:
`+`/`__add__`, `-`/`__sub__`, `*`/`__mul__`, `/`/`__truediv__`,
`//`/`__floordiv__`, `%`/`__mod__`, `**`/`__pow__`, `<`/`__lt__`,
`<=`/`__le__`, `>`/`__gt__`, `>=`/`__ge__`, `==`/`__eq__`, and
`!=`/`__ne__`. Additional recognized hooks include `__getitem__`,
`__len__`, `__call__`, and `__str__` in their corresponding contexts. This
is a finite compiler-supported set, not Python's complete data model.

## Pointers, allocation, and ownership operations

### Allocation and raw pointers

`new T` and `new T[count]` are explicit allocation expressions. Pointer
declarations use `T*`; dereference and address-of use `*pointer` and `&value`.
`sizeof(T)` returns the target representation's size for supported types.
Bounds checks, null checks, and lifetime guarantees should not be assumed for
raw pointers.

The default compiler mode enables automatic memory management through the
userspace runtime. `--nogc` disables automatic GC and exposes `free()`; the
caller then owns allocation lifetime. Do not manually free GC-managed
allocations. `--no-userspace` removes userspace runtime support and restricts
features that depend on it.

### Borrow and move expressions

The parser accepts:

```cpy
borrow value
borrow mut value
move value
```

These are explicit ownership/borrowing expressions with semantic checks. They
are not a replacement for a complete Rust-style ownership system, and the
actual allowed operations depend on the value's type and compiler analysis.

### Deletion

`del expression` is a statement whose effect depends on the target:

- A statically bound local name is unbound in the current function; later
  references are invalid.
- A `dynamic` value is cleared to the runtime's empty value.
- A GC-managed `new` allocation is unbound and left for the collector; `del`
  must not manually free a GC interior pointer.
- A manually managed heap allocation can be released by `del`; an addressable
  slot can be cleared to its zero/null value.

Use `free(pointer)` for explicit manual release where applicable, not as a
general replacement for `del`. Do not free a GC-managed `new` allocation.

## Errors and deferred statements

### Assertions

`assert condition[, message]` checks a condition and accepts an optional
message expression.

### Try, except, and raise

```cpy
try:
    work()
except:
    recover()
```

An `except` clause may name an exception type:

```cpy
try:
    work()
except Error:
    recover()
```

A `try` requires at least one `except`; there is no parsed `finally` clause or
exception-binding (`except Error as e`) syntax. Raising uses a named exception
class and a parenthesized message:

```cpy
raise Error("operation failed")
```

Do not assume Python's built-in exception classes, traceback behavior, or
exception propagation semantics. Those depend on the runtime/compiler
implementation.

### Defer

Within a function suite, `defer statement` registers a statement for deferred
execution when the function exits. Deferred statements execute in reverse
registration order on explicit returns and at the implicit function end. For
example:

```cpy
def use_resource() -> void:
    defer close_resource()
```

Defers are not Python context managers; use them only for the compiler's
supported scope-exit behavior.

## Imports and native interoperation

### Imports

The parser accepts a module name, a quoted path/header, an SDK-qualified
module, or a package path:

```cpy
import module_name
import "path/to/header.h"
import platform sdk("/path/to/sdk")
import @vendor/package
```

Import resolution and whether a form loads Cpy source, a C header, a package,
or a platform SDK are handled by the compiler's userspace/import system.
Package and SDK availability is environment-specific.

### C declarations and embedded source

Quoted-path and SDK imports are used for C/native declarations. A `ccode:`
block embeds an indented raw C source block:

```cpy
ccode:
    /* C source supplied to the native compilation path */
```

The embedded text is passed to the native compilation machinery; it is not
type-checked as Cpyte. Include declarations and linkage requirements needed by
the surrounding code.

### LLVM IR and inline assembly

`llvm:` embeds an indented raw LLVM IR block. `unsafe llvm:` marks the
corresponding unsafe form. Inline assembly has the form `asm("template")`,
with optional operand/clobber clauses accepted by the parser. These constructs
are target-specific, bypass ordinary high-level safety/type guarantees, and
require valid syntax for the selected target/backend.

## Compiler and command-line use

Typical commands:

```text
cpy [options] program.cpy            # JIT mode is the default
cpy --aot program.cpy                 # AOT executable
cpy build [build-options] program.cpy # AOT build command
cpy format [--write|--check] file.cpy # formatter
cpy sef <subcommand> ...              # SEF tooling
```

Useful global options include:

| Option | Effect |
|---|---|
| `--help`, `--version` | Print help or compiler version. |
| `--strict` | Enable strict semantic analysis. |
| `--nogc` | Disable automatic GC and enable `free()`. |
| `--no-userspace` | Compile without the userspace runtime. |
| `--jit`, `--aot`, `--scorpion` | Select JIT, AOT, or Scorpion/SEF compilation mode. |
| `--ast`, `--emit-llvm` | Print the parsed AST or generated LLVM IR. |
| `--tab-size N` | Set indentation tab size. |
| `--cpu CPU` / `--march CPU`, `--mattr FEATURES` | Set LLVM target CPU/features. |
| `--pic`, `--lto` | Request position-independent output or link-time optimization. |
| `--opt N` | Set the optimization level where supported. |
| `--export NAME` | Export a symbol in dynamic SEF output; repeatable. `SYMBOL=WIRE_NAME` selects a published name. |
| `--no-auto-export` | Disable automatic export of public functions for Scorpion/SEF output. |
| `--require NAME=VER`, `--scope S`, `--weak NAME` | Configure dynamic SEF import requirements, export scope, and weak imports; repeatable. |
| `--lazy`, `--no-lazy` | Enable or disable lazy dynamic SEF call-site binding. |
| `--versym`, `--no-versym` | Select wide v2.1 or legacy v2.0 dynamic SEF records. |

The `build` command accepts output/debug/optimization and linking options;
`format` supports write/check modes; `sef` provides SEF pack, dump, check, size,
and digest tools. Run `cpy --help` for the exact options of the installed
version.

Two source directives are recognized at the beginning of a source file:

- `#nogc` disables automatic GC, equivalent to `--nogc`.
- `#scorpion` selects the Scorpion ABI build mode.

The compiler strips these directives before lexing. Other `#` lines are
ordinary comments.

## Unsupported or restricted constructs

The following are not part of the standard language grammar or should not be
assumed to work as their Python/C++ analogues:

- Triple-quoted strings and multiline-comment delimiters.
- Lambdas, comprehensions, generators, dictionaries, sets, tuple literals,
  unpacking, array/string slices, and variadic function parameters.
- Python exception binding, `finally`, context managers, and general
  `with`/`match` statements.
- Struct methods (methods belong to classes), multiple inheritance, and
  arbitrary runtime reflection.
- Generalized operator overloading or the full Python special-method model.
- General array resizing through `append` for fixed-size arrays or raw C
  buffers; `len` for unregistered fixed-size/raw buffers.
- Automatic safety guarantees for pointer arithmetic, bounds, nullness,
  ownership, or memory lifetime.
- A `pass` statement, public/private variable declarations, standalone
  `static` variables, or `->` member access.
- The `:=` assignment statement, despite the token being recognized.

Some syntax such as decorators, generic declarations, protocols, exceptions,
and inline native blocks has front-end support whose exact semantics or
availability can vary by target and runtime. A successful parse alone is not
proof that a feature is supported in every compilation mode.

## Grammar summary

This is a compact grammar of the standard parser forms, not a replacement for
the semantic checks described above. `suite` means `:` followed by a newline
and an indented block.

```ebnf
program          = { statement } ;
suite            = ":" NEWLINE INDENT { statement } DEDENT ;

statement        = declaration | assignment | expression NEWLINE
                 | "print" ( expression | "(" [ expression { "," expression } ] ")" )
                 | "let" expression [ assignment_operator expression ]
                 | "return" [ expression ]
                 | "assert" expression [ "," expression ]
                 | "break" | "continue" | "del" expression
                 | "if" expression suite
                     { "elif" expression suite } [ "else" suite ]
                 | "while" expression suite
                 | "for" identifier "in" expression suite
                 | "switch" expression ":" NEWLINE INDENT
                     { "case" expression case_body | "default" case_body } DEDENT
                 | "try" suite { "except" [ identifier ] suite }
                 | "raise" identifier "(" expression ")"
                 | "ccode" ":" NEWLINE INDENT STRING DEDENT
                 | "llvm" ":" NEWLINE INDENT STRING DEDENT
                 | "unsafe" "llvm" ":" NEWLINE INDENT STRING DEDENT
                 | "defer" statement ;

declaration      = variable_decl | function_decl | class_decl | struct_decl
                 | protocol_decl | enum_decl | type_alias | import_decl ;
variable_decl    = type identifier [ "=" expression ]
                 | type "(" identifier ")" "=" expression
                 | "auto" identifier "=" expression ;
assignment       = lvalue assignment_operator expression ;
lvalue           = identifier | expression "." identifier
                 | expression "[" expression "]" | "*" expression ;
function_decl    = { decorator } "def" identifier [ generic_params ]
                   "(" [ parameter { "," parameter } ] ")"
                   [ "->" type ] suite
                 | [ modifier ] "def" identifier
                   "(" [ parameter { "," parameter } ] ")" [ "->" type ] suite
                 | [ modifier ] identifier
                   "(" [ parameter { "," parameter } ] ")"
                   [ "->" type ] suite ;
parameter        = identifier [ ":" type | type ] [ "=" expression ]
                 | "(" identifier ")" [ ":" type ] [ "=" expression ]
                 | type identifier [ "=" expression ] ;
modifier         = "public" | "private" | "static" | "virtual" | "override" ;
decorator        = "@" expression NEWLINE ;
class_decl       = class_head | "sealed" [ "dataclass" ] class_head
                 | "dataclass" [ "sealed" ] class_head ;
class_head       = "class" identifier [ generic_params ]
                   [ "(" identifier ")" ] class_suite ;
class_suite      = ":" NEWLINE INDENT
                   { field_decl | function_decl | property_decl } DEDENT ;
property_decl    = "property" identifier ":" NEWLINE INDENT
                   "get" suite [ "set" suite ] DEDENT ;
struct_decl      = "struct" identifier [ generic_params ] struct_suite ;
protocol_decl    = ( "protocol" | "interface" ) identifier
                   [ generic_params ] [ "(" identifier ")" ] class_suite ;
enum_decl        = "enum" identifier ":" NEWLINE INDENT
                   { identifier [ "=" expression ] NEWLINE } DEDENT ;
type_alias       = "type" identifier "=" type ;
import_decl      = "import" ( identifier | STRING )
                   [ "sdk" "(" STRING ")" ]
                 | "import" "@" package_path ;

type             = identifier { "*" | "**" | "&" | "[]" | "[" NUMBER "]"
                               | "<" type { "," type } ">" } ;
expression       = conditional ;
conditional      = binary [ "if" binary "else" conditional ] ;
binary           = unary { binary_operator unary } ;
binary_operator  = "**" | "*" | "/" | "//" | "%"
                 | "+" | "-" | "<<" | ">>"
                 | "<" | "<=" | ">" | ">=" | "==" | "!=" | "in"
                 | "&" | "^" | "|" | "and" | "or" ;
unary            = { "+" | "-" | "not" | "~" | "*" | "&" | "--" } postfix ;
postfix          = atom { "(" [ arguments ] ")" | "[" expression "]"
                        | "." identifier | "as" identifier } ;
atom             = NUMBER | STRING | FSTRING | identifier | boolean | "null"
                 | "input" "(" ")" | "input_str" "(" ")" | "input_big" "(" ")"
                 | "(" expression ")" | "[" [ expression { "," expression } ] "]"
                 | "new" type [ "[" expression "]" ] [ "(" [ arguments ] ")" ]
                 | "sizeof" "(" type ")" | asm_expr | "borrow" [ "mut" ] expression
                 | "move" expression | cast_expr ;
cast_expr        = "(" type ")" unary ;
asm_expr         = "asm" [ "volatile" ] "(" STRING
                   [ ":" [ STRING "(" expression ")" { "," STRING "(" expression ")" } ]
                     [ ":" [ STRING "(" expression ")" { "," STRING "(" expression ")" } ]
                       [ ":" STRING { "," STRING } ] ] ] ")" ;
field_decl       = type identifier NEWLINE ;
struct_suite     = ":" NEWLINE INDENT { field_decl } DEDENT ;
case_body        = ":" ( statement | NEWLINE INDENT { statement } DEDENT ) ;
arguments        = argument { "," argument } ;
argument         = [ identifier "=" ] expression ;
assignment_operator = "=" | "+=" | "-=" | "*=" | "/=" | "//=" ;
generic_params   = "<" identifier { "," identifier } ">" ;
package_path     = identifier { "." identifier | "/" identifier } ;
boolean          = "true" | "false" | "True" | "False" ;
```

The grammar deliberately omits error recovery, extension-provided tokens, and
semantic restrictions. In particular, declarations that parse may still be
rejected by type analysis or unsupported by the selected backend.
