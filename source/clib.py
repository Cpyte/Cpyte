import re
import os

# Function descriptor: (return_type, [(param_name, type), ...], vararg=bool)
# vararg defaults to False

# ── libclang setup ──────────────────────────────────────────────
_LIBCLANG_PATHS = [
    '/Library/Developer/CommandLineTools/usr/lib/libclang.dylib',
    '/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/libclang.dylib',
    '/usr/lib/llvm-*/lib/libclang.so',
    '/usr/lib/libclang.so',
]
_libclang_loaded = False

def _init_libclang():
    global _libclang_loaded
    if _libclang_loaded:
        return True
    for p in _LIBCLANG_PATHS:
        expanded = os.path.expanduser(p)
        if '*' in expanded:
            from glob import glob
            candidates = glob(expanded)
            if candidates:
                expanded = candidates[0]
        if os.path.exists(expanded):
            try:
                import clang.cindex
                clang.cindex.Config.set_library_file(expanded)
                _libclang_loaded = True
                return True
            except Exception:
                continue
    try:
        import clang.cindex
        _libclang_loaded = True
        return True
    except Exception:
        return False

C_LIBRARIES = {
    'stdio': {
        'printf':   ('int', [('fmt', 'str')], True),
        'putchar':  ('int', [('c', 'int')]),
        'getchar':  ('int', []),
        'puts':     ('int', [('s', 'str')]),
        'sprintf':  ('int', [('buf', 'str'), ('fmt', 'str')], True),
        'snprintf': ('int', [('buf', 'str'), ('n', 'int'), ('fmt', 'str')], True),
        'fprintf':  ('int', [('stream', 'void*'), ('fmt', 'str')], True),
        'scanf':    ('int', [('fmt', 'str')], True),
        'sscanf':   ('int', [('s', 'str'), ('fmt', 'str')], True),
    },
    'stdlib': {
        'abs':     ('int', [('x', 'int')]),
        'labs':    ('int', [('x', 'int')]),
        'rand':    ('int', []),
        'srand':   ('void', [('seed', 'int')]),
        'malloc':  ('void*', [('size', 'int')]),
        'calloc':  ('void*', [('nmemb', 'int'), ('size', 'int')]),
        'realloc': ('void*', [('ptr', 'void*'), ('size', 'int')]),
        'free':    ('void', [('ptr', 'void*')]),
        'atoi':    ('int', [('s', 'str')]),
        'atol':    ('int', [('s', 'str')]),
        'atof':    ('float', [('s', 'str')]),
        'exit':    ('void', [('status', 'int')]),
        'system':  ('int', [('cmd', 'str')]),
    },
    'math': {
        'sqrt':   ('float', [('x', 'float')]),
        'sin':    ('float', [('x', 'float')]),
        'cos':    ('float', [('x', 'float')]),
        'tan':    ('float', [('x', 'float')]),
        'asin':   ('float', [('x', 'float')]),
        'acos':   ('float', [('x', 'float')]),
        'atan':   ('float', [('x', 'float')]),
        'atan2':  ('float', [('y', 'float'), ('x', 'float')]),
        'pow':    ('float', [('x', 'float'), ('y', 'float')]),
        'exp':    ('float', [('x', 'float')]),
        'log':    ('float', [('x', 'float')]),
        'log10':  ('float', [('x', 'float')]),
        'floor':  ('float', [('x', 'float')]),
        'ceil':   ('float', [('x', 'float')]),
        'fabs':   ('float', [('x', 'float')]),
        'fmod':   ('float', [('x', 'float'), ('y', 'float')]),
    },
    'string': {
        'strlen':   ('int', [('s', 'str')]),
        'strcmp':   ('int', [('s1', 'str'), ('s2', 'str')]),
        'strncmp':  ('int', [('s1', 'str'), ('s2', 'str'), ('n', 'int')]),
        'strcpy':   ('str', [('dst', 'str'), ('src', 'str')]),
        'strncpy':  ('str', [('dst', 'str'), ('src', 'str'), ('n', 'int')]),
        'strcat':   ('str', [('dst', 'str'), ('src', 'str')]),
        'strncat':  ('str', [('dst', 'str'), ('src', 'str'), ('n', 'int')]),
        'strchr':   ('str', [('s', 'str'), ('c', 'int')]),
        'strstr':   ('str', [('haystack', 'str'), ('needle', 'str')]),
        'strdup':   ('str', [('s', 'str')]),
        'memset':   ('void*', [('s', 'void*'), ('c', 'int'), ('n', 'int')]),
        'memcpy':   ('void*', [('dst', 'void*'), ('src', 'void*'), ('n', 'int')]),
        'memcmp':   ('int', [('s1', 'void*'), ('s2', 'void*'), ('n', 'int')]),
    },
    'time': {
        'time':      ('int', [('t', 'void*')]),
        'clock':     ('int', []),
        'difftime':  ('float', [('t1', 'int'), ('t2', 'int')]),
        'ctime':     ('str', [('t', 'void*')]),
    },
}

_HEADER_PATTERN = re.compile(
    r'(\w[\w\s\*]*)\s+(\w+)\s*\(([^)]*)\)\s*;'
)


def resolve_library(name):
    resolved = C_LIBRARIES.get(name)
    if resolved is None:
        return None
    symbols = {}
    for fname, desc in resolved.items():
        ret_type = desc[0]
        params = desc[1]
        vararg = len(desc) > 2 and desc[2]
        symbols[fname] = (ret_type, params, vararg)
    return symbols, 'c'


