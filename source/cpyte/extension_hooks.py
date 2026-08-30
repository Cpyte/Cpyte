"""
Cpyte Compiler Extension Framework.

Provides a stable extension API for packages that need to participate in
compilation without modifying the Cpyte compiler itself.

Extension stages:

    lifecycle
        ↓
    lexer
        ↓
    parser
        ↓
    semantic
        ↓
    AST transformation
        ↓
    codegen
        ↓
    optimization
        ↓
    linking
        ↓
    runtime

Extensions are ordinary Python packages loaded by HookLoader.
"""

from __future__ import annotations

import importlib.util
import os
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Generic, TypeVar

# ============================================================================
# Diagnostics
# ============================================================================

class DiagnosticSeverity(IntEnum):
    NOTE = 0
    WARNING = 1
    ERROR = 2
    FATAL = 3


@dataclass(slots=True)
class SourceLocation:
    file: str | None = None
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None


@dataclass(slots=True)
class Diagnostic:
    severity: DiagnosticSeverity
    message: str
    location: SourceLocation | None = None
    code: str | None = None
    package: str | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def error(
        cls,
        message: str,
        *,
        code: str | None = None,
        location: SourceLocation | None = None,
        package: str | None = None,
    ) -> Diagnostic:
        return cls(
            DiagnosticSeverity.ERROR,
            message,
            location,
            code,
            package,
        )

    @classmethod
    def warning(
        cls,
        message: str,
        *,
        code: str | None = None,
        location: SourceLocation | None = None,
        package: str | None = None,
    ) -> Diagnostic:
        return cls(
            DiagnosticSeverity.WARNING,
            message,
            location,
            code,
            package,
        )


# ============================================================================
# Compiler Context
# ============================================================================

