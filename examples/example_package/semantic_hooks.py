"""
Example semantic hooks for the example_package.

This demonstrates how packages can extend semantic analysis with custom type checking.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'source'))
from cpyte.extension_hooks import SemanticHook


class AsyncSemanticHook(SemanticHook):
    """Semantic hook for async function validation."""
    
    def __init__(self, package_name: str, hook_path: str = None):
        super().__init__(package_name, hook_path)
    
    def should_visit_node(self, node):
        """Check if this is an async function."""
        return hasattr(node, 'is_async') and node.is_async
    
    def visit_node(self, node, context):
        """Validate async function semantics."""
        errors = []
        
        # Check that async functions return Promise types
        if hasattr(node, 'rettype') and node.rettype:
            if not node.rettype.endswith('Promise') and not node.rettype.endswith('Future'):
                errors.append(
                    f"Async function '{node.name}' should return Promise or Future type, "
                    f"but returns '{node.rettype}'"
                )
        
        return errors if errors else None
    
    def get_custom_type_rules(self):
        """Return custom type checking rules."""
        return {
            'Promise': self._check_promise_type,
            'Future': self._check_future_type,
        }
    
    def _check_promise_type(self, type_expr):
        """Custom type checking for Promise types."""
        # Placeholder for custom type logic
        return True
    
    def _check_future_type(self, type_expr):
        """Custom type checking for Future types."""
        # Placeholder for custom type logic
        return True
    
    def initialize(self, context):
        """Initialize the semantic hook."""
        pass


def get_hooks():
    """Return list of semantic hooks provided by this package."""
    return [
        AsyncSemanticHook("example_package"),
    ]