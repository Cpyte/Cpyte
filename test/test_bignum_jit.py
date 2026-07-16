import sys, os
sys.path.insert(0, '/Users/main/PycharmProjects/WEW/source')
from cpyte._bignum_bc import load_bignum_bc
from cpyte._runtime_bc import load_runtime_bc
from llvmlite import binding as llvm

llvm.initialize_native_target()
llvm.initialize_native_asmprinter()

ir = r"""
declare i8* @bigint_new()
declare void @bigint_print(i8*)
define i32 @main() {
  %b = call i8* @bigint_new()
  call void @bigint_print(i8* %b)
  ret i32 0
}
"""

print("Parsing assembly...", flush=True)
mod = llvm.parse_assembly(ir)
target = llvm.Target.from_default_triple()
mod.triple = target.triple

print("Linking runtime...", flush=True)
llvm.link_modules(mod, load_runtime_bc())
print("Linking bignum...", flush=True)
llvm.link_modules(mod, load_bignum_bc())

print("Verifying...", flush=True)
try:
    mod.verify()
    print("Verified OK", flush=True)
except Exception as e:
    print(f"Verify failed: {e}", flush=True)
    sys.exit(1)

target_machine = target.create_target_machine()
backing_mod = llvm.parse_assembly('')
print("Creating engine...", flush=True)
engine = llvm.create_mcjit_compiler(backing_mod, target_machine)
engine.add_module(mod)
print("Finalizing...", flush=True)
engine.finalize_object()
print("Running static constructors...", flush=True)
engine.run_static_constructors()

print("Getting function address...", flush=True)
import ctypes
fn = ctypes.CFUNCTYPE(ctypes.c_int)(engine.get_function_address('main'))
print("Running main...", flush=True)
result = fn()
print('Result:', result)
