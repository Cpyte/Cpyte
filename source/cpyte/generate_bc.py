"""Compile a C source file to stripped LLVM bitcode and embed it as
a compressed+base64 Python module, matching the pattern used by
_runtime_bc.py and _bignum_bc.py.

Optimization-friendly knobs
---------------------------
``-O N`` / ``--opt N`` (or env ``CPYTE_BC_OPT_LEVEL``) controls how hard the
embedded bitcode is pre-optimized:

* ``-O0`` embeds raw, un-optimized IR. The per-program pipeline in
  ``compiling.optimize`` then optimizes the runtime together with the user
  program after linking — the friendliest setup for whole-program inlining.
* ``-O3`` (default) emits a fast, self-contained module.

``--canonicalize`` additionally runs LLVM module passes (constant merge,
global dead-code elimination, dead-prototype stripping) on the emitted module,
shrinking it without touching externally-visible symbols.

GMP headers are resolved automatically from the ``gmp-*`` submodule in the
repository root, falling back to Homebrew, then common prefixes — so the
build no longer relies on a hardcoded ``/opt/homebrew/include``.

Usage:
    python source/cpyte/generate_bc.py source/cpyte/runtime.c   source/cpyte/_runtime_bc.py
    python source/cpyte/generate_bc.py source/cpyte/bignum.c    source/cpyte/_bignum_bc.py
    python source/cpyte/generate_bc.py -O0 source/cpyte/bignum.c source/cpyte/_bignum_bc.py
"""

import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

from llvmlite import binding

try:
    from .ui import print_ok
except ImportError as e:
    print_ok = print

def _remove_probe_stack_ir(llvm_ir: str) -> str:
    return re.sub(r'\s+"probe-stack"="[^"]*"', '', llvm_ir)


def _repo_root() -> Path:
    """Repository root: source/cpyte/generate_bc.py -> <repo>/."""
    return Path(__file__).resolve().parents[2]


def _find_gmp_include(explicit: str | None = None) -> str | None:
    """Locate a directory containing ``gmp.h``.

    Resolution order:
      1. explicit ``--gmp-include`` / ``CPYTE_GMP_INCLUDE``
      2. the ``gmp-*`` submodule checked out in the repository root
      3. Homebrew's ``gmp`` formula (``brew --prefix gmp``)
      4. common prefixes (``/opt/homebrew/include``, ``/usr/local/include``)
    """
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    root = _repo_root()
    if root.is_dir():
        for d in sorted(root.glob('gmp-*')):
            candidates.append(str(d))
    brew = shutil.which('brew')
    if brew:
        try:
            prefix = subprocess.run(
                [brew, '--prefix', 'gmp'], capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            prefix = ''
        if prefix:
            candidates.append(os.path.join(prefix, 'include'))
    candidates.extend(['/opt/homebrew/include', '/usr/local/include'])
    seen = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if os.path.isfile(os.path.join(cand, 'gmp.h')):
            return cand
    return None


def _needs_gmp_headers(c_path: str, gmp_include: str | None) -> bool:
    if gmp_include:
        return True
    try:
        with open(c_path, encoding='utf-8', errors='replace') as f:
            return '#include <gmp.h>' in f.read()
    except OSError:
        return False


def _canonicalize_module(mod) -> None:
    """Remove unused globals and merge identical constants from the module.

    Only safe, non-behavior-changing passes are used: externally-visible
    (linkonce/weak/external) symbols are never internalized, so linked-in
    user modules can still resolve them.
    """
    binding.initialize_native_target()
    binding.initialize_native_asmprinter()
    target = binding.Target.from_default_triple()
    target_machine = target.create_target_machine()
    pto = binding.create_pipeline_tuning_options(speed_level=0)
    pb = binding.create_pass_builder(target_machine, pto)
    pm = binding.create_new_module_pass_manager()
    pm.add_constant_merge_pass()
    pm.add_global_dead_code_eliminate_pass()
    pm.add_strip_dead_prototype_pass()
    pm.run(mod, pb)


def compile_to_bitcode(c_path: str, target_triple: str | None = None,
                       cpu: str | None = None, opt_level: int = 3,
                       opt_size: bool = False,
                       gmp_include: str | None = None,
                       canonicalize: bool = False) -> bytes:
    c_path = str(Path(c_path).resolve())
    clang_opt = max(0, min(int(opt_level), 3))
    with tempfile.TemporaryDirectory() as tmp:
        ll_path = Path(tmp) / 'out.ll'
        cmd = [
            'clang', '-S', '-emit-llvm',
            '-o', str(ll_path),
            '-fno-stack-protector',
        ]
        if opt_size:
            cmd.append('-Oz')
        else:
            cmd.append(f'-O{clang_opt}')
        if _needs_gmp_headers(str(c_path), gmp_include):
            inc = _find_gmp_include(gmp_include)
            if inc:
                cmd.extend(['-I', inc])
        if target_triple:
            cmd.extend(['-target', target_triple])
        if cpu:
            cmd.append(f'-mcpu={cpu}')
        cmd.append(str(c_path))
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f'clang error: {r.stderr}', file=sys.stderr)
            raise SystemExit(r.returncode)
        text = ll_path.read_text()
        stripped = _remove_probe_stack_ir(text)
        mod = binding.parse_assembly(stripped)
        if canonicalize and clang_opt >= 1:
            _canonicalize_module(mod)
        raw = mod.as_bitcode()
    return raw


def make_module(source_c: str, out_py: str, module_name: str, **opts) -> None:
    target_triple = opts.pop('target_triple', None)
    if not target_triple:
        target_triple = binding.Target.from_default_triple().triple
    raw = compile_to_bitcode(source_c, target_triple, **opts)
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
    print_ok(f'Wrote {out_py}')


def _default_module_name(out_py: str) -> str:
    stem = Path(out_py).stem
    for suffix, name in (('_bignum_bc', 'bignum'),
                         ('_runtime_bc', 'runtime'),
                         ('_gc_bc', 'gc')):
        if stem.endswith(suffix):
            return name
    return stem.replace('_', '')


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description='Compile a C file to optimized, embedded LLVM bitcode.')
    parser.add_argument('source', help='input .c file')
    parser.add_argument('output', help='output .py module')
    parser.add_argument('module', nargs='?', help='module name (default: derived)')
    parser.add_argument('-O', '--opt', type=int, default=int(os.environ.get('CPYTE_BC_OPT_LEVEL', 3)),
                        help='clang optimization level 0-3 (default: 3, env CPYTE_BC_OPT_LEVEL)')
    parser.add_argument('--osize', action='store_true',
                        help='optimize the embedded module for size (-Oz), ignoring speed')
    parser.add_argument('--target', default=None, help='target triple (default: host)')
    parser.add_argument('--cpu', default=None, help='-mcpu value (e.g. apple-m1)')
    parser.add_argument('--gmp-include', default=os.environ.get('CPYTE_GMP_INCLUDE'),
                        help='explicit GMP include dir (default: auto-detect)')
    parser.add_argument('--canonicalize', action='store_true',
                        help='run safe LLVM module passes to shrink the embedded module')
    args = parser.parse_args(argv)

    binding.initialize_native_target()
    binding.initialize_native_asmprinter()
    module_name = args.module or _default_module_name(args.output)
    make_module(
        args.source, args.output, module_name,
        target_triple=args.target, cpu=args.cpu, opt_level=args.opt, opt_size=args.osize,
        gmp_include=args.gmp_include, canonicalize=args.canonicalize,
    )


if __name__ == '__main__':
    main()
