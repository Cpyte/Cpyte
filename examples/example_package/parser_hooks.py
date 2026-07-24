"""
Example parser hooks for the example_package.

This demonstrates how packages can extend the parser with custom syntax.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'source'))
from cpyte.extension_hooks import ParserHook


class AsyncParserHook(ParserHook):
    """Parser hook for async/await syntax."""
    
    def __init__(self, package_name: str, hook_path: str = None):
        super().__init__(package_name, hook_path)
    
    def should_handle_statement(self, tokens, pos):
        """Check if this is an async function definition."""
        if pos < len(tokens) and tokens[pos].value == 'async':
            # Check if followed by 'def'
            if pos + 1 < len(tokens) and tokens[pos + 1].value == 'def':
                return True
        return False
    
    def parse_statement(self, tokens, pos, context):
        """Parse async def statement."""
        # Skip 'async' keyword
        pos += 1
        
        # Parse as regular function but mark as async
        from cpyte.astparse import parse_def
        
        node, new_pos = parse_def(tokens, pos)
        node.is_async = True  # Add custom attribute
        
        return node, new_pos
    
    def initialize(self, context):
        """Initialize the parser hook."""
        pass


def get_hooks():
    """Return list of parser hooks provided by this package."""
    return [
        AsyncParserHook("example_package"),
    ]