"""Small terminal UI helpers: ANSI colors, TTY detection, labeled status.

Colors are applied only when the destination stream is a real terminal, so
piped output stays clean. Standard conventions are honoured:

  NO_COLOR          any value disables color (no-color.org)
  FORCE_COLOR / CLICOLOR_FORCE  any value forces color even when piped
  CLICOLOR=0        disables color
"""

import os
import sys
import traceback

_RESET = '\x1b[0m'

_CODES = {
    'reset': 0, 'bold': 1, 'dim': 2, 'italic': 3, 'underline': 4,
    'black': 30, 'red': 31, 'green': 32, 'yellow': 33,
    'blue': 34, 'magenta': 35, 'cyan': 36, 'white': 37,
    'bright_black': 90, 'bright_red': 91, 'bright_green': 92,
    'bright_yellow': 93, 'bright_blue': 94, 'bright_magenta': 95,
    'bright_cyan': 96, 'bright_white': 97,
}

_UNKNOWN_STYLE = object()


def colors_enabled(stream=None):
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR') or os.environ.get('CLICOLOR_FORCE'):
        return True
    if os.environ.get('CLICOLOR') == '0':
        return False
    stream = stream if stream is not None else sys.stdout
    return bool(getattr(stream, 'isatty', lambda: False)())


def paint(text, *styles, stream=None):
    if not styles or not colors_enabled(stream):
        return text
    codes = []
    for s in styles:
        code = _CODES.get(s, _UNKNOWN_STYLE)
        if code is _UNKNOWN_STYLE:
            continue
        codes.append(str(code))
    if not codes:
        return text
    return '\x1b[%sm%s%s' % (';'.join(codes), text, _RESET)


def ok(text, stream=None):
    return paint(text, 'bright_green', stream=stream)


def err(text, stream=None):
    return paint(text, 'bright_red', stream=stream)


def warn(text, stream=None):
    return paint(text, 'bright_yellow', stream=stream)


def info(text, stream=None):
    return paint(text, 'bright_cyan', stream=stream)


def dim(text, stream=None):
    return paint(text, 'dim', stream=stream)


def bold(text, stream=None):
    return paint(text, 'bold', stream=stream)


def status(text, stream=None):
    marker = paint('▶', 'bright_cyan', stream=stream)
    return marker + ' ' + paint(text, 'dim', stream=stream)


def usage(text, stream=None):
    return paint(text, 'bright_cyan', 'bold', stream=stream)


def print_ok(text, stream=None):
    print(ok(text, stream=stream), file=stream if stream is not None else sys.stdout)


def print_err(text, stream=None):
    print(err(text, stream=stream), file=stream if stream is not None else sys.stderr)


def print_warn(text, stream=None):
    print(warn(text, stream=stream), file=stream if stream is not None else sys.stderr)


def print_status(text, stream=None):
    print(status(text, stream=stream), file=stream if stream is not None else sys.stderr)


def print_usage(text, stream=None):
    print(usage(text, stream=stream), file=stream if stream is not None else sys.stderr)


def paint_usage(text, stream=None):
    """Colorize a multi-line usage block: bold headline, cyan section
    headers (lines ending in ':'), dim option lines."""
    lines = text.rstrip('\n').split('\n')
    painted = []
    for i, ln in enumerate(lines):
        if i == 0:
            painted.append(paint(ln, 'bright_cyan', 'bold', stream=stream))
        elif ln.endswith(':'):
            painted.append(paint(ln, 'bright_cyan', 'bold', stream=stream))
        elif ln.startswith('  '):
            painted.append(paint(ln, 'dim', stream=stream))
        else:
            painted.append(ln)
    return '\n'.join(painted)


def format_traceback(exc=None, stream=None):
    """Render a Python traceback in the cpyte terminal style.

    The header and stack frames are dimmed so the interesting parts stand out:
    the source line stays plain and the final exception line is bright red.

    ``exc`` may be an exception instance; when ``None`` the traceback of the
    exception currently being handled is used.
    """
    if exc is None:
        text = traceback.format_exc()
    else:
        text = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    lines = text.rstrip('\n').split('\n')
    painted = []
    for i, ln in enumerate(lines):
        if i == 0 or ln.startswith('  File '):
            painted.append(paint(ln, 'dim', stream=stream))
        elif ln.strip() and i == len(lines) - 1:
            painted.append(paint(ln, 'bright_red', stream=stream))
        else:
            painted.append(ln)
    return '\n'.join(painted)


def print_traceback(exc=None, stream=None):
    stream = stream if stream is not None else sys.stderr
    print(format_traceback(exc=exc, stream=stream), file=stream)
