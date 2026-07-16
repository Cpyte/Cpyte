import subprocess
import sys
import os
import shutil


_CANDIDATES = ['cc', 'clang', 'gcc']


class LinkerNotFoundError(RuntimeError):
    pass


def find_linker(candidates=None):
    candidates = candidates or _CANDIDATES
    for name in candidates:
        exe = shutil.which(name)
        if exe:
            try:
                r = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return exe
            except (OSError, subprocess.TimeoutExpired):
                continue
    raise LinkerNotFoundError(
        'no C linker found; install clang, gcc, or ensure cc is on PATH'
    )


class Linker:
    def __init__(self, cc=None):
        self._cc = cc or find_linker()

    @property
    def cc(self):
        return self._cc

    def compile_c(self, src, output=None, opt_level=3, debug=False):
        if output is None:
            base = src.rsplit('.', 1)[0] if '.' in src else src
            output = base + '.o'
        cmd = [self._cc, '-c']
        if debug:
            cmd.append('-g')
        if opt_level is not None:
            cmd.append(f'-O{opt_level}')
        cmd.extend(['-o', output, src])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f'error compiling {src}: {r.stderr}', file=sys.stderr)
            raise SystemExit(1)
        return output

    def link(self, objects, output, libraries=None, library_paths=None,
             shared=False, debug=False, opt_level=3, frameworks=None):
        cmd = [self._cc]
        if shared:
            cmd.append('-shared')
        if debug:
            cmd.append('-g')
        if opt_level is not None:
            cmd.append(f'-O{opt_level}')
        cmd.extend(['-o', output] + list(objects))
        for lib in (libraries or []):
            cmd.extend(['-l', lib])
        for path in (library_paths or []):
            cmd.extend(['-L', path])
        for fw in (frameworks or []):
            cmd.extend(['-framework', fw])
        if not shared:
            cmd.append('-lm')
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f'link error: {r.stderr}', file=sys.stderr)
            raise SystemExit(1)
        return output


def build(objects, output=None, libraries=None, library_paths=None,
          shared=False, debug=False, opt_level=3, cc=None, frameworks=None):
    linker = Linker(cc)

    final_objects = []
    for src in objects:
        if src.endswith('.c'):
            obj = linker.compile_c(src, opt_level=opt_level, debug=debug)
            final_objects.append(obj)
        else:
            final_objects.append(src)

    if output is None:
        base = None
        for o in objects:
            name = o.rsplit('.', 1)[0] if '.' in o else o
            base = name
        output = base or 'a.out'

    return linker.link(
        final_objects, output,
        libraries=libraries, library_paths=library_paths,
        shared=shared,
        debug=debug, opt_level=opt_level,
        frameworks=frameworks,
    )
