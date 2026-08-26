# Writing CPM Extension Packages

This guide covers how to create CPM packages that extend the Cpyte compiler with custom keywords, syntax, type checking, code generation, and runtime code.

## Package Structure

Extension packages live under `.cpm/modules/<name>/<version>/` and follow this layout:

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

Packages can be extension-only (no `.cpy` or `.ll` files) — they provide only keywords, operators, and compiler hooks.

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

`capabilities` declares what the package introduces. The framework registers keywords and operators with the lexer during import.

- **`keywords`** — Custom identifiers (must be valid Python identifiers, not reserved)
- **`operators`** — Custom operator tokens (not reserved operators)
- **`tags`** — Tags starting with `@` (e.g. `"@async"`)
- **`macros`** — Macro names (parsed but not currently invoked by the framework)
- **`custom_types`** — Type names (parsed but not currently checked by the framework)

### Extensions

`extensions` maps hook categories to Python files. Each file must exist on disk and must define a `get_hooks()` function returning a list of hook instances.

### Reserved Names

Packages **cannot** override these keywords:

`def`, `class`, `return`, `if`, `else`, `elif`, `while`, `for`, `in`, `break`, `continue`, `public`, `private`, `static`, `virtual`, `override`, `import`, `true`, `false`, `null`, `True`, `False`, `and`, `or`, `not`, `print`, `input`, `input_str`, `input_big`, `switch`, `case`, `default`, `new`, `struct`, `sizeof`, `ref`, `int64`, `uint64`, `let`, `try`, `except`, `raise`, `asm`

Packages **cannot** override these operators:

`+`, `-`, `*`, `/`, `//`, `%`, `**`, `==`, `!=`, `<`, `>`, `<=`, `>=`, `&`, `|`, `^`, `~`, `<<`, `>>`, `&&`, `||`, `!`, `=`, `+=`, `-=`, `*=`, `/=`, `//=`, `->`, `.`, `[`, `]`, `(`, `)`, `{`, `}`, `,`, `:`, `;`

## Hook Files

Every hook Python file must define a top-level `get_hooks()` function that returns a list of hook class instances.

```python
from cpyte.extension_hooks import ParserHook

class MyParserHook(ParserHook):
    def initialize(self, context):
        pass

def get_hooks():
    return [MyParserHook("my_package")]
```

The framework calls `initialize(context)` on each hook after loading. The context dictionary contains:

```python
{
    "workspace_root": "/path/to/workspace",   # project root
    "package_dir": "/path/to/package/version", # package directory on disk
    "package_name": "my_package",             # package name string
    "analyzer": <SemanticAnalyzer instance>,   # only during semantic analysis phase
}
```

Hooks run in priority order (higher priority runs first). The default priority is `0`.

## Parser Hooks

Parser hooks add custom syntax to the language. Extend `ParserHook` and implement `should_handle_statement` / `parse_statement`.

```python
from cpyte.extension_hooks import ParserHook

class DeferParserHook(ParserHook):
    def should_handle_statement(self, tokens, pos):
        if pos < len(tokens) and tokens[pos].value == 'defer':
            return True
        return False

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
| `should_handle_statement(tokens, pos)` | Full token list, current position | `bool` | Return `True` to take over parsing at this position |
| `parse_statement(tokens, pos, context)` | Token list, position, `{"tokens": tokens, "pos": pos}` | `(node, new_pos)` | Parse and return an AST node plus updated position |

Tokens are objects with `.type`, `.value`, `.line`, `.column` attributes.

## Semantic Hooks

Semantic hooks validate AST nodes during type checking. Extend `SemanticHook` and implement `should_visit_node` / `visit_node`.

```python
from cpyte.extension_hooks import SemanticHook

class PromiseTypeHook(SemanticHook):
    def should_visit_node(self, node):
        return hasattr(node, 'rettype') and node.rettype == 'Promise'

    def visit_node(self, node, context):
        errors = []
        if hasattr(node, 'name'):
            errors.append(f"Async function '{node.name}' returns Promise")
        return errors if errors else None

    def initialize(self, context):
        pass