@dataclass
class CompilerContext:
    """
    Shared compiler state exposed to extensions.

    Extensions should prefer using the explicit fields below instead of
    relying on arbitrary dictionary keys. `data` exists for extension-
    specific state.
    """

    source_file: str | None = None
    target: str | None = None
    platform: str | None = None
    architecture: str | None = None
    optimization_level: int = 0
    debug: bool = False

    ast: Any = None
    semantic_model: Any = None
    llvm_module: Any = None

    diagnostics: list[Diagnostic] = field(default_factory=list)

    include_paths: list[str] = field(default_factory=list)
    library_paths: list[str] = field(default_factory=list)
    libraries: list[str] = field(default_factory=list)

    compiler_flags: list[str] = field(default_factory=list)
    linker_flags: list[str] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)

    data: dict[str, Any] = field(default_factory=dict)

    def emit(
        self,
        severity: DiagnosticSeverity,
        message: str,
        *,
        code: str | None = None,
        location: SourceLocation | None = None,
        package: str | None = None,
    ) -> Diagnostic:
        diagnostic = Diagnostic(
            severity=severity,
            message=message,
            location=location,
            code=code,
            package=package,
        )
        self.diagnostics.append(diagnostic)
        return diagnostic

    def error(self, message: str, **kwargs: Any) -> Diagnostic:
        return self.emit(DiagnosticSeverity.ERROR, message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> Diagnostic:
        return self.emit(DiagnosticSeverity.WARNING, message, **kwargs)

    def note(self, message: str, **kwargs: Any) -> Diagnostic:
        return self.emit(DiagnosticSeverity.NOTE, message, **kwargs)


# ============================================================================
# Hook Metadata
# ============================================================================

@dataclass(frozen=True, slots=True)
class HookMetadata:
    """
    Metadata used by the registry to order and validate extensions.
    """

    name: str
    version: str = "0.0.0"
    priority: int = 0

    requires: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    description: str = ""


# ============================================================================
# Compiler Stages
# ============================================================================

class HookStage(Enum):
    LIFECYCLE = "lifecycle"
    LEXER = "lexer"
    PARSER = "parser"
    SEMANTIC = "semantic"
    TRANSFORM = "transform"
    CODEGEN = "codegen"
    OPTIMIZE = "optimize"
    LINK = "link"
    RUNTIME = "runtime"


# ============================================================================
# Base Hook
# ============================================================================

class CompilerHook(ABC):

    stage: HookStage | None = None

    def __init__(
        self,
        package_name: str,
        *,
        metadata: HookMetadata | None = None,
    ):
        self.package_name = package_name
        self.metadata = metadata or HookMetadata(name=package_name)

        self.hook_path: str | None = None
        self.enabled = True

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def priority(self) -> int:
        return self.metadata.priority

    def initialize(self, context: CompilerContext) -> None:
        pass

    def shutdown(self, context: CompilerContext) -> None:
        pass

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False


# ============================================================================
# Lexer
# ============================================================================

class LexerHook(CompilerHook):

    stage = HookStage.LEXER

    def get_additional_keywords(self) -> set[str]:
        return set()

    def get_additional_operators(self) -> set[str]:
        return set()

    def should_customize_token(
        self,
        token_type: str,
        token_value: str,
    ) -> bool:
        return False

    def customize_token(
        self,
        token_type: str,
        token_value: str,
        line: int,
        column: int,
    ) -> dict[str, Any] | None:
        return None


# ============================================================================
# Parser
# ============================================================================

class ParserHook(CompilerHook):

    stage = HookStage.PARSER

    def should_handle_expression(
        self,
        tokens: list[Any],
        position: int,
    ) -> bool:
        return False

    def parse_expression(
        self,
        tokens: list[Any],
        position: int,
        context: CompilerContext,
    ) -> tuple[Any, int]:
        raise NotImplementedError

    def should_handle_statement(
        self,
        tokens: list[Any],
        position: int,
    ) -> bool:
        return False

    def parse_statement(
        self,
        tokens: list[Any],
        position: int,
        context: CompilerContext,
    ) -> tuple[Any, int]:
        raise NotImplementedError


# ============================================================================
# Semantic Analysis
# ============================================================================

class SemanticHook(CompilerHook):

    stage = HookStage.SEMANTIC

    def should_visit_node(self, node: Any) -> bool:
        return False

    def visit_node(
        self,
        node: Any,
        context: CompilerContext,
    ) -> list[Diagnostic]:
        return []

    def get_custom_type_rules(self) -> dict[str, Callable[..., Any]]:
        return {}

    def can_convert(
        self,
        source_type: Any,
        target_type: Any,
        context: CompilerContext,
    ) -> bool | None:
        """
        Return:

            True  -> conversion is supported
            False -> conversion is explicitly rejected
            None  -> hook does not know
        """
        return None

    def infer_type(
        self,
        node: Any,
        context: CompilerContext,
    ) -> Any | None:
        return None


# ============================================================================
# AST Transformation
# ============================================================================

class TransformHook(CompilerHook):

    stage = HookStage.TRANSFORM

    def should_transform(self, node: Any) -> bool:
        return False

    def transform_node(
        self,
        node: Any,
        context: CompilerContext,
    ) -> Any:
        return node

    def transform_module(
        self,
        module: Any,
        context: CompilerContext,
    ) -> Any:
        return module


# ============================================================================
# Symbol / Import Resolution
# ============================================================================

class SymbolResolverHook(CompilerHook):

    """
    Allows packages to provide external symbols, imports, SDKs, headers,
    generated declarations, etc.
    """

    stage = HookStage.SEMANTIC

    def can_resolve_import(self, name: str) -> bool:
        return False

    def resolve_import(
        self,
        name: str,
        context: CompilerContext,
    ) -> Any | None:
        return None

    def can_resolve_symbol(self, name: str) -> bool:
        return False

    def resolve_symbol(
        self,
        name: str,
        context: CompilerContext,
    ) -> Any | None:
        return None


# ============================================================================
# Code Generation
# ============================================================================

class CodegenHook(CompilerHook):

    stage = HookStage.CODEGEN

    def should_emit_node(self, node: Any) -> bool:
        return False

    def emit_node(
        self,
        node: Any,
        builder: Any,
        context: CompilerContext,
    ) -> Any:
        raise NotImplementedError

    def before_codegen(
        self,
        module: Any,
        context: CompilerContext,
    ) -> None:
        pass

    def after_codegen(
        self,
        module: Any,
        context: CompilerContext,
    ) -> None:
        pass


# ============================================================================
# Optimization
# ============================================================================

class OptimizeHook(CompilerHook):

    stage = HookStage.OPTIMIZE

    def should_add_passes(self, context: CompilerContext) -> bool:
        return False

    def add_module_passes(
        self,
        pass_manager: Any,
        context: CompilerContext,
    ) -> None:
        pass

    def optimize_module(
        self,
        module: Any,
        context: CompilerContext,
    ) -> Any:
        return module


# ============================================================================
# Build / Linking
# ============================================================================

class BuildHook(CompilerHook):

    stage = HookStage.LINK

    def get_include_paths(
        self,
        context: CompilerContext,
    ) -> list[str]:
        return []

    def get_library_paths(
        self,
        context: CompilerContext,
    ) -> list[str]:
        return []

    def get_libraries(
        self,
        context: CompilerContext,
    ) -> list[str]:
        return []

    def get_defines(
        self,
        context: CompilerContext,
    ) -> dict[str, str | None]:
        return {}

    def get_compiler_flags(
        self,
        context: CompilerContext,
    ) -> list[str]:
        return []

    def get_linker_flags(
        self,
        context: CompilerContext,
    ) -> list[str]:
        return []


# ============================================================================
# Runtime
# ============================================================================

class RuntimeHook(CompilerHook):

    stage = HookStage.RUNTIME

    def get_runtime_code(
        self,
        context: CompilerContext,
    ) -> str | None:
        return None

    def get_runtime_files(
        self,
        context: CompilerContext,
    ) -> list[str]:
        return []

    def get_runtime_libraries(
        self,
        context: CompilerContext,
    ) -> list[str]:
        return []


# ============================================================================
# Lifecycle
# ============================================================================

class LifecycleHook(CompilerHook):

    stage = HookStage.LIFECYCLE

    def before_compile(self, context: CompilerContext) -> None:
        pass

    def after_compile(self, context: CompilerContext) -> None:
        pass

    def on_compile_error(self, context: CompilerContext) -> None:
        pass


# ============================================================================
# Registration
# ============================================================================

H = TypeVar("H", bound=CompilerHook)


@dataclass(slots=True)
class HookRegistration(Generic[H]):

    hook: H
    package_name: str
    priority: int

    @property
    def name(self) -> str:
        return self.hook.name


# ============================================================================
# Registry
# ============================================================================

class HookRegistry:

    def __init__(self) -> None:
        self._hooks: dict[HookStage, list[HookRegistration]] = {
            stage: []
            for stage in HookStage
        }

        self._by_name: dict[str, CompilerHook] = {}
        self._context: CompilerContext | None = None

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def set_context(self, context: CompilerContext) -> None:
        self._context = context

    def get_context(self) -> CompilerContext | None:
        return self._context

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        hook: CompilerHook,
        *,
        priority: int | None = None,
    ) -> None:

        if hook.stage is None:
            raise ValueError(
                f"Hook {hook.name!r} does not define a compiler stage"
            )

        if hook.name in self._by_name:
            raise ValueError(
                f"Hook {hook.name!r} is already registered"
            )

        priority = (
            hook.priority
            if priority is None
            else priority
        )

        for conflict in hook.metadata.conflicts:
            if conflict in self._by_name:
                raise ValueError(
                    f"Hook {hook.name!r} conflicts with {conflict!r}"
                )

        registration = HookRegistration(
            hook=hook,
            package_name=hook.package_name,
            priority=priority,
        )

        self._hooks[hook.stage].append(registration)

        self._hooks[hook.stage].sort(
            key=lambda r: (-r.priority, r.package_name, r.name)
        )

        self._by_name[hook.name] = hook

    def unregister(self, name: str) -> bool:
        hook = self._by_name.pop(name, None)

        if hook is None:
            return False

        if hook.stage is not None:
            self._hooks[hook.stage] = [
                registration
                for registration in self._hooks[hook.stage]
                if registration.hook is not hook
            ]

        return True

    def unregister_package(self, package_name: str) -> None:
        names = [
            name
            for name, hook in self._by_name.items()
            if hook.package_name == package_name
        ]

        for name in names:
            self.unregister(name)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get(
        self,
        stage: HookStage,
    ) -> list[CompilerHook]:

        return [
            registration.hook
            for registration in self._hooks[stage]
            if registration.hook.enabled
        ]

    # ------------------------------------------------------------------
    # Compatibility accessors (legacy callers used get_*_hooks methods)
    # ------------------------------------------------------------------

    def get_parser_hooks(self) -> list[CompilerHook]:
        return self.get(HookStage.PARSER)

    def get_semantic_hooks(self) -> list[CompilerHook]:
        return self.get(HookStage.SEMANTIC)

    def get_codegen_hooks(self) -> list[CompilerHook]:
        return self.get(HookStage.CODEGEN)

    def get_lexer_hooks(self) -> list[CompilerHook]:
        return self.get(HookStage.LEXER)

    def get_runtime_hooks(self) -> list[CompilerHook]:
        return self.get(HookStage.RUNTIME)

    def get_typed(
        self,
        hook_type: type[H],
    ) -> list[H]:

        return [
            registration.hook
            for registrations in self._hooks.values()
            for registration in registrations
            if (
                registration.hook.enabled
                and isinstance(registration.hook, hook_type)
            )
        ]

    def find(self, name: str) -> CompilerHook | None:
        return self._by_name.get(name)

    def all(self) -> list[CompilerHook]:
        return [
            registration.hook
            for registrations in self._hooks.values()
            for registration in registrations
            if registration.hook.enabled
        ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize_all(
        self,
        context: CompilerContext,
    ) -> None:

        self._context = context

        for hook in self.all():
            hook.initialize(context)

    def shutdown_all(
        self,
        context: CompilerContext,
    ) -> None:

        for hook in reversed(self.all()):
            hook.shutdown(context)

    def clear(self) -> None:
        self._hooks = {
            stage: []
            for stage in HookStage
        }
        self._by_name.clear()
        self._context = None


# ============================================================================
# Hook Loading
# ============================================================================

class HookLoadError(Exception):
    pass


class HookLoader:

    @staticmethod
    def load_hooks_from_package(
        package_name: str,
        package_dir: str,
        hook_files: list[str],
        registry: HookRegistry,
        context: CompilerContext,
    ) -> int:

        loaded = 0

        for relative_path in hook_files:

            hook_path = os.path.abspath(
                os.path.join(package_dir, relative_path)
            )

            if not os.path.isfile(hook_path):
                raise HookLoadError(
                    f"Hook file does not exist: {hook_path}"
                )

            try:
                hooks = HookLoader._load_hook_file(
                    hook_path,
                    package_name,
                )

                for hook in hooks:
                    hook.initialize(context)
                    registry.register(hook)
                    loaded += 1

            except HookLoadError:
                raise

            except Exception as exc:
                raise HookLoadError(
                    f"Failed loading hooks from "
                    f"{hook_path}: {exc}"
                ) from exc

        return loaded

    @staticmethod
    def _load_hook_file(
        hook_path: str,
        package_name: str,
    ) -> list[CompilerHook]:

        module_name = (
            f"_cpyte_extension_"
            f"{abs(hash((package_name, hook_path)))}"
        )

        spec = importlib.util.spec_from_file_location(
            module_name,
            hook_path,
        )

        if spec is None or spec.loader is None:
            raise HookLoadError(
                f"Cannot create module loader for {hook_path}"
            )

        module = importlib.util.module_from_spec(spec)

        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise HookLoadError(
                f"Failed to execute {hook_path}: {exc}"
            ) from exc

        get_hooks = getattr(module, "get_hooks", None)

        if get_hooks is None:
            raise HookLoadError(
                f"{hook_path} must define get_hooks()"
            )

        if not callable(get_hooks):
            raise HookLoadError(
                f"get_hooks in {hook_path} is not callable"
            )

        try:
            hooks = get_hooks()
        except Exception as exc:
            raise HookLoadError(
                f"get_hooks() failed in {hook_path}: {exc}"
            ) from exc

        if not isinstance(hooks, (list, tuple)):
            raise HookLoadError(
                f"get_hooks() in {hook_path} must return "
                f"a list or tuple"
            )

        result: list[CompilerHook] = []

        for hook in hooks:

            if not isinstance(hook, CompilerHook):
                raise HookLoadError(
                    f"Invalid hook returned by {hook_path}: "
                    f"{hook!r}"
                )

            hook.package_name = package_name
            hook.hook_path = hook_path

            # Hooks constructed with only a package name fall back to the
            # default HookMetadata(name=package_name). The registry keys hooks
            # by metadata name and rejects duplicates, so give such hooks a
            # unique name derived from the package, defining file and class.
            if hook.name == package_name:
                module_name = os.path.splitext(
                    os.path.basename(hook_path)
                )[0]
                hook.metadata = HookMetadata(
                    name=(
                        f"{package_name}:{module_name}:"
                        f"{type(hook).__name__}"
                    ),
                    version=hook.metadata.version,
                    priority=hook.metadata.priority,
                    requires=hook.metadata.requires,
                    conflicts=hook.metadata.conflicts,
                    description=hook.metadata.description,
                )

            result.append(hook)

        return result


# ============================================================================
# Hook Dispatcher
# ============================================================================

class HookDispatcher:

    """
    High-level interface used by the compiler itself.

    The compiler should generally interact with this class rather than
    manually iterating over every hook list.
    """

    def __init__(self, registry: HookRegistry):
        self.registry = registry

    # ------------------------------------------------------------------
    # Lexer
    # ------------------------------------------------------------------

    def additional_keywords(self) -> set[str]:
        result: set[str] = set()

        for hook in self.registry.get(HookStage.LEXER):
            if isinstance(hook, LexerHook):
                result.update(
                    hook.get_additional_keywords()
                )

        return result

    def additional_operators(self) -> set[str]:
        result: set[str] = set()

        for hook in self.registry.get(HookStage.LEXER):
            if isinstance(hook, LexerHook):
                result.update(
                    hook.get_additional_operators()
                )

        return result

    # ------------------------------------------------------------------
    # AST
    # ------------------------------------------------------------------

    def transform_node(
        self,
        node: Any,
        context: CompilerContext,
    ) -> Any:

        for hook in self.registry.get(HookStage.TRANSFORM):

            if not isinstance(hook, TransformHook):
                continue

            if hook.should_transform(node):
                node = hook.transform_node(
                    node,
                    context,
                )

        return node

    # ------------------------------------------------------------------
    # Semantic
    # ------------------------------------------------------------------

    def analyze_node(
        self,
        node: Any,
        context: CompilerContext,
    ) -> list[Diagnostic]:

        diagnostics: list[Diagnostic] = []

        for hook in self.registry.get(HookStage.SEMANTIC):

            if isinstance(hook, SemanticHook):
                if hook.should_visit_node(node):
                    diagnostics.extend(
                        hook.visit_node(
                            node,
                            context,
                        )
                    )

        return diagnostics

    # ------------------------------------------------------------------
    # Codegen
    # ------------------------------------------------------------------

    def emit_node(
        self,
        node: Any,
        builder: Any,
        context: CompilerContext,
    ) -> Any | None:

        for hook in self.registry.get(HookStage.CODEGEN):

            if not isinstance(hook, CodegenHook):
                continue

            if hook.should_emit_node(node):
                return hook.emit_node(
                    node,
                    builder,
                    context,
                )

        return None

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def collect_build_configuration(
        self,
        context: CompilerContext,
    ) -> None:

        for hook in self.registry.get(HookStage.LINK):

            if not isinstance(hook, BuildHook):
                continue

            context.include_paths.extend(
                hook.get_include_paths(context)
            )

            context.library_paths.extend(
                hook.get_library_paths(context)
            )

            context.libraries.extend(
                hook.get_libraries(context)
            )

            context.defines.update(
                hook.get_defines(context)
            )

            context.compiler_flags.extend(
                hook.get_compiler_flags(context)
            )

            context.linker_flags.extend(
                hook.get_linker_flags(context)
            )

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------

    def runtime_code(
        self,
        context: CompilerContext,
    ) -> list[str]:

        result: list[str] = []

        for hook in self.registry.get(HookStage.RUNTIME):

            if not isinstance(hook, RuntimeHook):
                continue

            code = hook.get_runtime_code(context)

            if code:
                result.append(code)

        return result


# ============================================================================
# Global Registry
# ============================================================================

_global_registry = HookRegistry()
_global_dispatcher = HookDispatcher(_global_registry)


def get_global_hook_registry() -> HookRegistry:
    return _global_registry


def get_global_hook_dispatcher() -> HookDispatcher:
    return _global_dispatcher
