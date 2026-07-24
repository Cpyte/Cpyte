"""
Extension hooks framework for Cpyte package extensions.

This module provides the hook system that allows packages to extend the compiler
at various stages: lexing, parsing, semantic analysis, and code generation.
"""

import os
import sys
import importlib.util
from typing import Dict, List, Optional, Callable, Any, Set
from dataclasses import dataclass
from abc import ABC, abstractmethod


# =============================================================================
# Hook Base Classes
# =============================================================================

class CompilerHook(ABC):
    """Base class for all compiler hooks."""
    
    def __init__(self, package_name: str, hook_path: Optional[str] = None):
        self.package_name = package_name
        self.hook_path = hook_path
        self.enabled = True
    
    @abstractmethod
    def initialize(self, context: Dict[str, Any]) -> None:
        """Initialize the hook with compiler context."""
        pass
    
    def disable(self) -> None:
        """Disable this hook."""
        self.enabled = False
    
    def enable(self) -> None:
        """Enable this hook."""
        self.enabled = True


class LexerHook(CompilerHook):
    """Hook for extending the lexer with custom tokens and keywords."""
    
    def get_additional_keywords(self) -> Set[str]:
        """Return set of additional keywords this hook provides."""
        return set()
    
    def get_additional_operators(self) -> Set[str]:
        """Return set of additional operators this hook provides."""
        return set()
    
    def should_customize_token(self, token_type: str, token_value: str) -> bool:
        """Return True if this hook wants to customize a given token."""
        return False
    
    def customize_token(self, token_type: str, token_value: str, line: int, column: int) -> Optional[Dict[str, Any]]:
        """Customize a token, returning modified token data or None to keep original."""
        return None
    
    def initialize(self, context: Dict[str, Any]) -> None:
        """Initialize the lexer hook with compiler context."""
        pass


class ParserHook(CompilerHook):
    """Hook for extending the parser with custom syntax."""
    
    def should_handle_expression(self, tokens: List[Any], pos: int) -> bool:
        """Return True if this hook wants to handle expression parsing at current position."""
        return False
    
    def parse_expression(self, tokens: List[Any], pos: int, context: Dict[str, Any]) -> tuple:
        """
        Parse a custom expression.
        
        Returns:
            Tuple of (parsed_node, new_position)
        """
        raise NotImplementedError("Parser hook must implement parse_expression")
    
    def should_handle_statement(self, tokens: List[Any], pos: int) -> bool:
        """Return True if this hook wants to handle statement parsing at current position."""
        return False
    
    def parse_statement(self, tokens: List[Any], pos: int, context: Dict[str, Any]) -> tuple:
        """
        Parse a custom statement.
        
        Returns:
            Tuple of (parsed_node, new_position)
        """
        raise NotImplementedError("Parser hook must implement parse_statement")
    
    def initialize(self, context: Dict[str, Any]) -> None:
        """Initialize the parser hook with compiler context."""
        pass


class SemanticHook(CompilerHook):
    """Hook for extending semantic analysis with custom type checking and rules."""
    
    def should_visit_node(self, node: Any) -> bool:
        """Return True if this hook wants to analyze a specific node type."""
        return False
    
    def visit_node(self, node: Any, context: Dict[str, Any]) -> Optional[List[str]]:
        """
        Analyze a node and return list of error messages, or None if no errors.
        
        Returns:
            List of error messages, or None/empty list if no errors
        """
        return None
    
    def get_custom_type_rules(self) -> Dict[str, Callable]:
        """Return custom type checking rules."""
        return {}
    
    def initialize(self, context: Dict[str, Any]) -> None:
        """Initialize the semantic hook with compiler context."""
        pass


class CodegenHook(CompilerHook):
    """Hook for extending code generation with custom IR emission."""
    
    def should_emit_node(self, node: Any) -> bool:
        """Return True if this hook wants to handle code generation for a node."""
        return False
    
    def emit_node(self, node: Any, builder: Any, context: Dict[str, Any]) -> Any:
        """
        Generate custom IR for a node.
        
        Returns:
            The generated IR value or None
        """
        raise NotImplementedError("Codegen hook must implement emit_node")
    
    def should_add_module_passes(self) -> bool:
        """Return True if this hook wants to add custom optimization passes."""
        return False
    
    def add_module_passes(self, pass_manager: Any, context: Dict[str, Any]) -> None:
        """Add custom optimization passes to the pass manager."""
        pass
    
    def initialize(self, context: Dict[str, Any]) -> None:
        """Initialize the codegen hook with compiler context."""
        pass


