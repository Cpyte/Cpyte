import subprocess
import sys
import os
import re
import shutil
from .ui import print_err


_CANDIDATES = ['cc', 'clang', 'gcc']

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

_ERR_HINTS = (
    ': error:', ': fatal error:',
    'error:', 'fatal error:',
    'ld: ', 'Undefined symbols',
    'framework not found', 'library not found',
    'duplicate symbol', 'clang: error:',
)


def format_cc_diag(stderr, max_lines=12):
    """Reduce a compiler/linker stderr dump to the actionable diagnostic lines.

    Keeps the ``file:line:col: error:`` diagnostics (plus their source
    snippet/caret context) and drops the surrounding noise, so a failed link
    surfaces as a short, readable message instead of a huge clang dump.
    """
    text = _ANSI_RE.sub('', stderr or '').strip()
    if not text:
        return 'no output'
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]

    def _is_context(ln):
        stripped = ln.lstrip()
        return (stripped.startswith('^') or stripped.startswith('~')
                or (stripped and not stripped.endswith(':')
                    and len(stripped) <= 100
                    and 'error' not in stripped.lower()
                    and ': ' not in ln))

    shown = []
    for i, ln in enumerate(lines):
        if any(h in ln for h in _ERR_HINTS):
            shown.append(ln)
            nxt = i + 1
            while nxt < len(lines) and len(shown) < max_lines and _is_context(lines[nxt]):
                shown.append(lines[nxt])
                nxt += 1
    if not shown:
        shown = lines
    if len(shown) > max_lines:
        shown = shown[:max_lines]
        shown.append(f'... {len(lines) - max_lines} more line(s) suppressed '
                     '(run with --verbose for the full output)')
    return '\n'.join(shown)


def _cc_error(prefix, stderr):
    print(f'{prefix}{format_cc_diag(stderr)}', file=sys.stderr)
    raise SystemExit(1)


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


def _is_clang(cc):
    try:
        r = subprocess.run([cc, '--version'], capture_output=True, text=True, timeout=5)
        return 'clang' in (r.stdout + r.stderr).lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


class Linker:
    def __init__(self, cc=None, lto=False):
        self._cc = cc or find_linker()
        self._lto = bool(lto)
        if self._lto and not _is_clang(self._cc):
            print_err(
                f'error: LTO requires a clang-compatible compiler, but found {self._cc}'
            )
            raise SystemExit(1)

    @property
    def cc(self):
        return self._cc

    def compile_c(self, src, output=None, opt_level=3, opt_size=False, debug=False, pic=False, eh=False):
        if output is None:
            base = src.rsplit('.', 1)[0] if '.' in src else src
            output = base + '.o'
        cmd = [self._cc, '-c']
        if debug:
            cmd.append('-g')
        if pic:
            cmd.append('-fPIC')
        if eh:
            cmd.append('-fexceptions')
            cmd.append('-funwind-tables')
        if self._lto:
            cmd.append('-flto')
        if opt_size:
            cmd.append('-Oz')
        elif opt_level is not None:
            cmd.append(f'-O{opt_level}')
        cmd.extend(['-o', output, src])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            _cc_error(f'error compiling {src}: ', r.stderr)
        return output

    def link(self, objects, output, libraries=None, library_paths=None,
             shared=False, debug=False, opt_level=3, opt_size=False, frameworks=None, pic=False):
        cmd = [self._cc]
        if shared:
            cmd.append('-shared')
        if pic:
            cmd.append('-fPIC')
        if debug:
            cmd.append('-g')
        if self._lto:
            cmd.append('-flto')
        if opt_size:
            cmd.append('-Oz')
        elif opt_level is not None:
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
            _cc_error('link error: ', r.stderr)
        return output


def build(objects, output=None, libraries=None, library_paths=None,
          shared=False, debug=False, opt_level=3, cc=None, frameworks=None, pic=False, lto=False):
    linker = Linker(cc, lto=lto)

    final_objects = []
    for src in objects:
        if src.endswith('.c'):
            obj = linker.compile_c(src, opt_level=opt_level, debug=debug, pic=pic)
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
        frameworks=frameworks, pic=pic,
    )
