"""
Example runtime hooks for the example_package.

This demonstrates how packages can extend the runtime with additional code.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'source'))
from cpyte.extension_hooks import RuntimeHook


class AsyncRuntimeHook(RuntimeHook):
    """Runtime hook for async runtime support."""
    
    def __init__(self, package_name: str, hook_path: str = None):
        super().__init__(package_name, hook_path)
    
    def get_runtime_code(self):
        """Return additional runtime code for async support."""
        return """
// Async runtime support for example_package
typedef struct Promise {
    void* value;
    int is_resolved;
    void (*callback)(struct Promise*);
} Promise;

Promise* create_promise() {
    Promise* p = malloc(sizeof(Promise));
    p->value = NULL;
    p->is_resolved = 0;
    p->callback = NULL;
    return p;
}

void resolve_promise(Promise* p, void* value) {
    p->value = value;
    p->is_resolved = 1;
    if (p->callback) {
        p->callback(p);
    }
}
"""
    
    def get_runtime_libraries(self):
        """Return additional runtime libraries to link."""
        return []  # No additional libraries needed for this example
    
    def initialize(self, context):
        """Initialize the runtime hook."""
        pass


def get_hooks():
    """Return list of runtime hooks provided by this package."""
    return [
        AsyncRuntimeHook("example_package"),
    ]