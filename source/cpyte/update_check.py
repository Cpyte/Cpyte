"""Background update check against the Cpyte GitHub repository.

After a CLI task completes successfully, :func:`report_update` prints an
"update available" message when the installed ``__version__`` is older than the
newest release published on GitHub.

The network fetch runs on a daemon thread started by :func:`start_check` so the
check overlaps with the task itself and never delays it. Failures (offline,
rate-limited, malformed payload) are silent.

Set ``CPYTE_NO_UPDATE_CHECK=1`` to disable the check entirely.
"""

import json
import os
import re
import sys
import threading
import urllib.request

try:
    from . import ui
except ImportError:  # running as a plain script (source/mainpie.py)
    from cpyte import ui

_REPO = 'Cpyte/Cpyte'
_API_URL = f'https://api.github.com/repos/{_REPO}/releases/latest'
_RAW_VERSION_URL = (
    f'https://raw.githubusercontent.com/{_REPO}/main/source/cpyte/__init__.py'
)
_RELEASES_URL = f'https://github.com/{_REPO}/releases/latest'

_FETCH_TIMEOUT = 5.0

_state = {'result': None, 'done': threading.Event()}


def parse_version(version):
    """Split a version string into a numeric tuple.

    ``v2.7.0``, ``2.7`` and ``2.7.0+1`` all reduce to their leading numeric
    segments; prerelease suffixes such as ``-dev`` are ignored.
    """
    nums = re.findall(r'\d+', str(version).lstrip('vV'))
    return tuple(int(n) for n in nums) or (0,)


def compare_versions(a, b):
    """Return -1/0/1 if ``a`` is older/equal/newer than ``b``."""
    ta, tb = parse_version(a), parse_version(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def fetch_latest_version(timeout=_FETCH_TIMEOUT):
    """Return the newest released version (without a leading ``v``), or None.

    Tries the GitHub releases API first; if the repo has no releases it falls
    back to reading ``__version__`` from the default branch's ``__init__.py``.
    """
    tag = _fetch_release_tag(timeout)
    if tag:
        return tag
    return _fetch_raw_version(timeout)


def _fetch_release_tag(timeout):
    try:
        req = urllib.request.Request(
            _API_URL,
            headers={
                'User-Agent': 'cpyte-update-check',
                'Accept': 'application/vnd.github+json',
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8', 'replace'))
        tag = data.get('tag_name')
        if tag:
            return str(tag).lstrip('vV')
    except Exception:
        return None
    return None


def _fetch_raw_version(timeout):
    try:
        req = urllib.request.Request(
            _RAW_VERSION_URL, headers={'User-Agent': 'cpyte-update-check'}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode('utf-8', 'replace')
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        if m:
            return str(m.group(1)).lstrip('vV')
    except Exception:
        return None
    return None


def start_check(current_version):
    """Begin fetching the latest release on a background daemon thread."""
    if os.environ.get('CPYTE_NO_UPDATE_CHECK'):
        return
    threading.Thread(
        target=_runner, args=(str(current_version),), daemon=True
    ).start()


def _runner(current_version):
    try:
        latest = fetch_latest_version()
    except Exception:
        latest = None
    _state['result'] = (current_version, latest)
    _state['done'].set()


def report_update(wait=1.0):
    """Print an update-available message when a newer release was found.

    Call after the main task completes. Waits up to ``wait`` seconds for the
    background check to finish; silently skips when it is still pending or
    nothing newer was found.
    """
    if os.environ.get('CPYTE_NO_UPDATE_CHECK'):
        return
    if not _state['done'].is_set() and not _state['done'].wait(wait):
        return
    current, latest = _state['result']
    if not latest or compare_versions(latest, current) <= 0:
        return
    ui.print_warn(f'cpyte {latest} is available — you are on {current}.')
    print(f'  {ui.dim("Update:")} {_RELEASES_URL}', file=sys.stderr)
