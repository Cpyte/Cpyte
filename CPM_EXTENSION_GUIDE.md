# CPM Extension System Integration Guide

## Overview

The Cpyte package extension system is fully integrated with the existing CPM (Cpyte Package Manager) structure. **CPM does not require any code changes** to support package extensions - the system was designed to work with the existing CPM package structure.

## What CPM Already Provides

The extension system leverages the existing CPM infrastructure:

### 1. Package Storage Structure
```
.cpm/modules/
├── package_name/
│   └── version/
│       ├── package.json         # Extension manifest (NEW)
│       ├── parser_hooks.py      # Custom syntax (NEW)
│       ├── semantic_hooks.py    # Type checking (NEW)
│       ├── codegen_hooks.py     # LLVM generation (NEW)
│       ├── runtime_hooks.py     # Runtime code (NEW)
│       ├── *.cpy                # Main entry point (EXISTING)
│       └── *.ll                 # Prebuilt LLVM IR (EXISTING)
```

### 2. Version Management
- CPM already handles multiple versions per package
- Extension system uses the latest version automatically
- Version selection works the same as for regular packages

### 3. Import Resolution
- CPM's existing import mechanism handles `@package_name` syntax
- Extension system hooks into the same import flow
- No changes needed to CPM's import resolution

## What CPM Should Document

While CPM doesn't need code changes, it would benefit from documentation updates:

### 1. Package Manifest Format
Document the `package.json` schema for extensions:

```json
{
  "name": "package_name",
  "version": "1.0.0",
  "description": "Package description",
  "capabilities": {
    "keywords": ["async", "await", "defer"],
    "operators": ["~~"],
    "types": ["Promise", "Future"]
  },
  "extensions": {
    "parser_hooks": ["parser_hooks.py"],
    "semantic_hooks": ["semantic_hooks.py"],
    "codegen_hooks": ["codegen_hooks.py"],
    "runtime_hooks": ["runtime_hooks.py"]
  }
}
```

### 2. Extension Hooks Documentation
Document the available hook types and their interfaces:

- **LexerHook**: Extend tokenization
- **ParserHook**: Add custom syntax rules
- **SemanticHook**: Custom type checking
- **CodegenHook**: Custom LLVM IR generation
- **RuntimeHook**: Runtime code injection

### 3. Extension-Only Packages
Document that packages can be extension-only (no `.cpy` or `.ll` files):
- Such packages provide only keywords, operators, and hooks
- They can still be imported successfully
- Useful for language feature extensions

### 4. Package Discovery
Document how packages are discovered and loaded:
- Pre-loading in `mainpie.py` before compilation
- Pre-loading in `analyze()` before semantic analysis
- Automatic keyword registration during lexing
- Hook loading during compilation phases

## Recommended CPM CLI Commands

CPM could add optional convenience commands (not required):

### 1. Package Initialization
```bash
cpm init my_package --extension
```
Creates a package with extension scaffolding.

### 2. Package Validation
```bash
cpm validate package.json
```
Validates the package manifest against the schema.

### 3. Package Info
```bash
cpm info @package_name
```
Shows package capabilities and extensions.

## Testing

The extension system is tested with:

- `test_extension_system.py` - Unit tests for manifest parsing, keyword registration, hook loading
- `examples/example_package/` - Complete example package
- `examples/example_package_example.cpy` - Test file using the example package

Run tests:
```bash
python test_extension_system.py
python source/cpyte/mainpie.py examples/example_package_example.cpy
```

## Security Considerations

The extension system includes:
- Manifest validation with schema checking
- Path validation to prevent directory traversal
- Safe module loading with controlled context
- Hook execution in isolated contexts

CPM should document these security features and recommend best practices for package authors.

## Conclusion

The CPM extension system is fully functional with the existing CPM infrastructure. The only work needed is documentation updates to help package authors:
1. Understand the extension capabilities
2. Write proper package manifests
3. Implement compiler hooks correctly
4. Test their extensions

No code changes to CPM are required.