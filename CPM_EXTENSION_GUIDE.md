# Writing CPM Extension Packages

This guide covers how to create CPM packages that extend the Cpyte compiler
with custom keywords, syntax, type checking, code generation, and runtime code.

The extension system is built on `source/cpyte/extension_hooks.py`. A package
ships one or more Python hook files; each file must define a `get_hooks()`
function that returns a list of hook instances. The compiler never executes
package code except through these hooks.

## Package Structure

Extension packages live under `.cpm/modules/<name>/<version>/` and follow this
layout:

```
.cpm/modules/
└── my_package/
    └── 1.0.0/
        ├── package.json            # Required manifest
        ├── parser_hooks.py         # Custom syntax (optional)
        ├── semantic_hooks.py       # Type checking (optional)
        ├── codegen_hooks.py        # LLVM IR generation (optional)
        ├── runtime_hooks.py        # C runtime code (optional)
        ├── main.cpy                # Library entry point (optional)
        └── lib.ll                  # Prebuilt LLVM IR (optional)
```

Packages can be extension-only (no `.cpy` or `.ll` files) — they provide only
keywords, operators, and compiler hooks.

To install a package into a project, place its contents under the project's
`.cpm/modules/<name>/<version>/` directory (this is what CPM's `cpm install`
does). The compiler discovers every installed package at the start of a
compilation.

## The Manifest

Every package needs a `package.json` at its root.

```json
{
  "name": "my_package",
  "version": "1.0.0",
  "capabilities": {
    "keywords": ["async", "await", "defer"],
    "operators": ["~~"],
    "tags": ["@async", "@callback"],
    "macros": ["async_def"],
    "custom_types": ["Promise", "Future"]
  },
  "extensions": {
    "parser_hooks": ["parser_hooks.py"],
    "semantic_hooks": ["semantic_hooks.py"],
    "codegen_hooks": ["codegen_hooks.py"],
    "optimize_hooks": ["optimize_hooks.py"],
    "runtime_hooks": ["runtime_hooks.py"]
  },
  "dependencies": [],
  "metadata": {
    "description": "Async/await support for Cpyte",
    "author": "Your Name",
    "license": "MIT"
  }
}
```

### Required Fields

| Field | Type | Description |
|---|---|---|
| `name` | `string` | Package name |
| `version` | `string` | Semver version string |

### Capabilities

`capabilities` declares what the package introduces. The framework registers
keywords and operators with the lexer during manifest loading, before the source
is tokenized.

- **`keywords`** — Custom identifiers (must be valid Python identifiers, not reserved)
- **`operators`** — Custom operator tokens (not reserved operators)
- **`tags`** — Tags starting with `@` (e.g. `"@async"`)
- **`macros`** — Macro names (parsed but not currently invoked by the framework)
- **`custom_types`** — Type names (parsed but not currently checked by the framework)

### Extensions

