import sys

sys.path.insert(0, "/Users/main/PycharmProjects/WEW/source")
from cpyte._bignum_bc import load_bignum_bc
from cpyte._runtime_bc import _B64
from llvmlite import binding as llvm

llvm.initialize_native_target()
llvm.initialize_native_asmprinter()

import base64
import zlib


def load_runtime_bc():
    data = zlib.decompress(base64.b64decode(_B64))
    return llvm.parse_bitcode(data)


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
backing_mod = llvm.parse_assembly("")
print("Creating engine...", flush=True)
engine = llvm.create_mcjit_compiler(backing_mod, target_machine)
engine.add_module(mod)
print("Finalizing...", flush=True)
engine.finalize_object()
print("Running static constructors...", flush=True)
engine.run_static_constructors()

print("Getting function address...", flush=True)
import ctypes

fn = ctypes.CFUNCTYPE(ctypes.c_int)(engine.get_function_address("main"))
print("Running main...", flush=True)
# bigint_print writes through libc printf/putchar (fd 1), not sys.stdout, so
# capture at the descriptor level with dup2 rather than Python stream objects.
import os
import tempfile

with tempfile.NamedTemporaryFile(delete=False) as cap:
    cap_name = cap.name
cap_fd = os.open(cap_name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
saved = os.dup(1)
os.dup2(cap_fd, 1)
try:
    result = fn()
    ctypes.CDLL(None).fflush(
        None
    )  # flush libc stdout while fd 1 points at the capture file
finally:
    os.dup2(saved, 1)
    os.close(cap_fd)
    os.close(saved)
with open(cap_name, "r") as cap:
    out = cap.read()
os.unlink(cap_name)
print("Result:", result)
# Regression: bignum.c bigint_print used to omit the trailing \n, fusing runs
# together while print_int/print_str emit a newline. Every bigint_print call
# must end with a newline.
if not out.endswith("\n"):
    print(f"FAIL: bigint_print output missing trailing newline: {out!r}")
    sys.exit(1)
print("Newline check OK:", repr(out))