class RuntimeHook(CompilerHook):
    """Hook for extending runtime behavior."""
    
    def get_runtime_code(self) -> Optional[str]:
        """Return additional runtime code as string, or None."""
        return None
    
    def get_runtime_libraries(self) -> List[str]:
        """Return list of additional runtime libraries to link."""
        return []
    
    def initialize(self, context: Dict[str, Any]) -> None:
        """Initialize the runtime hook with compiler context."""
        pass


# =============================================================================
# Hook Registry
# =============================================================================

@dataclass
class HookRegistration:
    """Represents a registered hook with its metadata."""
    hook: CompilerHook
    priority: int  # Higher priority hooks run first
    package_name: str
    hook_path: Optional[str] = None


class HookRegistry:
    """
    Central registry for all compiler hooks.
    
    Manages registration, prioritization, and execution of hooks across
    all compilation stages.
    """
    
    def __init__(self):
        self._lexer_hooks: List[HookRegistration] = []
        self._parser_hooks: List[HookRegistration] = []
        self._semantic_hooks: List[HookRegistration] = []
        self._codegen_hooks: List[HookRegistration] = []
        self._runtime_hooks: List[HookRegistration] = []
        
        self._context: Dict[str, Any] = {}
    
    def set_context(self, context: Dict[str, Any]) -> None:
        """Set the compiler context for hooks."""
        self._context = context.copy()
    
    def update_context(self, key: str, value: Any) -> None:
        """Update a specific context value."""
        self._context[key] = value
    
    def get_context(self) -> Dict[str, Any]:
        """Get the current compiler context."""
        return self._context.copy()
    
    def register_lexer_hook(self, hook: LexerHook, priority: int = 0) -> None:
        """Register a lexer hook."""
        registration = HookRegistration(
            hook=hook,
            priority=priority,
            package_name=hook.package_name,
            hook_path=hook.hook_path
        )
        self._lexer_hooks.append(registration)
        self._lexer_hooks.sort(key=lambda r: r.priority, reverse=True)
    
    def register_parser_hook(self, hook: ParserHook, priority: int = 0) -> None:
        """Register a parser hook."""
        registration = HookRegistration(
            hook=hook,
            priority=priority,
            package_name=hook.package_name,
            hook_path=hook.hook_path
        )
        self._parser_hooks.append(registration)
        self._parser_hooks.sort(key=lambda r: r.priority, reverse=True)
    
    def register_semantic_hook(self, hook: SemanticHook, priority: int = 0) -> None:
        """Register a semantic analysis hook."""
        registration = HookRegistration(
            hook=hook,
            priority=priority,
            package_name=hook.package_name,
            hook_path=hook.hook_path
        )
        self._semantic_hooks.append(registration)
        self._semantic_hooks.sort(key=lambda r: r.priority, reverse=True)
    
    def register_codegen_hook(self, hook: CodegenHook, priority: int = 0) -> None:
        """Register a code generation hook."""
        registration = HookRegistration(
            hook=hook,
            priority=priority,
            package_name=hook.package_name,
            hook_path=hook.hook_path
        )
        self._codegen_hooks.append(registration)
        self._codegen_hooks.sort(key=lambda r: r.priority, reverse=True)
    
    def register_runtime_hook(self, hook: RuntimeHook, priority: int = 0) -> None:
        """Register a runtime hook."""
        registration = HookRegistration(
            hook=hook,
            priority=priority,
            package_name=hook.package_name,
            hook_path=hook.hook_path
        )
        self._runtime_hooks.append(registration)
        self._runtime_hooks.sort(key=lambda r: r.priority, reverse=True)
    
    def get_lexer_hooks(self) -> List[LexerHook]:
        """Get all registered lexer hooks in priority order."""
        return [reg.hook for reg in self._lexer_hooks if reg.hook.enabled]
    
    def get_parser_hooks(self) -> List[ParserHook]:
        """Get all registered parser hooks in priority order."""
        return [reg.hook for reg in self._parser_hooks if reg.hook.enabled]
    
    def get_semantic_hooks(self) -> List[SemanticHook]:
        """Get all registered semantic hooks in priority order."""
        return [reg.hook for reg in self._semantic_hooks if reg.hook.enabled]
    
    def get_codegen_hooks(self) -> List[CodegenHook]:
        """Get all registered codegen hooks in priority order."""
        return [reg.hook for reg in self._codegen_hooks if reg.hook.enabled]
    
    def get_runtime_hooks(self) -> List[RuntimeHook]:
        """Get all registered runtime hooks in priority order."""
        return [reg.hook for reg in self._runtime_hooks if reg.hook.enabled]
    
    def unregister_package_hooks(self, package_name: str) -> None:
        """Unregister all hooks from a specific package."""
        self._lexer_hooks = [r for r in self._lexer_hooks if r.package_name != package_name]
        self._parser_hooks = [r for r in self._parser_hooks if r.package_name != package_name]
        self._semantic_hooks = [r for r in self._semantic_hooks if r.package_name != package_name]
        self._codegen_hooks = [r for r in self._codegen_hooks if r.package_name != package_name]
        self._runtime_hooks = [r for r in self._runtime_hooks if r.package_name != package_name]
    
    def clear(self) -> None:
        """Clear all registered hooks."""
        self._lexer_hooks.clear()
        self._parser_hooks.clear()
        self._semantic_hooks.clear()
        self._codegen_hooks.clear()
        self._runtime_hooks.clear()
        self._context.clear()