def parse_header_file(filepath):
    with open(filepath) as f:
        content = f.read()
    symbols = {}
    for m in _HEADER_PATTERN.finditer(content):
        raw_ret = m.group(1).strip()
        fname = m.group(2).strip()
        raw_params = m.group(3).strip()
        ret_type = _c_type_to_lang(raw_ret)
        if not raw_params:
            params = []
            vararg = False
        else:
            parts = _split_params(raw_params)
            params = []
            vararg = False
            for p in parts:
                p = p.strip()
                if p == '...':
                    vararg = True
                    continue
                tokens = p.split()
                if len(tokens) == 1:
                    ptype = _c_type_to_lang(tokens[0])
                    pname = ''
                else:
                    ptype = _c_type_to_lang(' '.join(tokens[:-1]))
                    pname = tokens[-1]
                if ptype is None:
                    continue
                params.append((pname or f'p{len(params)}', ptype))
        if ret_type is not None:
            symbols[fname] = (ret_type, params, vararg)
    return symbols, 'h'


_C_TYPE_MAP = {
    'void': 'void',
    'char': 'char',
    'int': 'int',
    'short': 'int',
    'long': 'int',
    'unsigned': 'int',
    'float': 'float',
    'double': 'float',
    'size_t': 'int',
    'FILE': 'void*',
}

_C_PTR_TYPE_MAP = {
    'void': 'void*',
    'char': 'str',
    'int': 'int*',
    'float': 'float*',
    'double': 'double*',
}

_C_UNSIGNED_TYPES = {
    'unsigned char': 'int',
    'unsigned short': 'int',
    'unsigned int': 'int',
    'unsigned long': 'int',
    'unsigned long long': 'int',
}

_C_QUALIFIERS = {'const', 'restrict', 'volatile', '__restrict', '__restrict__', '_Nonnull', '_Nullable'}


def _c_type_to_lang(raw):
    tokens = raw.replace('*', ' * ').split()
    cleaned = []
    for t in tokens:
        if t not in _C_QUALIFIERS:
            cleaned.append(t)
    if not cleaned:
        return None

    ptr_count = 0
    while cleaned and cleaned[-1] == '*':
        cleaned.pop()
        ptr_count += 1

    base = ' '.join(cleaned)
    if ptr_count > 0:
        if base in _C_UNSIGNED_TYPES:
            return 'int*'
        mapped = _C_PTR_TYPE_MAP.get(base)
        if mapped:
            return mapped
        return 'void*'
    if base in _C_UNSIGNED_TYPES:
        return _C_UNSIGNED_TYPES[base]
    return _C_TYPE_MAP.get(base)


def _split_params(s):
    depth = 0
    parts = []
    cur = []
    for ch in s:
        if ch in '({[':
            depth += 1
            cur.append(ch)
        elif ch in ')}]':
            depth -= 1
            cur.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append(''.join(cur).strip())
    return parts


def parse_c_source(filepath):
    if not _init_libclang():
        return _parse_c_source_regex(filepath)

    import clang.cindex as ci
    try:
        idx = ci.Index.create()
        tu = idx.parse(filepath)
    except Exception:
        return _parse_c_source_regex(filepath)

    symbols = {}
    for c in tu.cursor.get_children():
        if c.kind != ci.CursorKind.FUNCTION_DECL:
            continue

        if c.storage_class == ci.StorageClass.STATIC:
            continue
        if c.spelling == 'main':
            continue
        if c.spelling in _C_KEYWORDS:
            continue

        ret_type = _c_type_to_lang(c.result_type.spelling)
        if ret_type is None:
            continue

        vararg = False
        try:
            vararg = c.type.is_function_variadic()
        except AssertionError:
            vararg = False

        params = []
        for p in c.get_arguments():
            ptype = _c_type_to_lang(p.type.spelling)
            if ptype is None:
                continue
            params.append((p.spelling or f'p{len(params)}', ptype))

        symbols[c.spelling] = (ret_type, params, vararg)

    return symbols, 'c'


def _parse_c_source_regex(filepath):
    with open(filepath) as f:
        content = f.read()
    symbols = {}
    for m in _C_SRC_RE.finditer(content):
        raw_ret = m.group(1).strip()
        fname = m.group(2).strip()
        raw_params = m.group(3).strip()

        if fname in _C_KEYWORDS or fname == 'main':
            continue

        ret_type = _c_type_to_lang(raw_ret)
        if ret_type is None:
            continue

        if not raw_params or raw_params == 'void':
            params = []
            vararg = False
        else:
            parts = _split_params(raw_params)
            params = []
            vararg = False
            for p in parts:
                p = p.strip()
                if p == '...':
                    vararg = True
                    continue
                tokens = p.split()
                if len(tokens) == 1:
                    ptype = _c_type_to_lang(tokens[0])
                    pname = ''
                else:
                    ptype = _c_type_to_lang(' '.join(tokens[:-1]))
                    pname = tokens[-1]
                if ptype is None:
                    continue
                params.append((pname or f'p{len(params)}', ptype))
        symbols[fname] = (ret_type, params, vararg)
    return symbols, 'c'

_C_KEYWORDS = {
    'if', 'while', 'for', 'switch', 'return', 'sizeof',
    'typedef', 'struct', 'union', 'enum', 'case', 'default',
    'break', 'continue', 'goto', 'do', 'else',
}

_C_SRC_RE = re.compile(
    r'(?:(?:static|inline|extern)\s+)*'
    r'([\w\s\*]+?)\s+'
    r'(\w+)\s*\(([^)]*)\)\s*(?:\[[^\]]*\])?\s*\{'
)
