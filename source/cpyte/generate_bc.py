"""Compile a C source file to stripped LLVM bitcode and embed it as
a compressed+base64 Python module, matching the pattern used by
_runtime_bc.py and _bignum_bc.py.

Usage:
    python source/cpyte/generate_bc.py source/cpyte/runtime.c   source/cpyte/_runtime_bc.py
    python source/cpyte/generate_bc.py source/cpyte/bignum.c    source/cpyte/_bignum_bc.py
"""

import base64
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import zlib
from llvmlite import binding


def _remove_probe_stack_ir(llvm_ir: str) -> str:
    return re.sub(r'\s+"probe-stack"="[^"]*"', '', llvm_ir)


def compile_to_bitcode(c_path: str, target_triple: str | None = None) -> bytes:
    c_path = Path(c_path).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        ll_path = Path(tmp) / 'out.ll'
        cmd = [
            'clang', '-O3', '-S', '-emit-llvm',
            '-o', str(ll_path),
            '-fno-stack-protector',
            str(c_path),
        ]
        # GMP header is in /opt/homebrew/include on Apple Silicon
        if 'bignum' in str(c_path):
            cmd.insert(1, '-I/opt/homebrew/include')
        if target_triple:
            cmd.extend(['-target', target_triple])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f'clang error: {r.stderr}', file=sys.stderr)
            raise SystemExit(r.returncode)
        text = ll_path.read_text()
        stripped = _remove_probe_stack_ir(text)
        mod = binding.parse_assembly(stripped)
        raw = mod.as_bitcode()
    return raw


def make_module(source_c: str, out_py: str, module_name: str) -> None:
    target = binding.Target.from_default_triple()
    raw = compile_to_bitcode(source_c, target.triple)
    compressed = zlib.compress(raw)
    b64 = base64.b64encode(compressed).decode()
    lines = [b64[i:i+80] for i in range(0, len(b64), 80)]
    body = '\n'.join(f'    {l!r}' for l in lines)
    py_code = f'''import base64, zlib
from llvmlite import binding


_B64 = (
{body}
)

def load_{module_name}_bc():
    data = zlib.decompress(base64.b64decode(_B64))
    return binding.parse_bitcode(data)
'''
    Path(out_py).write_text(py_code)
    print(f'Wrote {out_py}')


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(f'Usage: {sys.argv[0]} <source.c> <output.py> [module_name]', file=sys.stderr)
        raise SystemExit(1)
    source_c = sys.argv[1]
    out_py = sys.argv[2]
    module_name = sys.argv[3] if len(sys.argv) > 3 else Path(out_py).stem.replace('_bignum_bc', 'bignum').replace('_runtime_bc', 'runtime').replace('_', '')
    binding.initialize_native_target()
    binding.initialize_native_asmprinter()
    make_module(source_c, out_py, module_name)