# =============================================================================
# Hook Loader
# =============================================================================

class HookLoadError(Exception):
    """Raised when hook loading fails."""
    pass


class HookLoader:
    """Loads and initializes hooks from package hook files."""
    
    @staticmethod
    def load_hooks_from_package(
        package_name: str,
        package_dir: str,
        hook_files: List[str],
        registry: HookRegistry,
        context: Dict[str, Any]
    ) -> int:
        """
        Load hooks from a package's hook files.
        
        Args:
            package_name: Name of the package
            package_dir: Directory containing the package
            hook_files: List of hook file paths (relative to package_dir)
            registry: Hook registry to register loaded hooks
            context: Compiler context to pass to hooks
            
        Returns:
            Number of hooks successfully loaded
            
        Raises:
            HookLoadError: If hook loading fails
        """
        loaded_count = 0
        
        for hook_file in hook_files:
            hook_path = os.path.join(package_dir, hook_file)
            
            if not os.path.exists(hook_path):
                print(f"Warning: Hook file not found: {hook_path}", file=sys.stderr)
                continue
            
            try:
                hooks = HookLoader._load_hook_file(hook_path, package_name)
                for hook in hooks:
                    hook.initialize(context)
                    
                    # Register based on hook type
                    if isinstance(hook, LexerHook):
                        registry.register_lexer_hook(hook)
                    elif isinstance(hook, ParserHook):
                        registry.register_parser_hook(hook)
                    elif isinstance(hook, SemanticHook):
                        registry.register_semantic_hook(hook)
                    elif isinstance(hook, CodegenHook):
                        registry.register_codegen_hook(hook)
                    elif isinstance(hook, RuntimeHook):
                        registry.register_runtime_hook(hook)
                    
                    loaded_count += 1
                    
            except Exception as e:
                raise HookLoadError(
                    f"Failed to load hooks from {hook_path}: {e}"
                )
        
        return loaded_count
    
    @staticmethod
    def _load_hook_file(hook_path: str, package_name: str) -> List[CompilerHook]:
        """
        Load hooks from a single hook file.
        
        Hook files should define a function `get_hooks()` that returns
        a list of hook instances.
        
        Args:
            hook_path: Path to the hook file
            package_name: Name of the package owning the hooks
            
        Returns:
            List of loaded hook instances
            
        Raises:
            HookLoadError: If loading fails
        """
        # Load the module
        spec = importlib.util.spec_from_file_location(
            f"{package_name}_hooks",
            hook_path
        )
        if spec is None or spec.loader is None:
            raise HookLoadError(f"Cannot load hook file: {hook_path}")
        
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            raise HookLoadError(f"Failed to execute hook file {hook_path}: {e}")
        
        # Get hooks from the module
        if not hasattr(module, 'get_hooks'):
            raise HookLoadError(
                f"Hook file {hook_path} must define a get_hooks() function"
            )
        
        get_hooks_func = getattr(module, 'get_hooks')
        if not callable(get_hooks_func):
            raise HookLoadError(
                f"get_hooks in {hook_path} must be a callable"
            )
        
        try:
            hooks = get_hooks_func()
        except Exception as e:
            raise HookLoadError(
                f"get_hooks() failed in {hook_path}: {e}"
            )
        
        if not isinstance(hooks, list):
            raise HookLoadError(
                f"get_hooks() in {hook_path} must return a list"
            )
        
        # Validate hooks
        for hook in hooks:
            if not isinstance(hook, CompilerHook):
                raise HookLoadError(
                    f"Invalid hook in {hook_path}: must be instance of CompilerHook"
                )
            hook.package_name = package_name
            hook.hook_path = hook_path
        
        return hooks


# =============================================================================
# Global hook registry instance
# =============================================================================

_global_hook_registry = HookRegistry()


def get_global_hook_registry() -> HookRegistry:
    """Get the global hook registry instance."""
    return _global_hook_registry


def reset_global_hook_registry() -> None:
    """Reset the global hook registry (useful for testing)."""
    _global_hook_registry.clear()