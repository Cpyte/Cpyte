"""
Example codegen hooks for the example_package.

This demonstrates how packages can extend code generation with custom IR
emission, and how the optimization stage (pass manager) is contributed through
an :class:`OptimizeHook` rather than the codegen hook.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "source"))
from cpyte.extension_hooks import CodegenHook, OptimizeHook


class AsyncCodegenHook(CodegenHook):
    """Codegen hook for async function IR generation."""

    def should_emit_node(self, node):
        """Check if this is an async function."""
        return hasattr(node, "is_async") and node.is_async

    def emit_node(self, node, builder, context):
        """Generate custom IR for async functions."""
        # `context` is a CompilerContext; the LLVM codegen instance and module
        # are exposed through `context.data`.
        llvm = context.data.get("llvm")

        # Generate async function wrapper
        # This is a simplified version - real implementation would integrate
        # with async runtime and promise handling

        # For now, just use standard function generation
        return llvm.emit_funcdef(node) if llvm else node

    def initialize(self, context):
        """Initialize the codegen hook."""


class AsyncOptimizeHook(OptimizeHook):
    """Optimization passes for async code (contributed via the optimize stage)."""

    def should_add_passes(self, context):
        """Return True to add custom optimization passes."""
        return True

    def add_module_passes(self, pass_manager, context):
        """Add custom optimization passes for async code."""
        # This would integrate with coroutine optimization
        pass


def get_hooks():
    """Return list of codegen hooks provided by this package."""
    return [
        AsyncCodegenHook("example_package"),
        AsyncOptimizeHook("example_package"),
    ]