`extensions` maps hook categories to Python files. Each file must exist on disk
and must define a top-level `get_hooks()` function returning a list of hook
instances (see [Hook Files](#hook-files)).

### Reserved Names

Packages **cannot** override these keywords:

`def`, `class`, `return`, `if`, `else`, `elif`, `while`, `for`, `in`, `break`,
`continue`, `public`, `private`, `static`, `virtual`, `override`, `import`,
`true`, `false`, `null`, `True`, `False`, `and`, `or`, `not`, `print`, `input`,
`input_str`, `input_big`, `switch`, `case`, `default`, `new`, `struct`,
`sizeof`, `ref`, `int64`, `uint64`, `let`, `try`, `except`, `raise`, `asm`

Packages **cannot** override these operators:

`+`, `-`, `*`, `/`, `//`, `%`, `**`, `==`, `!=`, `<`, `>`, `<=`, `>=`, `&`,
`|`, `^`, `~`, `<<`, `>>`, `&&`, `||`, `!`, `=`, `+=`, `-=`, `*=`, `/=`,
`//=`, `->`, `.`, `[`, `]`, `(`, `)`, `{`, `}`, `,`, `:`, `;`

## Hook Files

Every hook Python file must define a top-level `get_hooks()` function that
returns a list (or tuple) of hook class instances.

```python
from cpyte.extension_hooks import ParserHook

class MyParserHook(ParserHook):
    def initialize(self, context):
        pass

def get_hooks():
    return [MyParserHook("my_package", metadata=HookMetadata(name="my_package.parser", description="..."))]
```

The first positional argument is always the package name. CompilerHook's
constructor signature is:

```python
CompilerHook(package_name, *, metadata: HookMetadata | None = None)
```

`HookMetadata` is:

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Unique hook name |
| `version` | `str` | Hook version (default `"0.0.0"`) |
| `priority` | `int` | Higher priority runs first (default `0`) |
| `requires` | `tuple[str, ...]` | Names of hooks that must be registered first |
| `conflicts` | `tuple[str, ...]` | Names of hooks that cannot be registered together |
| `description` | `str` | Human-readable description |

If you construct a hook with only the package name, `metadata` defaults to
`HookMetadata(name=<package_name>)`. Because the registry keys hooks by their
metadata name and rejects duplicates, the loader derives a unique name for such
hooks from the package, defining file and class — for example
`my_package:parser_hooks:MyParserHook`. Give hooks an explicit
`HookMetadata(name=...)` when you want a stable public name.

The loader calls `initialize(context)` on every hook after loading it and
`shutdown(context)` when the compilation finishes.

### Hook Registry and Naming

`HookRegistry.register(hook, *, priority=None)` refuses to register two hooks
with the same name or a hook whose metadata declares a conflicting registered
name. Hooks are stored per `HookStage` and sorted by priority (highest first),
then package and name.

## Compiler Context

Every callback receives a `CompilerContext` — a dataclass of shared compiler
state, not a loose dictionary:

```python
CompilerContext(
    source_file: str | None = None,
    target: str | None = None,
    platform: str | None = None,
    architecture: str | None = None,
    optimization_level: int = 0,
    debug: bool = False,
    ast: Any = None,
    semantic_model: Any = None,
    llvm_module: Any = None,
    diagnostics: list[Diagnostic] = ...,
    include_paths: list[str] = ...,
    library_paths: list[str] = ...,
    libraries: list[str] = ...,
    compiler_flags: list[str] = ...,
    linker_flags: list[str] = ...,
    defines: dict[str, str | None] = ...,
    data: dict[str, Any] = ...,
)
```

Stage-specific helpers are also exposed through `data`:

| Stage | `context.data` keys |
|---|---|
| Parser | `tokens`, `pos`, `astparse` |
| Semantic | `analyzer`, `workspace_root`, `scope`, `node`, `package_dir`, `package_name` |
| Codegen | `llvm`, `module`, `builder` |
| Lifecycle | `package_dir`, `package_name` |

The semantic analyzer is also available as `context.semantic_model`.

`CompilerContext` can emit diagnostics directly:

- `context.error(message, *, code=None, location=None, package=None)`
- `context.warning(message, ...)`
- `context.note(message, ...)`

Each returns a `Diagnostic` that is appended to `context.diagnostics`.

## Diagnostics

Semantic hooks report problems by returning `Diagnostic` objects:

| Field | Type | Description |
|---|---|---|
| `severity` | `DiagnosticSeverity` | `NOTE`, `WARNING`, `ERROR`, or `FATAL` |
| `message` | `str` | Human-readable message |
| `location` | `SourceLocation` | Optional `file`/`line`/`column` span |
| `code` | `str \| None` | Optional error code (e.g. `"ASYNC001"`) |
| `package` | `str \| None` | Package that produced the diagnostic |
| `notes` | `list[str]` | Optional follow-up hints |

Use the constructors:

```python
Diagnostic.error(message, *, code=None, location=None, package=None)
Diagnostic.warning(message, *, code=None, location=None, package=None)
```

An `ERROR` or `FATAL` diagnostic fails the build; `WARNING` and `NOTE` are
displayed but non-fatal.

## The Compiler Stages

The framework defines the following `HookStage`s, in pipeline order:

```
lifecycle -> lexer -> parser -> semantic -> transform -> codegen -> optimize -> link -> runtime
```

The compiler currently consults these stages:

| Stage | Hook class | When hooks run |
|---|---|---|
| `PARSER` | `ParserHook` | Statement dispatch in the parser |
| `SEMANTIC` | `SemanticHook` | Every AST node during analysis |
| `CODEGEN` | `CodegenHook` | AST nodes during LLVM emission |
| `RUNTIME` | `RuntimeHook` | Collecting C runtime sources/libraries |
| `LIFECYCLE` | `LifecycleHook` | Compilation start/finish/error |
| `OPTIMIZE` | `OptimizeHook` | Pass-manager extension |
| `LINK` | `BuildHook` | Build configurations (flags, paths, defines) |
| `LEXER` | `LexerHook` | Additional keywords/operators |
| `TRANSFORM` | `TransformHook` | AST transformation |

The framework-level `HookDispatcher` (`get_global_hook_dispatcher()`) provides a
convenience API over the registry for stages you integrate into your own
tooling.

## Lexer Hooks

`LexerHook` contributes keywords and operators on top of what the manifest
declares, and can rewrite individual tokens.

```python
from cpyte.extension_hooks import LexerHook

class MyLexerHook(LexerHook):
    def get_additional_keywords(self) -> set[str]:
        return {"async", "await", "defer", "async_def"}

    def get_additional_operators(self) -> set[str]:
        return {"~~"}

    def should_customize_token(self, token_type, token_value) -> bool:
        return token_value == "future"

    def customize_token(self, token_type, token_value, line, column):
        return {"type": "KEYWORD", "value": "future"}
```

### Methods

| Method | Arguments | Returns | Description |
|---|---|---|---|
| `get_additional_keywords()` | — | `set[str]` | Extra keywords to recognize |
| `get_additional_operators()` | — | `set[str]` | Extra operator tokens |
| `should_customize_token(token_type, token_value)` | token fields | `bool` | Return `True` to rewrite this token |
| `customize_token(token_type, token_value, line, column)` | token fields | `dict \| None` | Return modified token data, or `None` to keep it |

## Parser Hooks

`ParserHook` adds custom statements and expressions to the language. Implement
`should_handle_statement` / `parse_statement` (and the expression counterparts)
and the parser will defer to you at the matching position.

```python
from cpyte.extension_hooks import ParserHook

class DeferParserHook(ParserHook):
    def should_handle_statement(self, tokens, pos):
        return pos < len(tokens) and tokens[pos].value == "defer"

    def parse_statement(self, tokens, pos, context):
        pos += 1  # consume 'defer'
        from cpyte.astparse import parse_statement
        node, new_pos = parse_statement(tokens, pos)
        return {"type": "defer", "body": node}, new_pos

    def initialize(self, context):
        pass

def get_hooks():
    return [DeferParserHook("my_package")]
```

### Methods

| Method | Arguments | Returns | Description |
|---|---|---|---|
| `should_handle_expression(tokens, position)` | full token list, current position | `bool` | Return `True` to take over expression parsing |
| `parse_expression(tokens, position, context)` | tokens, position, `CompilerContext` | `(node, new_pos)` | Parse and return an AST node plus updated position |
| `should_handle_statement(tokens, position)` | full token list, current position | `bool` | Return `True` to take over statement parsing |
| `parse_statement(tokens, position, context)` | tokens, position, `CompilerContext` | `(node, new_pos)` | Parse and return an AST node plus updated position |

Tokens are objects with `.type`, `.value`, `.line`, `.column` attributes. The
parser context exposes `context.data["tokens"]` and `context.data["pos"]`.

## Semantic Hooks

`SemanticHook` validates AST nodes during type checking. `visit_node` returns a
list of `Diagnostic` objects — error-capable diagnostics fail the build.

```python
from cpyte.extension_hooks import Diagnostic, SemanticHook

class PromiseTypeHook(SemanticHook):
    def should_visit_node(self, node):
        return hasattr(node, "rettype") and node.rettype == "Promise"

    def visit_node(self, node, context):
        diagnostics = []
        if hasattr(node, "name"):
            diagnostics.append(
                Diagnostic.error(
                    f"Async function '{node.name}' returns Promise",
                    code="ASYNC001",
                    package="my_package",
                )
            )
        return diagnostics

    def can_convert(self, source_type, target_type, context):
        # True -> conversion supported; False -> explicitly rejected;
        # None -> hook does not know.
        return None

    def infer_type(self, node, context):
        return None

    def initialize(self, context):
        pass

def get_hooks():
    return [PromiseTypeHook("my_package")]
```

### Methods

| Method | Arguments | Returns | Description |
|---|---|---|---|
| `should_visit_node(node)` | AST node | `bool` | Return `True` to analyze this node |
| `visit_node(node, context)` | AST node, `CompilerContext` | `list[Diagnostic]` | Validation results; `ERROR`/`FATAL` fail the build |
| `get_custom_type_rules()` | — | `dict[str, Callable]` | Custom type-checking rules keyed by type name |
| `can_convert(source_type, target_type, context)` | types, context | `bool \| None` | Conversion support query |
| `infer_type(node, context)` | AST node, context | `Any \| None` | Hook-supplied type inference |

The semantic context exposes `context.semantic_model` (the analyzer) and
`context.data["analyzer"]`, `context.data["workspace_root"]`, the current
`context.data["scope"]` and `context.data["node"]`, plus `package_dir` and
`package_name`.

## Transform Hooks

`TransformHook` rewrites AST nodes before codegen. If `should_transform` returns
`True`, `transform_node` replaces the node. `transform_module` can rewrite the
whole module.

```python
from cpyte.extension_hooks import TransformHook

class InlineTransformHook(TransformHook):
    def should_transform(self, node) -> bool:
        return getattr(node, "type", None) == "defer"

    def transform_node(self, node, context):
        node["type"] = "scoped_block"
        return node

    def transform_module(self, module, context):
        return module
```

### Methods

| Method | Arguments | Returns | Description |
|---|---|---|---|
| `should_transform(node)` | AST node | `bool` | Return `True` to transform this node |
| `transform_node(node, context)` | node, context | `Any` | Return the replacement node |
| `transform_module(module, context)` | module, context | `Any` | Return the replacement module |

## Symbol Resolution Hooks

`SymbolResolverHook` lets packages provide external symbols and imports — SDK
declarations, headers, foreign functions — without a `.cpy` file.

```python
from cpyte.extension_hooks import SymbolResolverHook

class SdkSymbolsHook(SymbolResolverHook):
    def can_resolve_symbol(self, name) -> bool:
        return name.startswith("sdk_")

    def resolve_symbol(self, name, context):
        return {"name": name, "rettype": "int64", "args": []}

    def can_resolve_import(self, name) -> bool:
        return name == "sdk"

    def resolve_import(self, name, context):
        return {"type": "cpp_import", "module": "sdk"}
```

### Methods

| Method | Arguments | Returns | Description |
|---|---|---|---|
| `can_resolve_import(name)` | import name | `bool` | Return `True` to resolve this import |
| `resolve_import(name, context)` | name, context | `Any \| None` | Resolved import descriptor |
| `can_resolve_symbol(name)` | symbol name | `bool` | Return `True` to resolve this symbol |
| `resolve_symbol(name, context)` | name, context | `Any \| None` | Resolved symbol descriptor |

## Codegen Hooks

`CodegenHook` generates custom LLVM IR for AST nodes. `should_emit_node` gates
the node, and `emit_node` returns the produced IR value. `before_codegen` /
`after_codegen` hook into the start and end of module emission.

```python
from cpyte.extension_hooks import CodegenHook

class AsyncCodegenHook(CodegenHook):
    def should_emit_node(self, node) -> bool:
        return hasattr(node, "is_async") and node.is_async

    def emit_node(self, node, builder, context):
        llvm = context.data.get("llvm")       # LLVM bytecoding instance
        module = context.data.get("module")   # llvmlite Module
        builder = context.data.get("builder") # llvmlite IRBuilder
        return llvm.emit_funcdef(node)

    def before_codegen(self, module, context):
        pass

    def after_codegen(self, module, context):
        pass

    def initialize(self, context):
        pass

def get_hooks():
    return [AsyncCodegenHook("my_package")]
```

### Methods

| Method | Arguments | Returns | Description |
|---|---|---|---|
| `should_emit_node(node)` | AST node | `bool` | Return `True` to handle IR generation for this node |
| `emit_node(node, builder, context)` | AST node, llvmlite IRBuilder, `CompilerContext` | IR value or `None` | Generate and return LLVM IR |
| `before_codegen(module, context)` | llvmlite Module, context | `None` | Run immediately before module emission |
| `after_codegen(module, context)` | llvmlite Module, context | `None` | Run immediately after module emission |

`CompilerContext.llvm_module` is set during codegen, and
`context.data["llvm"]`, `context.data["module"]` and `context.data["builder"]`
expose the codegen internals.

## Optimization Hooks

`OptimizeHook` contributes LLVM pass-manager passes and can rewrite the module.
Return `True` from `should_add_passes` to have `add_module_passes` invoked.

```python
from cpyte.extension_hooks import OptimizeHook

class AsyncOptimizeHook(OptimizeHook):
    def should_add_passes(self, context) -> bool:
        return True

    def add_module_passes(self, pass_manager, context):
        pass

    def optimize_module(self, module, context):
        return module
```

### Methods

| Method | Arguments | Returns | Description |
|---|---|---|---|
| `should_add_passes(context)` | context | `bool` | Return `True` to register passes |
| `add_module_passes(pass_manager, context)` | LLVM pass manager, context | `None` | Register custom optimization passes |
| `optimize_module(module, context)` | module, context | `Any` | Return the (possibly rewritten) module |

## Build Hooks

`BuildHook` (the `LINK` stage) collects build configuration for the program.

```python
from cpyte.extension_hooks import BuildHook

class SdkBuildHook(BuildHook):
    def get_include_paths(self, context) -> list[str]:
        return ["/opt/sdk/include"]

    def get_library_paths(self, context) -> list[str]:
        return ["/opt/sdk/lib"]

    def get_libraries(self, context) -> list[str]:
        return ["sdk"]

    def get_defines(self, context) -> dict[str, str | None]:
        return {"SDK_ENABLED": "1"}

    def get_compiler_flags(self, context) -> list[str]:
        return []

    def get_linker_flags(self, context) -> list[str]:
        return ["-Wl,--as-needed"]
```

### Methods

| Method | Arguments | Returns | Description |
|---|---|---|---|
| `get_include_paths(context)` | context | `list[str]` | Extra include directories |
| `get_library_paths(context)` | context | `list[str]` | Extra library directories |
| `get_libraries(context)` | context | `list[str]` | Libraries to link |
| `get_defines(context)` | context | `dict[str, str \| None]` | Preprocessor defines |
| `get_compiler_flags(context)` | context | `list[str]` | Extra compiler flags |
| `get_linker_flags(context)` | context | `list[str]` | Extra linker flags |

## Runtime Hooks

`RuntimeHook` injects C source code, files, and libraries into the program's
compilation. Returned code is written verbatim to a temporary `.c` file and
compiled with the program — include any headers your code needs.

```python
from cpyte.extension_hooks import RuntimeHook

class MyRuntimeHook(RuntimeHook):
    def get_runtime_code(self, context) -> str | None:
        return """
#include <stdlib.h>

typedef struct MyHandle {
    void* value;
    int is_ready;
} MyHandle;

MyHandle* create_handle() {
    MyHandle* h = malloc(sizeof(MyHandle));
    h->value = NULL;
    h->is_ready = 0;
    return h;
}
"""

    def get_runtime_files(self, context) -> list[str]:
        return []  # package-relative files to compile alongside

    def get_runtime_libraries(self, context) -> list[str]:
        return []  # extra libraries to link

    def initialize(self, context):
        pass

def get_hooks():
    return [MyRuntimeHook("my_package")]
```

### Methods

| Method | Returns | Description |
|---|---|---|
| `get_runtime_code(context)` | `str \| None` | C source code appended to the program's runtime |
| `get_runtime_files(context)` | `list[str]` | Package-relative `.c`/`.ll` files to compile in |
| `get_runtime_libraries(context)` | `list[str]` | Library names to link |

## Lifecycle Hooks

`LifecycleHook` observes the whole compilation.

```python
from cpyte.extension_hooks import LifecycleHook

class ProfilingHook(LifecycleHook):
    def before_compile(self, context):
        pass

    def after_compile(self, context):
        pass

    def on_compile_error(self, context):
        pass
```

### Methods

| Method | Arguments | Returns | Description |
|---|---|---|---|
| `before_compile(context)` | context | `None` | Called at the start of compilation |
| `after_compile(context)` | context | `None` | Called after a successful compilation |
| `on_compile_error(context)` | context | `None` | Called when compilation fails |

## Using an Extension Package

Once installed via CPM, import the package with the `@` prefix:

```cpy
import @my_package

def main():
    print("hello from extended cpyte")
```

When compilation starts, the compiler:

1. Discovers every package under `.cpm/modules/` and registers its manifest —
   keywords and operators are made available to the lexer before tokenizing.
2. Loads the importing file (and any other packages it pulls in) during
   semantic analysis, loading that package's hook files and registering the
   hooks with the global registry.
3. Makes any exported symbols from `.cpy`/`.ll` files available for use.

When a package's hooks are loaded and the package integrates into one of the
compiler-internal stages, the compiler prints a short notice:

```
▶ deep extension integration active: my_package
```

## CLI Flags

- `--no-extensions` — Disables the entire extension system
- `--no-userspace` — Implies `--no-extensions`

## Complete Example

See `examples/example_package/` for a working package and
`examples/example_package_example.cpy` for usage.

```bash
# Install the example package under this project's CPM store
mkdir -p .cpm/modules/example_package/1.0.0
cp examples/example_package/* .cpm/modules/example_package/1.0.0/

# Run the example
python entry.py examples/example_package_example.cpy
```

## Best Practices

1. **Keep hooks focused** — One hook class per concern. A single `get_hooks()`
   can return multiple hooks.
2. **Give hooks unique names** — Pass an explicit `HookMetadata(name=...)` when
   you need a stable public name; otherwise the loader derives one from the
   package, file, and class name.
3. **Use `Diagnostic`s for validation** — Return `Diagnostic.error(...)` /
   `Diagnostic.warning(...)` from `visit_node` instead of raising; the built-in
   reporter renders them with locations and error codes.
4. **Own your C code** — Runtime hook C is compiled verbatim; include the
   headers it needs (`<stdlib.h>` for `malloc`/`NULL`, etc.).
5. **Validate in manifests** — Use reserved name checks as a safety net, not
   your primary validation.
6. **Test independently** — Write `.cpy` test files that exercise your
   package's keywords and types.
7. **Name hooks clearly** — Class names should describe what they hook into
   (e.g. `AsyncParserHook`, `PromiseTypeHook`).