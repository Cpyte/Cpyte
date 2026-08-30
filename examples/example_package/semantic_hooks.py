"""
Example semantic hooks for the example_package.

This demonstrates how packages can extend semantic analysis with custom type
checking. The new API returns a list of :class:`Diagnostic` objects instead of
plain error strings, so package authors get the same rich reporting (level
colors, location, error codes) as the built-in analyzer.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'source'))
from cpyte.extension_hooks import Diagnostic, DiagnosticSeverity, SemanticHook


class AsyncSemanticHook(SemanticHook):
    """Semantic hook for async function validation."""

    def should_visit_node(self, node):
        """Check if this is an async function."""
        return hasattr(node, 'is_async') and node.is_async

    def visit_node(self, node, context):
        """Validate async function semantics.

        Returns a list of :class:`Diagnostic`. Diagnostics with an ERROR or
        FATAL severity fail the build; WARNING/NOTE are shown but non-fatal.
        """
        diagnostics = []

        # Check that async functions return Promise types
        if hasattr(node, 'rettype') and node.rettype:
            if not node.rettype.endswith('Promise') and not node.rettype.endswith('Future'):
                diagnostics.append(
                    Diagnostic.error(
                        f"Async function '{node.name}' should return Promise or Future "
                        f"type, but returns '{node.rettype}'",
                        code="ASYNC001",
                        package="example_package",
                    )
                )

        return diagnostics

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


def get_hooks():
    """Return list of semantic hooks provided by this package."""
    return [
        AsyncSemanticHook("example_package"),
    ]