def get_hooks():
    return [PromiseTypeHook("my_package")]
```

### Methods

| Method | Arguments | Returns | Description |
|---|---|---|---|
| `should_visit_node(node)` | AST node | `bool` | Return `True` to analyze this node |
| `visit_node(node, context)` | AST node, `{"analyzer": <SemanticAnalyzer>, "scope": <Scope>}` | `list[str]` or `None` | Return error message list, or `None` for no errors |

## Codegen Hooks

Codegen hooks generate custom LLVM IR. Extend `CodegenHook` and implement `should_emit_node` / `emit_node`. Optionally implement `should_add_module_passes` / `add_module_passes` for custom optimization.

```python
from cpyte.extension_hooks import CodegenHook

class AsyncCodegenHook(CodegenHook):
    def should_emit_node(self, node):
        return hasattr(node, 'is_async') and node.is_async

    def emit_node(self, node, builder, context):
        llvm = context['llvm']       # LLVM bytecoding instance
        module = context['module']   # llvmlite Module
        builder = context['builder'] # llvmlite IRBuilder
        return llvm.emit_funcdef(node)

    def should_add_module_passes(self):
        return False

    def initialize(self, context):
        pass

def get_hooks():
    return [AsyncCodegenHook("my_package")]
```

### Methods

| Method | Arguments | Returns | Description |
|---|---|---|---|
| `should_emit_node(node)` | AST node | `bool` | Return `True` to handle IR generation for this node |
| `emit_node(node, builder, context)` | AST node, llvmlite IRBuilder, `{"llvm": LLVM, "module": Module, "builder": IRBuilder}` | IR value or `None` | Generate and return LLVM IR |
| `should_add_module_passes()` | — | `bool` | Return `True` to add optimization passes |
| `add_module_passes(pass_manager, context)` | LLVM pass manager, context dict | `None` | Register custom optimization passes |

## Runtime Hooks

Runtime hooks inject C source code into the compilation pipeline. Extend `RuntimeHook` and implement `get_runtime_code`.

```python
from cpyte.extension_hooks import RuntimeHook

class MyRuntimeHook(RuntimeHook):
    def get_runtime_code(self):
        return """
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

    def initialize(self, context):
        pass

def get_hooks():
    return [MyRuntimeHook("my_package")]
```

### Methods

| Method | Returns | Description |
|---|---|---|
| `get_runtime_code()` | `str` or `None` | C source code appended to the program's runtime |
| `get_runtime_libraries()` | `list[str]` | Library names to link (parsed but not currently used) |

The C code string is written to a temporary `.c` file and compiled alongside the program.

## Using an Extension Package

Once installed via CPM, import the package with the `@` prefix:

```cpy
import @my_package

def main():
    print("hello from extended cpyte")
```

The framework automatically:
1. Reads `package.json` and registers keywords/operators with the lexer
2. Loads all hook files and registers hooks with the compiler
3. Makes any exported symbols from `.cpy`/`.ll` files available for use

## CLI Flags

- `--no-extensions` — Disables the entire extension system
- `--no-userspace` — Implies `--no-extensions`

## Complete Example

See `examples/example_package/` for a working package and `examples/example_package_example.cpy` for usage.

```bash
# Run the example
cpy examples/example_package_example.cpy
```

## Best Practices

1. **Keep hooks focused** — One hook class per concern. A single `get_hooks()` can return multiple hooks.
2. **Fail gracefully** — Hooks that raise exceptions are caught and skipped. Use `visit_node` return values for validation errors instead of raising.
3. **Validate in manifests** — Use reserved name checks as a safety net, not your primary validation.
4. **Test independently** — Write `.cpy` test files that exercise your package's keywords and types.
5. **Name hooks clearly** — Class names should describe what they hook into (e.g. `AsyncParserHook`, `PromiseTypeHook`).
