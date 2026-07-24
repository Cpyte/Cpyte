"""
Example codegen hooks for the example_package.

This demonstrates how packages can extend code generation with custom IR emission.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'source'))
from cpyte.extension_hooks import CodegenHook


class AsyncCodegenHook(CodegenHook):
    """Codegen hook for async function IR generation."""
    
    def __init__(self, package_name: str, hook_path: str = None):
        super().__init__(package_name, hook_path)
    
    def should_emit_node(self, node):
        """Check if this is an async function."""
        return hasattr(node, 'is_async') and node.is_async
    
    def emit_node(self, node, builder, context):
        """Generate custom IR for async functions."""
        llvm = context['llvm']
        
        # Generate async function wrapper
        # This is a simplified version - real implementation would integrate
        # with async runtime and promise handling
        
        # For now, just use standard function generation
        return llvm.funcdo(node)
    
    def should_add_module_passes(self):
        """Return True to add custom optimization passes."""
        return True
    
    def add_module_passes(self, pass_manager, context):
        """Add custom optimization passes for async code."""
        # Add async-specific optimization passes
        # This would integrate with coroutine optimization
        pass
    
    def initialize(self, context):
        """Initialize the codegen hook."""
        pass


def get_hooks():
    """Return list of codegen hooks provided by this package."""
    return [
        AsyncCodegenHook("example_package"),
    ]