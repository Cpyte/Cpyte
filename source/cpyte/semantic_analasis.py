import os
import re
import subprocess
import sys

def _get_multiarch():
    for cc in ('cc', 'gcc', 'clang'):
        try:
            r = subprocess.run([cc, '-print-multiarch'], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def _get_sdk_paths():
    """Auto-discover system include/SDK roots across macOS, Linux and Windows."""
    paths = []

    if sys.platform == 'darwin':
        try:
            sdk = subprocess.run(['xcrun', '--show-sdk-path'], capture_output=True, text=True, timeout=5)
            if sdk.returncode == 0 and sdk.stdout.strip():
                paths.append(sdk.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
        for base in (
            '/Library/Developer/CommandLineTools/SDKs',
            '/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs',
        ):
            if os.path.isdir(base):
                for entry in sorted(os.listdir(base), reverse=True):
                    full = os.path.join(base, entry)
                    if os.path.isdir(full) and entry.startswith('MacOSX') and full not in paths:
                        paths.append(full)
        for p in ('/usr/local/include', '/usr/include'):
            if os.path.isdir(p) and p not in paths:
                paths.append(p)

    elif sys.platform == 'win32':
        roots = []
        windows_sdk = os.environ.get('WindowsSdkDir')
        if windows_sdk:
            roots.append(os.path.join(windows_sdk, 'Include'))
        pf = os.environ.get('ProgramFiles(x86)') or os.environ.get('ProgramFiles')
        if pf:
            roots.append(os.path.join(pf, 'Windows Kits', '10', 'Include'))
            roots.append(os.path.join(pf, 'Windows Kits', '8.1', 'Include'))
        vctools = os.environ.get('VCToolsInstallDir')
        if vctools:
            roots.append(os.path.join(vctools, 'include'))
        for root in roots:
            if not os.path.isdir(root):
                continue
            versioned = [d for d in sorted(os.listdir(root), reverse=True)
                         if re.match(r'^\d+(\.\d+)+', d)]
            candidates = [os.path.join(root, d) for d in versioned] if versioned else [root]
            for c in candidates:
                if os.path.isdir(c) and c not in paths:
                    paths.append(c)
        for p in ('C:/msys64/usr/include', 'C:/msys2/usr/include'):
            if os.path.isdir(p) and p not in paths:
                paths.append(p)

    else:
        for p in ('/usr/local/include', '/usr/include'):
            if os.path.isdir(p) and p not in paths:
                paths.append(p)
        multiarch = _get_multiarch()
        if multiarch:
            cand = os.path.join('/usr/include', multiarch)
            if os.path.isdir(cand) and cand not in paths:
                paths.append(cand)

    return paths

from .lexar import Lexer, LexerError, register_keywords, unregister_keywords
from .astparse import (
    _loc, Number, String, FString, Variable, Call, Index, Attr,
    UnaryOp, BinOp, Assign, Return, If, FuncDef, Print, ExprStmt,
    VarDecl, Break, Continue, Switch, Import, While,
    NewExpr, Deref, AddrOf, SizeOf, StructDef, Field, Input,
    InputStr, InputBig, Signed67, Try, Raise, ExceptHandler, InlineAsm,
    EnumDef, TypeAlias, ClassDef,
    parse_file, ParseError,
)
from .clib import resolve_library, parse_header_file, parse_c_source, _framework_name_from_path
from .package_manifest import (
    ManifestParser, ManifestValidator, PackageManifest, 
    get_global_registry, reset_global_registry, iter_cpm_version_dirs
)
from .extension_hooks import HookRegistry, HookLoader, get_global_hook_registry


class Diagnostic:
    __slots__ = ('message', 'token', 'note', 'level')

    def __init__(self, message: str, token=None, note: str | None = None, level: str = 'error'):
        self.message = message
        self.token = token
        self.note = note
        self.level = level


class Reporter:
    def __init__(self, source: str):
        self.source = source
        self.lines = source.split('\n')
        self.diagnostics: list[Diagnostic] = []

    def error(self, message: str, token=None, note: str | None = None):
        self.diagnostics.append(Diagnostic(message, token, note, level='error'))

    def strict_error(self, message: str, token=None, note: str | None = None):
        self.diagnostics.append(Diagnostic(message, token, note, level='strict-error'))

    def strict_warning(self, message: str, token=None, note: str | None = None):
        self.diagnostics.append(Diagnostic(message, token, note, level='strict-warning'))

    def has_errors(self) -> bool:
        return any(d.level in ('error', 'strict-error') for d in self.diagnostics)

    def display(self) -> str:
        if not self.diagnostics:
            return ''

        parts = []
        for diag in self.diagnostics:
            parts.append(self._format(diag))

        err_count = sum(1 for d in self.diagnostics if d.level in ('error', 'strict-error'))
        warn_count = sum(1 for d in self.diagnostics if d.level == 'strict-warning')
        summary_parts = []
        if err_count:
            plural = 's' if err_count > 1 else ''
            summary_parts.append(f'{err_count} semantic error{plural}')
        if warn_count:
            plural = 's' if warn_count > 1 else ''
            summary_parts.append(f'{warn_count} strict warning{plural}')
        if summary_parts:
            parts.append('found: ' + ', '.join(summary_parts) + '.')
        return '\n'.join(parts)

    def _format(self, diag: Diagnostic) -> str:
        token = diag.token
        if token is None:
            return f'{diag.level}: {diag.message}'

        line = token.line
        col = token.column

        source_line = self.lines[line - 1] if 0 < line <= len(self.lines) else ''

        line_str = str(line)
        pad = ' ' * (len(line_str) + 1)

        parts = [
            f'{diag.level}[{line}:{col}]: {diag.message}',
            f'{pad}|',
            f' {line_str} | {source_line}',
            f'{pad}| {" " * (col - 1)}^',
        ]

        if diag.note:
            parts.append(f'{pad}| {diag.note}')

        return '\n'.join(parts)


class Symbol:
    __slots__ = ('kind', 'type', 'node', 'const_value', 'initialized')

    def __init__(self, kind: str, type_: str | None = None, node=None,
                 initialized: bool = True):
        self.kind = kind
        self.type = type_
        self.node = node
        self.const_value = None
        self.initialized = bool(initialized)


class Scope:
    def __init__(self, parent: 'Scope | None' = None):
        self.parent = parent
        self.symbols: dict[str, Symbol] = {}

    def define(self, name: str, symbol: Symbol):
        self.symbols[name] = symbol

    def undefine(self, name: str):
        self.symbols.pop(name, None)

    def lookup(self, name: str) -> Symbol | None:
        if name in self.symbols:
            return self.symbols[name]
        if self.parent is not None:
            return self.parent.lookup(name)
        return None

    def lookup_local(self, name: str) -> Symbol | None:
        return self.symbols.get(name)


def _is_literal_zero(node) -> bool:
    if not isinstance(node, Number):
        return False
    if node.inferred_type == 'float':
        return False
    try:
        return int(node.value, 0) == 0
    except (ValueError, TypeError):
        return False


def _is_compile_time_false(node) -> bool:
    if isinstance(node, Number):
        try:
            v = int(node.value, 0)
            return v == 0
        except (ValueError, TypeError):
            try:
                return float(node.value) == 0.0
            except (ValueError, TypeError):
                return False
    return False


def _is_compile_time_true(node) -> bool:
    return isinstance(node, Number) and not _is_compile_time_false(node)


# When expression inference recurses deeper than this, the analyzer switches
# to an iterative post-order replay so that arbitrarily deep expressions can
# never blow the Python recursion limit. Each nesting level costs a small
# constant number of interpreter frames, so this stays far below the default
# recursion limit (1000) even combined with statement/visit frames.
_ANALYZE_DEPTH_LIMIT = 120


class SemanticAnalyzer:
    def __init__(self, source: str, filepath: str | None = None,
                 workspace_root: str | None = None, strict: bool = False, enable_extensions: bool = True):
        self.reporter = Reporter(source)
        self.globals = Scope()
        self.current_func: FuncDef | None = None
        self.current_class: ClassDef | None = None
        self._generic_instantiations: dict[str, list[tuple]] = {}  # name -> [(type_args, ...)]
        self.locals: Scope | None = None
        self.filepath = filepath
        self._filedir = os.path.dirname(filepath) if filepath else None
        self._workspace_root = workspace_root
        self._loop_depth = 0
        self._enum_context_name: str | None = None
        self.strict = strict
        self.enable_extensions = enable_extensions
        self._loaded_packages: set[str] = set()
        self._manifest_registry = get_global_registry()
        self._hook_registry = get_global_hook_registry()
        self._analyze_depth = 0
        self._infer_memo: dict[int, str] = {}
        self._in_iterative = False
        self._lazy_imports: list[dict] = []
        self._lazy_header_cache: dict[tuple, tuple] = {}
        self._lazy_load_error: str | None = None
    
    def _load_package_manifest(self, package_dir: str, package_name: str) -> bool:
        """
        Load and register a package manifest.
        
        Args:
            package_dir: Directory containing the package
            package_name: Name of the package
            
        Returns:
            True if manifest was loaded successfully, False otherwise
        """
        if not self.enable_extensions:
            return False
            
        if package_name in self._loaded_packages:
            return True  # Already loaded
            
        manifest_path = os.path.join(package_dir, 'package.json')
        if not os.path.exists(manifest_path):
            return False  # No manifest file
            
        try:
            manifest = ManifestParser.validate_and_parse(manifest_path)
            
            # Register keywords with lexer
            if manifest.capabilities.keywords:
                register_keywords(manifest.capabilities.keywords)
            
            # Register manifest in global registry
            self._manifest_registry.register(manifest)
            
            # Load hooks if present
            if self._workspace_root:
                context = {
                    'workspace_root': self._workspace_root,
                    'package_dir': package_dir,
                    'package_name': package_name,
                    'analyzer': self,
                }
                
                all_hook_files = (
                    manifest.extensions.parser_hooks +
                    manifest.extensions.semantic_hooks +
                    manifest.extensions.codegen_hooks +
                    manifest.extensions.runtime_hooks
                )
                
                if all_hook_files:
                    HookLoader.load_hooks_from_package(
                        package_name, package_dir, all_hook_files,
                        self._hook_registry, context
                    )
            
            self._loaded_packages.add(package_name)
            
            # Register empty symbols for extension-only packages
            # This allows the import to succeed even if there's no .cpy file
            self.globals.define(package_name, Symbol('package'))
            
            return True
            
        except Exception as e:
            self.error(f"Failed to load package manifest for '{package_name}': {e}")
            return False
    
    def _load_cpm_package_manifests(self) -> None:
        """Load manifests from all CPM packages in the workspace."""
        if not self.enable_extensions or not self._workspace_root:
            return
            
        cpm_root = os.path.join(self._workspace_root, '.cpm', 'modules')
        if not os.path.isdir(cpm_root):
            return
            
        for package_name, version_dir in iter_cpm_version_dirs(cpm_root):
            # Match on the base package name so import lookups (which use the
            # final path segment, e.g. "json" for "@std/json") find it.
            base_name = package_name.rsplit('/', 1)[-1]
            
            if base_name in self._loaded_packages:
                continue
            
            self._load_package_manifest(version_dir, base_name)

    def _tok(self, node):
        if isinstance(node, dict):
            return node.get('_token')
        return getattr(node, '_token', None)

    def error(self, message: str, node=None, note: str | None = None):
        self.reporter.error(message, self._tok(node), note)

    def _strict_error(self, message: str, node=None, note: str | None = None):
        if self.strict:
            self.reporter.strict_error(message, self._tok(node), note)

    def _strict_warning(self, message: str, node=None, note: str | None = None):
        if self.strict:
            self.reporter.strict_warning(message, self._tok(node), note)

    _NUMERIC_TYPES = ('int', 'int64', 'uint64', 'float', 'double', 'big', 'char')
    _INT_TYPES = ('int', 'int64', 'uint64', 'char')
    _FLOAT_TYPES = ('float', 'double')
    _WIDE_INT_TYPES = ('int64', 'uint64')

    def _numeric_promote(self, t1: str | None, t2: str | None) -> str | None:
        """C-like usual arithmetic conversions for numeric types.

        Returns the type an arithmetic operation on ``t1`` and ``t2`` produces,
        or ``None`` if the operands are not mutually promotable.
        """
        if t1 is None or t2 is None:
            return None
        if t1 == t2:
            return t1 if t1 in self._NUMERIC_TYPES else None
        if t1 in self._FLOAT_TYPES or t2 in self._FLOAT_TYPES:
            if (t1 in self._FLOAT_TYPES or t1 in self._INT_TYPES) and \
               (t2 in self._FLOAT_TYPES or t2 in self._INT_TYPES):
                return 'double' if 'double' in (t1, t2) else 'float'
            return None
        if 'big' in (t1, t2):
            if t1 in ('int', 'int64', 'uint64', 'big') and t2 in ('int', 'int64', 'uint64', 'big'):
                return 'big'
            return None
        if t1 in self._INT_TYPES and t2 in self._INT_TYPES:
            if t1 in self._WIDE_INT_TYPES or t2 in self._WIDE_INT_TYPES:
                return 'int64'
            return 'int'
        return None

    def analyze(self, nodes: list) -> bool:
        for node in nodes:
            self._visit(node)
        if self.reporter.has_errors():
            return False
        return True

    def _infer_type(self, node):
        key = id(node)
        if key in self._infer_memo:
            return self._infer_memo[key]
        if not self._in_iterative:
            self._analyze_depth += 1
            try:
                if self._analyze_depth > _ANALYZE_DEPTH_LIMIT:
                    return self._infer_type_iterative(node)
                return self._infer_type_recursive(node)
            finally:
                self._analyze_depth -= 1
        return self._infer_type_recursive(node)

    def _infer_type_recursive(self, node):
        if isinstance(node, Number):
            # Check for hexadecimal literals (0x prefix) BEFORE float 'e' check,
            # since hex values legitimately contain the letter 'e' as a digit (0-9a-f)
            if node.value.startswith('0x') or node.value.startswith('0X'):
                try:
                    val = int(node.value, 16)
                    if val > 2**31 - 1 or val < -2**31:
                        if val <= 2**63 - 1:
                            node.inferred_type = 'int64'
                            return 'int64'
                        if val <= 2**64 - 1:
                            node.inferred_type = 'uint64'
                            return 'uint64'
                        node.inferred_type = 'big'
                        return 'big'
                    node.inferred_type = 'int'
                    return 'int'
                except ValueError:
                    pass
                node.inferred_type = 'int64'
                return 'int64'
            if '.' in node.value or 'e' in node.value or 'E' in node.value:
                node.inferred_type = 'float'
                return 'float'
            # Check for large decimal values that might need 64-bit
            try:
                val = int(node.value)
                if val > 2**31 - 1 or val < -2**31:
                    if val <= 2**63 - 1 and val >= -2**63:
                        node.inferred_type = 'int64'
                        return 'int64'
                    if val <= 2**64 - 1:
                        node.inferred_type = 'uint64'
                        return 'uint64'
                    node.inferred_type = 'big'
                    return 'big'
            except ValueError:
                pass
            # For small integers, return 'int' but allow implicit conversion to int64
            node.inferred_type = 'int'
            return 'int'

        if isinstance(node, String):
            node.inferred_type = 'str'
            return 'str'

        if isinstance(node, FString):
            for kind, payload in node.parts:
                if kind == 'expr':
                    self._infer_type(payload)
            node.inferred_type = 'str'
            return 'str'

        if isinstance(node, Variable):
            sym = self.current_scope.lookup(node.name)
            if sym is None and self._enum_context_name:
                sym = self.current_scope.lookup(f'{self._enum_context_name}.{node.name}')
            if sym is None:
                sym = self._lazy_resolve(node.name)
            if sym is None:
                if self._report_lazy_load_error(node):
                    return None
                self.error(f'use of undeclared identifier `{node.name}`', node,
                           note=f'no definition found in this scope')
                return None
            if sym.const_value is not None:
                node.const_value = sym.const_value
            if sym.kind == 'enum_member':
                node.inferred_type = 'int'
                node.const_value = sym.const_value
                return 'int'
            if sym.kind in ('enum', 'struct'):
                node.inferred_type = node.name
                return node.name
            if sym.kind == 'type_alias':
                node.inferred_type = sym.type
                return sym.type
            if sym.kind == 'variable' and not sym.initialized:
                self.error(
                    f'use of uninitialized variable `{node.name}`',
                    node,
                    note='a variable declared without an initializer holds an '
                         'unspecified value; assign one before reading it'
                )
                return None
            node.inferred_type = sym.type
            return sym.type

        if isinstance(node, BinOp):
            left_t = self._infer_type(node.left)

            if node.op.name in ('AND', 'OR'):
                if node.op.name == 'AND' and _is_compile_time_false(node.left):
                    self._infer_type(node.right)
                    node.inferred_type = 'bool'
                    return 'bool'
                if node.op.name == 'OR' and _is_compile_time_true(node.left):
                    self._infer_type(node.right)
                    node.inferred_type = 'bool'
                    return 'bool'
                right_t = self._infer_type(node.right)
                node.inferred_type = 'bool'
                return 'bool'

            right_t = self._infer_type(node.right)

            if node.op.name in ('EQ_EQ', 'NOT_EQ', 'LESS', 'GREATER', 'LESS_EQ', 'GREATER_EQ'):
                if left_t is not None and right_t is not None and left_t != right_t:
                    ok = self._numeric_promote(left_t, right_t) is not None
                    ok = ok or (left_t == 'int' and right_t.endswith('*'))
                    ok = ok or (left_t.endswith('*') and right_t == 'int')
                    ok = ok or (left_t == 'str' and (right_t.endswith('*') or right_t == 'char'))
                    ok = ok or (right_t == 'str' and (left_t.endswith('*') or left_t == 'char'))
                    ok = ok or (left_t, right_t) in (
                        ('float', 'double'), ('double', 'float'),
                        ('int', 'int64'), ('int64', 'int'),
                        ('int', 'uint64'), ('uint64', 'int'),
                        ('int64', 'uint64'), ('uint64', 'int64'),
                        ('str', 'char'), ('char', 'str'),
                        ('char', 'int'), ('int', 'char'),
                        ('char', 'int64'), ('int64', 'char'),
                    )
                    ok = ok or left_t == 'big' and right_t in ('int', 'int64', 'uint64', 'big')
                    ok = ok or right_t == 'big' and left_t in ('int', 'int64', 'uint64', 'big')
                    if not ok:
                        self.error(
                            f'incompatible types in comparison: `{left_t}` vs `{right_t}`',
                            node,
                            note=f'both sides of `{node.op.name}` must be the same type'
                        )
                if left_t == 'big' or right_t == 'big':
                    node.inferred_type = 'bool'
                    return 'bool'
                node.inferred_type = 'bool'
                return 'bool'

            if node.op.name in ('SHL', 'SHR', 'AMPERSAND', 'PIPE', 'CARET', 'PERCENT', 'SLASH_SLASH'):
                if node.op.name in ('SHL', 'SHR', 'AMPERSAND', 'PIPE', 'CARET'):
                    # Bitwise operations not supported for big
                    valid_int_types = ('int', 'int64', 'uint64')
                    if left_t == 'big' or right_t == 'big':
                        self.error(
                            f'bitwise operator `{node.op.name}` not supported for `big` operands',
                            node,
                            note=f'got `{left_t}` and `{right_t}`'
                        )
                    elif (left_t is not None and left_t not in valid_int_types) or (right_t is not None and right_t not in valid_int_types):
                        self.error(
                            f'bitwise operator `{node.op.name}` requires integer operands',
                            node,
                            note=f'got `{left_t}` and `{right_t}`'
                        )
                    if left_t in ('int64', 'uint64') or right_t in ('int64', 'uint64'):
                        node.inferred_type = 'int64'
                        return 'int64'
                    node.inferred_type = 'int'
                    return 'int'
                # PERCENT, SLASH_SLASH
                valid_int_types = ('int', 'int64', 'uint64', 'big')
                if (left_t is not None and left_t not in valid_int_types) or (right_t is not None and right_t not in valid_int_types):
                    self.error(
                        f'operator `{node.op.name}` requires integer operands',
                        node,
                        note=f'got `{left_t}` and `{right_t}`'
                    )
                if _is_literal_zero(node.right):
                    self.error(
                        f'division by zero in `{node.op.name}`',
                        node,
                        note=f'cannot divide or mod by zero'
                    )
                # Type promotion for mixed integer types
                if left_t == 'big' or right_t == 'big':
                    node.inferred_type = 'big'
                    return 'big'
                if left_t in ('int64', 'uint64') or right_t in ('int64', 'uint64'):
                    node.inferred_type = 'int64'
                    return 'int64'  # Simplified: promote to int64 for mixed operations
                node.inferred_type = 'int'
                return 'int'

            if node.op.name in ('PLUS', 'MINUS', 'STAR', 'SLASH', 'POW'):
                if left_t == 'str' and right_t == 'str' and node.op.name == 'PLUS':
                    node.inferred_type = 'str'
                    return 'str'
                if left_t == 'str' or right_t == 'str':
                    self.error(
                        f'operator `{node.op.name}` not supported for string operands',
                        node,
                        note=f'strings only support `+` (concatenation)'
                    )
                    result = left_t if left_t is not None else right_t
                    node.inferred_type = result
                    return result
                if node.op.name == 'SLASH':
                    int_types = ('int', 'int64', 'uint64')
                    if left_t in int_types and right_t in int_types and _is_literal_zero(node.right):
                        self.error(
                            f'division by zero',
                            node,
                            note=f'cannot divide integer by zero'
                        )
                if left_t is not None and right_t is not None and left_t != right_t:
                    # Usual arithmetic conversions: promote to the widest type
                    promoted = self._numeric_promote(left_t, right_t)
                    if promoted is not None:
                        node.inferred_type = promoted
                        return promoted
                    self.error(
                        f'mismatched types `{left_t}` and `{right_t}` in arithmetic expression',
                        node,
                        note=f'cannot apply `{node.op.name}` to different types'
                    )
                if left_t in ('float', 'double') or right_t in ('float', 'double'):
                    node.inferred_type = 'double' if 'double' in (left_t, right_t) else 'float'
                    return node.inferred_type
                # Return the larger integer type
                if left_t == 'big' or right_t == 'big':
                    node.inferred_type = 'big'
                    return 'big'
                if left_t in ('int64', 'uint64') or right_t in ('int64', 'uint64'):
                    node.inferred_type = 'int64'
                    return 'int64'
                node.inferred_type = 'int'
                return 'int'

            node.inferred_type = left_t
            return left_t

        if isinstance(node, UnaryOp):
            operand_t = self._infer_type(node.operand)
            if node.op.name == 'NOT':
                node.inferred_type = 'int'
                return 'int'
            if node.op.name == 'MINUS':
                valid_types = ('int', 'float', 'double', 'int64', 'uint64', 'big')
                if operand_t is not None and operand_t not in valid_types:
                    self.error(
                        f'cannot apply unary minus to `{operand_t}`',
                        node,
                        note='unary minus expects numeric type'
                    )
            if node.op.name == 'TILDE':
                valid_types = ('int', 'int64', 'uint64')
                if operand_t is not None and operand_t not in valid_types:
                    self.error(
                        f'bitwise NOT (`~`) not supported for `{operand_t}`',
                        node,
                        note='bitwise NOT expects int, int64, or uint64 operand'
                    )
            if node.op.name == 'MINUS_MINUS':
                if operand_t == 'big':
                    self.error(
                        'decrement (`--`) not supported for `big`',
                        node,
                        note='big integers do not support decrement'
                    )
            node.inferred_type = operand_t
            return operand_t

        if isinstance(node, Call):
            sym = self._resolve_callee(node.callee)
            if sym is not None:
                for arg in node.args:
                    self._infer_type(arg)
                self._check_call_args(node, sym)
                return sym.type
            return None

        if isinstance(node, Input):
            return 'int'

        if isinstance(node, InputStr):
            return 'str'

        if isinstance(node, InputBig):
            return 'big'

        if isinstance(node, Signed67):
            return 'str'

        if isinstance(node, Index):
            obj_t = self._infer_type(node.obj)
            self._infer_type(node.index)
            if obj_t and obj_t.endswith('[]'):
                return obj_t[:-2]
            if obj_t == 'str':
                return 'char'
            if obj_t and obj_t.endswith('*'):
                return obj_t[:-1]
            if obj_t is not None:
                self.error(
                    f'cannot index value of type `{obj_t}`',
                    node,
                    note='indexing requires a string or array type'
                )
            return None

        if isinstance(node, Attr):
            obj_t = self._infer_type(node.obj)
            if obj_t:
                lookup_t = obj_t[:-1] if obj_t.endswith('*') else obj_t
                sym = self.current_scope.lookup(lookup_t)
                if sym and sym.kind == 'enum':
                    member_sym = self.current_scope.lookup(f'{lookup_t}.{node.name}')
                    if member_sym and member_sym.kind == 'enum_member':
                        node._enum_member_value = member_sym.const_value
                        return 'int'
                    self.error(
                        f'enum `{lookup_t}` has no member `{node.name}`',
                        node
                    )
                    return None
                struct_sym = self.current_scope.lookup(lookup_t)
                if struct_sym and struct_sym.kind in ('struct', 'class') and struct_sym.node:
                    for field in struct_sym.node.fields:
                        if field.name == node.name:
                            return field.type_expr
                    self.error(
                        f'type `{obj_t}` has no field `{node.name}`',
                        node
                    )
                    return None
                self.error(
                    f'cannot access field `{node.name}` on non-struct type `{obj_t}`',
                    node
                )
                return None
            return None

        if isinstance(node, Deref):
            operand_t = self._infer_type(node.operand)
            if operand_t is not None and operand_t.endswith('*'):
                return operand_t[:-1]
            if operand_t is not None:
                self.error(f'cannot dereference non-pointer type `{operand_t}`', node)
            elif operand_t is None and hasattr(node.operand, '_token'):
                self.error('cannot dereference value of unknown type', node)
            return operand_t

        if isinstance(node, AddrOf):
            operand_t = self._infer_type(node.operand)
            if operand_t:
                return operand_t + '*'
            return None

        if isinstance(node, NewExpr):
            if node.size is not None:
                self._infer_type(node.size)
                return node.type_expr + '[]'
            return node.type_expr + '*'

        if isinstance(node, SizeOf):
            return 'int'

        if isinstance(node, InlineAsm):
            for _, arg_expr in node.inputs:
                self._infer_type(arg_expr)
            if node.outputs:
                return 'i64'
            return 'void'

        if isinstance(node, ExprStmt):
            return self._infer_type(node.expr)

        return None

    def _infer_children(self, node) -> list:
        """Child nodes that `_infer_type_recursive` descends into, in order."""
        if isinstance(node, BinOp):
            return [node.left, node.right]
        if isinstance(node, UnaryOp):
            return [node.operand]
        if isinstance(node, Call):
            return list(node.args)
        if isinstance(node, Index):
            return [node.obj, node.index]
        if isinstance(node, Attr):
            return [node.obj]
        if isinstance(node, Deref):
            return [node.operand]
        if isinstance(node, AddrOf):
            return [node.operand]
        if isinstance(node, NewExpr):
            return [node.size] if node.size is not None else []
        if isinstance(node, InlineAsm):
            return [arg_expr for _, arg_expr in node.inputs]
        if isinstance(node, ExprStmt):
            return [node.expr]
        return []

    def _infer_combine(self, node):
        key = id(node)
        if key in self._infer_memo:
            return self._infer_memo[key]
        self._in_iterative = True
        try:
            t = self._infer_type_recursive(node)
        finally:
            self._in_iterative = False
        self._infer_memo[key] = t
        return t

    def _infer_type_iterative(self, node):
        memo = self._infer_memo
        if id(node) in memo:
            return memo[id(node)]
        stack = [('visit', node)]
        while stack:
            kind, n = stack.pop()
            key = id(n)
            if key in memo:
                continue
            if kind == 'visit':
                children = self._infer_children(n)
                if children:
                    stack.append(('combine', n))
                    for c in reversed(children):
                        stack.append(('visit', c))
                else:
                    self._infer_combine(n)
            else:
                self._infer_combine(n)
        return memo[id(node)]

    @property
    def current_scope(self) -> Scope:
        return self.locals if self.locals is not None else self.globals

    def _resolve_callee(self, callee):
        if isinstance(callee, Variable):
            sym = self.current_scope.lookup(callee.name)
            if sym is None:
                sym = self._lazy_resolve(callee.name)
            if sym is None:
                if self._report_lazy_load_error(callee):
                    return None
                self.error(f'use of undeclared identifier `{callee.name}`', callee,
                           note='call target must be a function defined in scope')
                return None
            if sym.kind not in ('function', 'builtin_func'):
                self.error(f'`{callee.name}` is not callable', callee,
                           note=f'declared as `{sym.kind}`, not a function')
                return None
            return sym
        return None

    def _check_call_args(self, call: Call, sym: Symbol):
        expected_count = 0
        if sym.node and isinstance(sym.node, FuncDef):
            expected_count = len(sym.node.params)
        elif sym.node and isinstance(sym.node, Import):
            for fname, (_, params, vararg) in sym.node.symbols:
                if fname == call.callee.name:
                    expected_count = len(params)
                    if vararg:
                        return
                    break
        actual_count = len(call.args)
        if expected_count != actual_count:
            name = call.callee.name if isinstance(call.callee, Variable) else '?'
            self.error(
                f'wrong number of arguments to `{name}`',
                call,
                note=f'expects {expected_count}, got {actual_count}'
            )

    def _visit(self, node, scope: Scope | None = None):
        for hook in self._hook_registry.get_semantic_hooks():
            try:
                if hook.should_visit_node(node):
                    hook.visit_node(node, {'analyzer': self, 'scope': scope})
            except Exception:
                pass

        if isinstance(node, FuncDef):
            self._visit_funcdef(node, scope)
        elif isinstance(node, If):
            self._visit_if(node, scope)
        elif isinstance(node, Return):
            self._visit_return(node)
        elif isinstance(node, Assign):
            self._visit_assign(node, scope)
        elif isinstance(node, VarDecl):
            self._visit_vardecl(node, scope)
        elif isinstance(node, Print):
            self._visit_print(node)
        elif isinstance(node, Break):
            self._visit_break(node)
        elif isinstance(node, Continue):
            self._visit_continue(node)
        elif isinstance(node, While):
            self._visit_while(node, scope)
        elif isinstance(node, Switch):
            self._visit_switch(node, scope)
        elif isinstance(node, ExprStmt):
            self._infer_type(node.expr)
        elif isinstance(node, Import):
            self._visit_import(node)
        elif isinstance(node, StructDef):
            self._visit_struct(node, scope)
        elif isinstance(node, ClassDef):
            self._visit_class(node, scope)
        elif isinstance(node, EnumDef):
            self._visit_enum(node, scope)
        elif isinstance(node, TypeAlias):
            self._visit_type_alias(node, scope)
        elif isinstance(node, Try):
            self._visit_try(node, scope)
        elif isinstance(node, Raise):
            self._visit_raise(node)
        elif isinstance(node, Deref):
            self._infer_type(node)
        elif isinstance(node, AddrOf):
            self._infer_type(node)
        elif isinstance(node, NewExpr):
            self._infer_type(node)
        elif isinstance(node, SizeOf):
            pass
        elif isinstance(node, InlineAsm):
            for _, var_expr in node.outputs:
                if isinstance(var_expr, Variable):
                    sym = self.current_scope.lookup(var_expr.name)
                    if sym is not None:
                        sym.initialized = True
            for _, var_expr in node.outputs:
                self._infer_type(var_expr)
            for _, arg_expr in node.inputs:
                self._infer_type(arg_expr)
        elif isinstance(node, Input):
            pass
        elif isinstance(node, InputStr):
            pass
        elif isinstance(node, InputBig):
            pass
        elif isinstance(node, Signed67):
            pass
        elif isinstance(node, dict):
            self._visit_dict(node, scope)
        elif isinstance(node, (list, tuple)):
            for n in node:
                self._visit(n, scope)

    def _resolve_module_path(self, module: str, sdk_path: str | None = None) -> str | None:
        candidates = [module]
        if self._filedir:
            candidates.append(os.path.normpath(os.path.join(self._filedir, module)))
        if self._workspace_root:
            candidates.append(os.path.normpath(os.path.join(self._workspace_root, module)))
        if module.endswith('.h') and '/' in module:
            parts = module.split('/')
            framework_name = parts[0]
            header_path = '/'.join(parts[1:])
            sdk_paths = [sdk_path] if sdk_path else []
            sdk_paths.extend(_get_sdk_paths())
            for sdk in sdk_paths:
                for framework_dir in (
                    os.path.join(sdk, 'System/Library/Frameworks', f'{framework_name}.framework', 'Headers'),
                    os.path.join(sdk, 'System/Library/Frameworks', f'{framework_name}.framework', 'Versions/A/Headers'),
                ):
                    candidate = os.path.join(framework_dir, header_path)
                    if candidate not in candidates:
                        candidates.append(candidate)
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return None

    def _visit_import(self, node: Import):
        module = node.module

        is_file_import = (module.endswith('.c') or module.endswith('.cc')
                          or module.endswith('.h') or module.endswith('.cpy')
                          or module.startswith('"'))

        if not is_file_import:
            if self._try_cpm_import(node, module):
                return

        if module.startswith('@'):
            self.error(f"package `{module}` not installed — run 'cpm install'", node)
            return

        resolved = self._resolve_module_path(module, node.sdk_path)

        if not is_file_import:
            result = resolve_library(module)
            if result is None:
                self.error(f'unknown library `{module}`', node)
            else:
                symbols, kind = result
                self._register_import_symbols(symbols, node)
            return

        if resolved is None:
            note = None
            if '/' in module and not node.sdk_path and not _get_sdk_paths():
                note = ('no C SDK found on this system — install Xcode Command Line '
                        'Tools, or point cpy at one with sdk("...")')
            self.error(f'file not found: `{module}`', node, note=note)
            return

        search_paths = [node.sdk_path] if node.sdk_path else []
        search_paths.extend(_get_sdk_paths())

        if module.endswith('.c') or module.endswith('.cc'):
            result = parse_c_source(resolved)
            if result:
                self._register_import_symbols(result[0], node)
            node.src_file = resolved
        elif module.endswith('.h'):
            framework = _framework_name_from_path(resolved)
            if framework:
                node.frameworks.append(framework)
            node.symbols = []
            node.var_names = set()
            self._lazy_imports.append({
                'node': node,
                'path': resolved,
                'search_paths': search_paths,
                'loaded': False,
            })
            return
        elif module.endswith('.cpy'):
            result = self._import_cpy(resolved, node)
            if result:
                self._register_import_symbols(result[0], node)
        elif '/' in module:
            ext = module.rsplit('.', 1)[-1] if '.' in module else ''
            if ext in ('c', 'cc'):
                result = parse_c_source(resolved)
                if result:
                    self._register_import_symbols(result[0], node)
                node.src_file = resolved
            elif ext == 'cpy':
                result = self._import_cpy(resolved, node)
                if result:
                    self._register_import_symbols(result[0], node)
            else:
                framework = _framework_name_from_path(resolved)
                if framework:
                    node.frameworks.append(framework)
                node.symbols = []
                node.var_names = set()
                self._lazy_imports.append({
                    'node': node,
                    'path': resolved,
                    'search_paths': search_paths,
                    'loaded': False,
                })
                return
        else:
            result = None

        if result is None:
            self.error(f'unknown library `{module}`', node)

    def _register_import_symbols(self, symbols, node, var_names=None):
        s = self.globals
        var_names = var_names or set()
        node.var_names = var_names
        for fname, (ret_type, params, vararg) in symbols.items():
            existing = s.lookup_local(fname)
            if not existing:
                kind = 'variable' if fname in var_names else 'function'
                s.define(fname, Symbol(kind, ret_type, node))
        node.symbols = list(symbols.items())

    def _register_import_constants(self, constants, node):
        s = self.globals
        for name, val in constants.items():
            existing = s.lookup_local(name)
            if not existing:
                sym = Symbol('variable', 'int', node)
                sym.const_value = val
                s.define(name, sym)

    def _lazy_resolve(self, name):
        """Resolve `name` from a lazily-imported header on first use.

        Headers are parsed on demand (once per analyzer, cached) and only the
        symbol actually referenced is registered, so `import "Framework.h"`
        pulls in just the bytecode the program uses.
        """
        if not self._lazy_imports:
            return None
        for entry in self._lazy_imports:
            if not entry.get('loaded'):
                cache = self._lazy_header_cache
                key = (entry['path'], tuple(entry['search_paths'] or ()))
                if key not in cache:
                    try:
                        result = parse_header_file(entry['path'], entry['search_paths'])
                    except Exception as e:
                        entry['loaded'] = True
                        if self._lazy_load_error is None:
                            self._lazy_load_error = (
                                f'could not load imported header `{entry["path"]}`: {e}')
                        continue
                    symbols, _kind, constants, frameworks, var_names = result
                    cache[key] = (symbols, constants, var_names, frameworks)
                (entry['symbols'], entry['constants'],
                 entry['var_names'], entry['frameworks']) = cache[key]
                entry['loaded'] = True
                node = entry['node']
                for fw in entry.get('frameworks', ()):
                    if fw and fw not in node.frameworks:
                        node.frameworks.append(fw)
            symbols = entry.get('symbols', {})
            if name in symbols:
                return self._lazy_register_used(
                    entry['node'], name, symbols[name], entry.get('var_names', set()))
            constants = entry.get('constants', {})
            if name in constants:
                return self._lazy_register_const(entry['node'], name, constants[name])
        return None

    def _report_lazy_load_error(self, node) -> bool:
        """Report a header load/parse failure once, at the first use site.

        Returns True if a load error was reported (caller should skip its own
        'undeclared identifier' diagnostic since the real root cause is the
        broken import).
        """
        if self._lazy_load_error is None:
            return False
        self.error(self._lazy_load_error, node,
                   note='the imported header could not be loaded or parsed')
        self._lazy_load_error = None
        return True

    def _lazy_register_used(self, node, name, entry, var_names=()):
        existing = self.globals.lookup_local(name)
        if existing is not None:
            return existing
        ret_type, params, vararg = entry
        kind = 'variable' if name in var_names else 'function'
        sym = Symbol(kind, ret_type, node)
        self.globals.define(name, sym)
        if node.symbols is None:
            node.symbols = []
        if not any(fname == name for fname, _ in node.symbols):
            node.symbols.append((name, entry))
        if kind == 'variable':
            if node.var_names is None:
                node.var_names = set()
            node.var_names.add(name)
        return sym

    def _lazy_register_const(self, node, name, value):
        existing = self.globals.lookup_local(name)
        if existing is not None:
            return existing
        sym = Symbol('variable', 'int', node)
        sym.const_value = value
        self.globals.define(name, sym)
        return sym

    def _find_package_entry(self, search_dir: str, pkg_name: str) -> str | None:
        for dir_candidate in (search_dir, os.path.join(search_dir, 'src')):
            if not os.path.isdir(dir_candidate):
                continue
            for entry_name in (f'{pkg_name}.cpy', 'package.cpy'):
                entry_path = os.path.join(dir_candidate, entry_name)
                if os.path.isfile(entry_path):
                    return entry_path
        return None

    def _check_llvm_version(self, pkg_dir: str) -> None:
        pkg_toml = os.path.join(pkg_dir, 'package.toml')
        if not os.path.isfile(pkg_toml):
            return
        try:
            import tomllib
            with open(pkg_toml, 'rb') as f:
                data = tomllib.load(f)
        except Exception:
            return
        meta = data.get('package', {})
        if not meta.get('prebuilt', False):
            return
        expected = meta.get('llvm_version', '')
        if not expected:
            return
        import llvmlite.binding as llvm
        v = llvm.llvm_version_info
        actual = f'{v[0]}.{v[1]}.{v[2]}'
        if actual != expected:
            print(
                f"WARNING: package `{meta.get('name', '?')}` was prebuilt with LLVM {expected}, "
                f"current LLVM is {actual} — mismatch may cause errors",
                file=sys.stderr
            )

    def _import_prebuilt(self, ll_dir: str, node: Import):
        self._check_llvm_version(ll_dir)
        ll_files = []
        symbols = {}
        for root, _dirs, files in os.walk(ll_dir):
            for f in sorted(files):
                if not f.endswith('.ll'):
                    continue
                path = os.path.join(root, f)
                with open(path) as fh:
                    content = fh.read()
                for m in re.finditer(r'^\s*define\s+.*?@(?:"(\w+)"|(\w+))\s*\(([^)]*)\)', content, re.MULTILINE):
                    func_name = m.group(1) or m.group(2)
                    params_str = m.group(3).strip()
                    param_count = len([p for p in params_str.split(',') if p.strip()]) if params_str else 0
                    if func_name not in symbols:
                        symbols[func_name] = ('int', [(f'p{i}', 'int') for i in range(param_count)], False)
                ll_files.append(path)
        if symbols:
            node.prebuilt_ll_files = ll_files
            self._register_import_symbols(symbols, node)
            return symbols, 'prebuilt'
        return None

    def _try_cpm_import(self, node: Import, module: str) -> bool:
        # Check if package was already loaded via manifest
        # Handle both @package.name and package.name formats
        pkg_name = module.lstrip('@').rsplit('/', 1)[-1]
        if pkg_name in self._loaded_packages:
            # Package was loaded via manifest, register empty symbols and succeed
            self._register_import_symbols({}, node)
            return True
            
        cpm_root = None
        if self._workspace_root:
            cpm_root = os.path.join(self._workspace_root, '.cpm', 'modules')
        elif self._filedir:
            cpm_root = os.path.join(self._filedir, '..', '.cpm', 'modules')
        if cpm_root and os.path.isdir(cpm_root):
            pkg_dir = os.path.join(cpm_root, module.lstrip('@'))
            if os.path.isdir(pkg_dir):
                versions = sorted([d for d in os.listdir(pkg_dir) if os.path.isdir(os.path.join(pkg_dir, d))], reverse=True)
                if versions:
                    pkg_name = module.lstrip('@').rsplit('/', 1)[-1]
                    version_dir = os.path.join(pkg_dir, versions[0])
                    
                    # Load package manifest if extensions are enabled
                    manifest_loaded = False
                    if self.enable_extensions:
                        manifest_loaded = self._load_package_manifest(version_dir, pkg_name)
                    
                    cpy_file = self._find_package_entry(version_dir, pkg_name)
                    if cpy_file:
                        result = self._import_cpy(cpy_file, node)
                        if result:
                            self._register_import_symbols(result[0], node)
                        return True
                    result = self._import_prebuilt(version_dir, node)
                    if result:
                        return True
                    
                    # If no .cpy or prebuilt files but manifest was loaded, allow the import
                    # This supports extension-only packages
                    if manifest_loaded:
                        # Register empty symbols for extension-only packages
                        self._register_import_symbols({}, node)
                        return True
        return False

    def _import_cpy(self, module: str, node: Import | None = None):
        try:
            with open(module) as f:
                source = f.read()
        except FileNotFoundError:
            self.error(f'file not found: `{module}`')
            return None

        lex = Lexer(source)
        try:
            tokens = lex.get_tokens()
        except LexerError as e:
            self.error(f'lex error in imported `{module}`: {e}')
            return None

        try:
            imported_ast, _ = parse_file(tokens)
        except ParseError as e:
            self.error(f'parse error in imported `{module}`: {e}')
            return None

        # Run semantic analysis on imported file
        sub = SemanticAnalyzer(source, filepath=module, workspace_root=self._workspace_root)
        if not sub.analyze(imported_ast):
            self.error(f'imported file `{module}` has semantic errors')
            return None

        # Extract public functions and structs
        symbols = {}
        sub_ast = []
        for ast_node in imported_ast:
            if isinstance(ast_node, FuncDef) and ast_node.visibility == 'public':
                params = [(name, ptype) for name, ptype in ast_node.params.items()]
                ret_type = ast_node.rettype or 'int'
                symbols[ast_node.name] = (ret_type, params, False)
                sub_ast.append(ast_node)
            elif isinstance(ast_node, StructDef):
                sub_ast.append(ast_node)
                existing = self.globals.lookup_local(ast_node.name)
                if not existing:
                    self.globals.define(ast_node.name, Symbol('struct', None, ast_node))
            elif isinstance(ast_node, Import):
                for fname, (ret_type, params, vararg) in ast_node.symbols:
                    if fname not in symbols:
                        symbols[fname] = (ret_type, params, vararg)

        if node is not None:
            node.sub_ast = sub_ast
        return symbols, 'cpy'

    def _visit_dict(self, node: dict, scope: Scope | None = None):
        t = node.get('type')
        if t == 'while':
            self._visit_while(node, scope)
        elif t == 'for':
            self._visit_for(node, scope)

    def _visit_funcdef(self, node: FuncDef, scope: Scope | None = None):
        s = scope or self.globals
        existing = s.lookup_local(node.name)
        if existing:
            self.error(f'redefinition of `{node.name}`', node,
                       note='a function with this name already exists in this scope')
            return

        sym = Symbol('function', node.rettype or 'void', node)
        s.define(node.name, sym)

        old_func = self.current_func
        self.current_func = node
        old_locals = self.locals
        self.locals = Scope(s)

        const_params = set(getattr(node, 'const_params', None) or ())
        for param_name, param_type in node.params.items():
            if param_name in const_params:
                # Constant-view parameter: read-only inside the function, but the
                # caller's value stays fully modifiable outside.
                self.locals.define(param_name, Symbol('const_view', param_type or None, node))
            else:
                self.locals.define(param_name, Symbol('variable', param_type or None, node))

        for stmt in node.body:
            self._visit(stmt, self.locals)

        self.current_func = old_func
        self.locals = old_locals

    def _visit_if(self, node: If, scope: Scope | None = None):
        self._infer_type(node.cond)
        old_locals = self.locals
        body_scope = Scope(scope or self.current_scope)
        self.locals = body_scope
        for stmt in node.body:
            self._visit(stmt, body_scope)
        if node.orelse:
            else_scope = Scope(scope or self.current_scope)
            self.locals = else_scope
            for stmt in node.orelse:
                self._visit(stmt, else_scope)
        self.locals = old_locals

    def _visit_return(self, node: Return):
        if node.value is not None:
            val_type = self._infer_type(node.value)
            if self.current_func:
                expected = self.current_func.rettype
                if expected and val_type and expected != val_type:
                    valid_conversions = [
                        ('int', 'int64'), ('int', 'uint64'),
                        ('int64', 'int'), ('uint64', 'int'),
                        ('int64', 'uint64'), ('uint64', 'int64'),
                        ('float', 'double'), ('double', 'float'),
                        ('str', 'char*'), ('char*', 'str'),
                        ('str', 'char'), ('char', 'str'),
                        ('int', 'big'), ('int64', 'big'), ('uint64', 'big'),
                        ('big', 'big'),
                    ]
                    ok = (val_type, expected) in valid_conversions
                    ok = ok or (val_type == 'int' and expected.endswith('*'))
                    ok = ok or (val_type == 'str' and (expected.endswith('*') or expected == 'char'))
                    ok = ok or (expected == 'str' and (val_type.endswith('*') or val_type == 'char'))
                    if not ok:
                        self.error(
                            f'return type `{val_type}` does not match declared return type `{expected}`',
                            node,
                            note=f'in function `{self.current_func.name}`'
                        )
        else:
            if self.current_func and self.current_func.rettype and self.current_func.rettype != 'void':
                self.error(
                    f'missing return value in function returning `{self.current_func.rettype}`',
                    node,
                    note=f'function `{self.current_func.name}` expects a return value of type `{self.current_func.rettype}`'
                )

    def _visit_assign(self, node: Assign, scope: Scope | None = None):
        val_type = self._infer_type(node.value)
        if isinstance(node.target, (Variable, str)):
            name = node.target.name if isinstance(node.target, Variable) else node.target
            s = scope or self.current_scope
            existing = s.lookup_local(name)
            if existing is None:
                existing = s.lookup(name)
            if existing is None:
                s.define(name, Symbol('variable', val_type, node))
            elif existing.kind == 'const':
                self.error(f'cannot assign to constant `{name}`', node,
                           note='constants are immutable once declared; '
                                'declare a variable with `{type} {name} = ...` if you need to reassign it')
            elif existing.kind == 'const_view':
                self.error(f'cannot assign to constant-view parameter `{name}`', node,
                           note='constant-view parameters are read-only inside the function; '
                                'copy to a local variable to modify the value')
            elif val_type is not None and existing.type is not None and val_type != existing.type:
                # Allow implicit conversion from int to int64/uint64
                # Allow implicit conversion between int64 and uint64
                # Allow int literal 0 as null for any pointer type
                # Allow float/double interchange (same LLVM type)
                # Allow str/char* interchange (same LLVM type)
                if existing.kind == 'variable':
                    existing.initialized = True
                narrowing = {
                    ('int64', 'int'), ('uint64', 'int'),
                    ('double', 'float'),
                }
                valid_conversions = [
                    ('int', 'int64'), ('int', 'uint64'),
                    ('int64', 'int'), ('uint64', 'int'),
                    ('int64', 'uint64'), ('uint64', 'int64'),
                    ('float', 'double'), ('double', 'float'),
                    ('str', 'char'), ('char', 'str'),
                    ('int', 'big'), ('int64', 'big'), ('uint64', 'big'),
                    ('big', 'big'),
                ]
                ok = (val_type, existing.type) in valid_conversions
                ok = ok or (val_type == 'int' and existing.type.endswith('*'))
                ok = ok or (val_type == 'str' and (existing.type.endswith('*') or existing.type == 'char'))
                ok = ok or (existing.type == 'str' and (val_type.endswith('*') or val_type == 'char'))
                if ok and (val_type, existing.type) in narrowing:
                    self._strict_error(
                        f'narrowing conversion from `{val_type}` to `{existing.type}` in assignment',
                        node
                    )
                if ok and (val_type == 'int' and existing.type.endswith('*')):
                    self._strict_warning(
                        f'implicit int-to-pointer conversion in assignment to `{existing.type}`',
                        node,
                        note='use 0 literal for null pointer'
                    )
                if not ok:
                    self.error(
                        f'cannot assign `{val_type}` to variable `{name}` of type `{existing.type}`',
                        node
                    )
            elif existing.kind == 'variable':
                existing.initialized = True
        elif isinstance(node.target, Attr):
            obj = node.target.obj
            if isinstance(obj, Variable):
                s = scope or self.current_scope
                obj_sym = s.lookup_local(obj.name)
                if obj_sym is None:
                    obj_sym = s.lookup(obj.name)
                if obj_sym is not None and obj_sym.kind == 'variable':
                    obj_sym.initialized = True
            obj_t = self._infer_type(node.target.obj)
            if obj_t:
                lookup_t = obj_t[:-1] if obj_t.endswith('*') else obj_t
                struct_sym = scope.lookup(lookup_t) if scope else self.current_scope.lookup(lookup_t)
                if struct_sym and struct_sym.kind == 'struct' and struct_sym.node:
                    for field in struct_sym.node.fields:
                        if field.name == node.target.name and val_type is not None and field.type_expr != val_type:
                            ok = (val_type == 'int' and field.type_expr.endswith('*'))
                            ok = ok or (val_type == 'void*' and field.type_expr.endswith('*'))
                            ok = ok or (field.type_expr == 'void*' and val_type.endswith('*'))
                            ok = ok or (val_type == 'str' and (field.type_expr.endswith('*') or field.type_expr == 'char'))
                            ok = ok or (field.type_expr == 'str' and (val_type.endswith('*') or val_type == 'char'))
                            ok = ok or (val_type, field.type_expr) in (
                                ('int', 'int64'), ('int', 'uint64'),
                                ('int64', 'uint64'), ('uint64', 'int64'),
                                ('float', 'double'), ('double', 'float'),
                                ('str', 'char'), ('char', 'str'),
                            )
                            if not ok:
                                self.error(
                                    f'cannot assign `{val_type}` to field `{node.target.name}` of type `{field.type_expr}`',
                                    node
                                )
                            break
        else:
            self._infer_type(node.target)

    def _visit_vardecl(self, node: VarDecl, scope: Scope | None = None):
        val_type = self._resolve_type_alias(node.var_type)
        node.var_type = val_type
        if val_type:
            self._check_generic_type(val_type)
        s = scope or self.current_scope
        existing = s.lookup_local(node.name)
        if existing:
            self.error(f'redeclaration of `{node.name}`', node,
                       note=f'variable already exists in this scope')
            return
        if node.is_const and node.init is None:
            self.error(f'constant `{node.name}` requires an initializer', node,
                       note=f'declare it as `{node.var_type} ({node.name}) = <value>`')
        if node.init is not None:
            init_type = self._infer_type(node.init)
            if init_type is not None and val_type is not None and init_type != val_type:
                # Allow implicit conversion from int to int64/uint64
                # Allow implicit conversion between int64 and uint64
                # Allow int literal 0 as null for any pointer type
                # Allow float/double interchange
                # Allow str/char* interchange
                # Allow char to int (widening)
                narrowing = {
                    ('int64', 'int'), ('uint64', 'int'),
                    ('double', 'float'),
                }
                valid_conversions = [
                    ('int', 'int64'), ('int', 'uint64'),
                    ('int64', 'int'), ('uint64', 'int'),
                    ('int64', 'uint64'), ('uint64', 'int64'),
                    ('float', 'double'), ('double', 'float'),
                    ('str', 'char'), ('char', 'str'),
                    ('char', 'int'),
                    ('int', 'big'), ('int64', 'big'), ('uint64', 'big'),
                    ('big', 'big'),
                ]
                ok = (init_type, val_type) in valid_conversions
                ok = ok or (init_type == 'int' and val_type.endswith('*'))
                ok = ok or (init_type == 'str' and (val_type.endswith('*') or val_type == 'char'))
                ok = ok or (val_type == 'str' and (init_type.endswith('*') or init_type == 'char'))
                if ok and (init_type, val_type) in narrowing:
                    self._strict_error(
                        f'narrowing conversion from `{init_type}` to `{val_type}` in variable declaration',
                        node
                    )
                if ok and (init_type == 'int' and val_type.endswith('*')):
                    self._strict_warning(
                        f'implicit int-to-pointer conversion in declaration of `{val_type}`',
                        node,
                        note='use 0 literal for null pointer'
                    )
                if not ok:
                    self.error(
                        f'cannot initialize `{val_type}` variable with value of type `{init_type}`',
                        node
                    )
        kind = 'const' if node.is_const else 'variable'
        is_global = s is self.globals
        s.define(node.name, Symbol(kind, val_type, node,
                                   initialized=(node.init is not None or is_global)))

    def _visit_break(self, node: Break):
        if self._loop_depth == 0:
            self.error('break outside loop', node)

    def _visit_continue(self, node: Continue):
        if self._loop_depth == 0:
            self.error('continue outside loop', node)

    def _visit_switch(self, node: Switch, scope: Scope | None = None):
        self._infer_type(node.value)
        for val, body in node.cases:
            if val is not None:
                self._infer_type(val)
            for stmt in body:
                self._visit(stmt, scope)

    def _visit_print(self, node: Print):
        self._infer_type(node.value)

    def _visit_class(self, node: ClassDef, scope: Scope | None = None):
        s = scope or self.globals
        class_sym = Symbol('class', node.name, node)
        s.define(node.name, class_sym)
        class_scope = Scope(s)

        # Resolve base class
        base_sym = None
        if node.base:
            base_sym = s.lookup(node.base)
            if base_sym is None or base_sym.kind != 'class':
                self.error(f'base class `{node.base}` not found', node)
            elif base_sym.node and isinstance(base_sym.node, ClassDef):
                # Copy base class fields (for memory layout)
                for f in base_sym.node.fields:
                    class_scope.define(f.name, Symbol('field', f.type_expr, f))
                # Copy base class methods
                for m in base_sym.node.methods:
                    existing = class_scope.lookup_local(m.name)
                    if not existing:
                        sym = Symbol(m.visibility or 'public', m.rettype or 'void', m)
                        class_scope.define(m.name, sym)

        # Register fields in class scope
        for f in node.fields:
            existing = class_scope.lookup_local(f.name)
            if existing:
                class_scope.undefine(f.name)
            class_scope.define(f.name, Symbol('field', f.type_expr, f))

        # Visit methods (second pass: analyze bodies, which registers signatures)
        old_class = self.current_class
        self.current_class = node
        for m in node.methods:
            # Inject 'this' parameter implicitly
            m.params = {'this': node.name + '*', **m.params}
            self._visit_funcdef(m, class_scope)
        self.current_class = old_class

    def _visit_try(self, node: Try, scope: Scope | None = None):
        for stmt in node.body:
            self._visit(stmt, scope)
        for handler in node.handlers:
            if handler.type_name:
                handler_sym = self.current_scope.lookup(handler.type_name)
                if handler_sym is None:
                    self.error(f'undefined exception type `{handler.type_name}`', handler)
            for stmt in handler.body:
                self._visit(stmt, scope)

    def _visit_raise(self, node: Raise):
        sym = self.current_scope.lookup(node.exc_type)
        if sym is None:
            self.error(f'undefined exception class `{node.exc_type}`', node)
        self._infer_type(node.message)

    def _visit_struct(self, node: StructDef, scope: Scope | None = None):
        s = scope or self.globals
        existing = s.lookup_local(node.name)
        if existing:
            self.error(f'redefinition of struct `{node.name}`', node)
            return
        s.define(node.name, Symbol('struct', None, node))
        struct_scope = Scope(s)
        for param in node.generic_params:
            struct_scope.define(param, Symbol('type_param', None, node))
        for field in node.fields:
            struct_scope.define(field.name, Symbol('field', field.type_expr, node))

    def _visit_enum(self, node: EnumDef, scope: Scope | None = None):
        s = scope or self.globals
        existing = s.lookup_local(node.name)
        if existing:
            self.error(f'redefinition of enum `{node.name}`', node)
            return
        s.define(node.name, Symbol('enum', None, node))
        prev_ctx = self._enum_context_name
        self._enum_context_name = node.name
        next_val = 0
        try:
            for member in node.members:
                if member['value'] is not None:
                    self._infer_type(member['value'])
                    val = self._eval_const_expr(member['value'], s)
                    member['_const_value'] = val
                    if isinstance(val, int):
                        next_val = val + 1
                else:
                    member['_const_value'] = next_val
                    member['_auto_index'] = next_val
                    next_val += 1
                sym = Symbol('enum_member', node.name, node)
                sym.const_value = member['_const_value']
                s.define(f'{node.name}.{member["name"]}', sym)
        finally:
            self._enum_context_name = prev_ctx

    def _check_generic_type(self, type_str: str):
        """If type_str is a generic instantiation like 'Pair<int, string>',
        record that the base struct needs monomorphization."""
        if not type_str or '<' not in type_str:
            return
        idx = type_str.index('<')
        base_name = type_str[:idx]
        struct_sym = self.globals.lookup(base_name)
        if struct_sym and struct_sym.kind == 'struct' and isinstance(struct_sym.node, StructDef):
            if struct_sym.node.generic_params:
                args_str = type_str[idx+1:-1]  # strip < and >
                # Split on commas (respecting nested generics)
                args = []
                depth = 0
                current = ''
                for ch in args_str:
                    if ch == '<':
                        depth += 1
                        current += ch
                    elif ch == '>':
                        depth -= 1
                        current += ch
                    elif ch == ',' and depth == 0:
                        args.append(current.strip())
                        current = ''
                    else:
                        current += ch
                if current.strip():
                    args.append(current.strip())
                args_tuple = tuple(args)
                if base_name not in self._generic_instantiations:
                    self._generic_instantiations[base_name] = []
                if args_tuple not in self._generic_instantiations[base_name]:
                    self._generic_instantiations[base_name].append(args_tuple)

    def _visit_type_alias(self, node: TypeAlias, scope: Scope | None = None):
        s = scope or self.globals
        existing = s.lookup_local(node.name)
        if existing:
            self.error(f'redefinition of type alias `{node.name}`', node)
            return
        s.define(node.name, Symbol('type_alias', node.target_type, node))

    def _eval_const_expr(self, node, scope: Scope | None = None):
        """Fold a constant expression to a Python int/float/str.

        Supports numeric literals (decimal/hex/float), unary -/+/~/not,
        arithmetic/bitwise/logical binary operators, references to previously
        defined enum members (bare or qualified) and const variables.
        """
        scope = scope or self.current_scope
        if isinstance(node, Number):
            if node.inferred_type == 'float':
                return float(node.value)
            try:
                return int(node.value, 0)
            except (ValueError, TypeError):
                try:
                    return int(node.value)
                except (ValueError, TypeError):
                    return node.value
        if isinstance(node, UnaryOp):
            val = self._eval_const_expr(node.operand, scope)
            if val is None:
                return None
            try:
                if node.op.name == 'MINUS':
                    return -val
                if node.op.name == 'PLUS':
                    return +val
                if node.op.name == 'TILDE':
                    return ~val
                if node.op.name == 'NOT':
                    return not val
            except TypeError:
                return None
            return None
        if isinstance(node, BinOp):
            left = self._eval_const_expr(node.left, scope)
            right = self._eval_const_expr(node.right, scope)
            if left is None or right is None:
                return None
            try:
                match node.op.name:
                    case 'PLUS': return left + right
                    case 'MINUS': return left - right
                    case 'STAR': return left * right
                    case 'SLASH' | 'SLASH_SLASH': return left // right
                    case 'PERCENT': return left % right
                    case 'POW': return left ** right
                    case 'SHL': return left << right
                    case 'SHR': return left >> right
                    case 'AMPERSAND': return left & right
                    case 'PIPE': return left | right
                    case 'CARET': return left ^ right
                    case 'AND': return left and right
                    case 'OR': return left or right
            except (ZeroDivisionError, TypeError):
                return None
            return None
        if isinstance(node, Variable):
            if node.const_value is not None:
                return node.const_value
            sym = scope.lookup(node.name)
            if sym is None and self._enum_context_name:
                sym = scope.lookup(f'{self._enum_context_name}.{node.name}')
            if sym is not None and sym.kind == 'enum_member':
                return sym.const_value
            if sym is not None and sym.kind == 'const' and sym.node is not None:
                init = getattr(sym.node, 'init', None)
                if init is not None:
                    return self._eval_const_expr(init, scope)
            if sym is not None and sym.const_value is not None:
                return sym.const_value
            return None
        if isinstance(node, Attr):
            if getattr(node, '_enum_member_value', None) is not None:
                return node._enum_member_value
            if isinstance(node.obj, Variable):
                member_sym = scope.lookup(f'{node.obj.name}.{node.name}')
                if member_sym is not None and member_sym.kind == 'enum_member':
                    return member_sym.const_value
            return None
        if isinstance(node, String):
            return node.value
        return None

    def _visit_while(self, node, scope: Scope | None = None):
        if isinstance(node, While):
            self._infer_type(node.cond)
            old_locals = self.locals
            loop_scope = Scope(scope or self.current_scope)
            self.locals = loop_scope
            self._loop_depth += 1
            for stmt in node.body:
                self._visit(stmt, loop_scope)
            self._loop_depth -= 1
            self.locals = old_locals
        elif isinstance(node, dict):
            cond = node.get('cond')
            if cond is not None:
                self._infer_type(cond)
            old_locals = self.locals
            loop_scope = Scope(scope or self.current_scope)
            self.locals = loop_scope
            self._loop_depth += 1
            for stmt in node.get('body', []):
                self._visit(stmt, loop_scope)
            self._loop_depth -= 1
            self.locals = old_locals

    def _visit_for(self, node: dict, scope: Scope | None = None):
        s = scope or self.current_scope
        loop_scope = Scope(s)
        loop_scope.define(node['var'], Symbol('variable', 'char', node))
        iterable = node.get('iter')
        if iterable is not None:
            self._infer_type(iterable)
        old_locals = self.locals
        self.locals = loop_scope
        self._loop_depth += 1
        for stmt in node.get('body', []):
            self._visit(stmt, loop_scope)
        self._loop_depth -= 1
        self.locals = old_locals

    def _resolve_type_alias(self, type_name):
        if type_name is None:
            return None
        sym = self.current_scope.lookup(type_name)
        if sym and sym.kind == 'type_alias':
            return self._resolve_type_alias(sym.type)
        return type_name


def analyze(source: str, nodes: list, strict: bool = False, workspace_root: str | None = None, enable_extensions: bool = True):
    analyzer = SemanticAnalyzer(source, strict=strict, workspace_root=workspace_root, enable_extensions=enable_extensions)
    # Pre-load CPM package manifests before analysis
    if enable_extensions:
        analyzer._load_cpm_package_manifests()
    if not analyzer.analyze(nodes):
        return analyzer.reporter.display(), None
    return None, analyzer._generic_instantiations
