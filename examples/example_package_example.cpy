# Example using @package import with extension system
# This demonstrates the package extension capabilities via CPM

# Import the example package (should load its manifest and extensions)
import @example_package

# Test that the new keywords are recognized from the package
def test_keywords():
    # The 'async', 'await', 'defer' keywords should be recognized
    # even though they're from the package
    async_val = 1
    await_val = 2
    defer_val = 3
    return async_val + await_val + defer_val

def main():
    print(test_keywords())