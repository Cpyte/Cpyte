import ast
import os
import re

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
        'malloc':  ('void*', [('size', 'int64')]),
        'calloc':  ('void*', [('nmemb', 'int64'), ('size', 'int64')]),
        'realloc': ('void*', [('ptr', 'void*'), ('size', 'int64')]),
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
    r'(?:CG_EXTERN|CF_EXPORT|EXTERN_C|extern)\s+'
    r'([\w\s\*]+?)\s+'          # return type (lazy)
    r'(?:__nullable|__nonnull|__null_unspecified|__kindof)\s+'
    r'(\w+)\s*'                 # function name
    r'\(([^)]*)\)'              # parameters
    r'\s*;',
    re.DOTALL
)

_HEADER_PATTERN_CF = re.compile(
    r'(?:CF_EXPORT)\s+'
    r'([\w\s\*]+)\s+'          # return type
    r'(\w+)\s*'                # function name  
    r'\(([^)]*)\)'             # parameters
    r'\s*;',
    re.DOTALL
)

_HEADER_PATTERN_CG = re.compile(
    r'(?:CG_EXTERN)\s+'
    r'([\w\s\*]+?)\s+'          # return type (lazy)
    r'(?:__nullable|__nonnull|__null_unspecified|__kindof)?\s*'
    r'(\w+)\s*'                # function name
    r'\(([^)]*)\)'             # parameters
    r'\s*;',
    re.DOTALL
)

_HEADER_PATTERN_ALT = re.compile(
    r'(\w[\w\s\*]*)\s+(\w+)\s*\(([^)]*)\)\s*;',
    re.DOTALL
)


def _add_symbol(symbols, m):
    raw_ret = m.group(1).strip()
    fname = m.group(2).strip()
    raw_params = m.group(3).strip()
    ret_type = _c_type_to_lang(raw_ret)
    if not raw_params or raw_params.strip() == 'void':
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
                # Handle * attached to param name (e.g., "void *ptr" -> tokens=['void', '*ptr'])
                type_tokens = tokens[:-1]
                pname = tokens[-1]
                while pname.startswith('*'):
                    type_tokens.append('*')
                    pname = pname[1:]
                ptype = _c_type_to_lang(' '.join(type_tokens))
                if not pname:
                    pname = f'p{len(params)}'
            if ptype is None:
                continue
            params.append((pname, ptype))
    if ret_type is not None:
        symbols[fname] = (ret_type, params, vararg)


# Pattern for CF_EXPORT const variable declarations (e.g., kCFRunLoopCommonModes)
_CONST_VAR_PATTERN = re.compile(
    r'(?:CF_EXPORT|CG_EXTERN|extern)\s+const\s+(\w+)\s+(\w+)\s*;',
    re.DOTALL
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


_INCLUDE_PATTERN = re.compile(r'#\s*include\s+[<"](\S+)[>"]')


def _resolve_include(include_path, current_file, search_paths):
    """Resolve a #include to an absolute file path."""
    # Quote includes: search relative to current file first
    if not include_path.startswith('<'):
        dirpath = os.path.dirname(current_file)
        candidate = os.path.normpath(os.path.join(dirpath, include_path))
        if os.path.exists(candidate):
            return candidate

    # Try framework paths (e.g., <CoreGraphics/CGEventTypes.h>)
    parts = include_path.split('/')
    if len(parts) >= 2:
        framework_name = parts[0]
        header_rel = '/'.join(parts[1:])
        for sdk in search_paths:
            for fw_subdir in (
                f'{framework_name}.framework/Headers',
                f'{framework_name}.framework/Versions/A/Headers',
            ):
                candidate = os.path.join(sdk, 'System/Library/Frameworks', fw_subdir, header_rel)
                if os.path.exists(candidate):
                    return candidate

    # Search standard SDK include paths
    for sdk in search_paths:
        for subdir in ('usr/include', ''):
            candidate = os.path.join(sdk, subdir, include_path)
            if os.path.exists(candidate):
                return candidate

    # Search framework private headers
    if len(parts) >= 2:
        framework_name = parts[0]
        header_rel = '/'.join(parts[1:])
        for sdk in search_paths:
            candidate = os.path.join(sdk, 'System/Library/Frameworks',
                                     f'{framework_name}.framework/PrivateHeaders', header_rel)
            if os.path.exists(candidate):
                return candidate

    return None


def _normalize_header_content(content):
    """Strip attributes and flatten multi-line declarations for easier parsing."""
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    # Remove known attribute macros with balanced parens
    attr_prefixes = [
        'API_AVAILABLE', 'API_UNAVAILABLE', 'API_DEPRECATED',
        'CF_AVAILABLE', 'CG_AVAILABLE_STARTING',
        'NS_AVAILABLE', 'NS_DEPRECATED',
        '__OSX_AVAILABLE_STARTING', '__TVOS_AVAILABLE_STARTING', '__IOS_AVAILABLE_STARTING',
        'SWIFT_UNAVAILABLE',
        'CF_BRIDGED_TYPE',
    ]
    for prefix in attr_prefixes:
        pattern = re.compile(re.escape(prefix) + r'\s*\(')
        while True:
            m = pattern.search(content)
            if not m:
                break
            start = m.start()
            depth = 1
            i = m.end()
            while i < len(content) and depth > 0:
                if content[i] == '(':
                    depth += 1
                elif content[i] == ')':
                    depth -= 1
                i += 1
            content = content[:start] + content[i:]
    # Remove __attribute__((...)) 
    content = re.sub(r'__attribute__\s*\(\([^)]*\)\)', '', content)
    # Remove single keywords
    for kw in ['nullable', 'nonnull', '__nullable', '__nonnull', '__null_unspecified',
               '__kindof', 'CF_RETURNS_RETAINED', 'CF_RETURNS_NOT_RETAINED',
               'NS_REQUIRES_NIL_TERMINATION', 'CF_BRIDGED_TRANSFER']:
        content = re.sub(r'\b' + kw + r'\b', '', content)
    # Remove remaining stray parens (from partially removed attributes): ) followed by )
    content = re.sub(r'\)\s*\)', ')', content)
    # Flatten continuation lines
    lines = content.split('\n')
    result = []
    for line in lines:
        if result and not line.strip():
            result.append(line)
            continue
        if result and (line.startswith('    ') or line.startswith('\t')):
            result[-1] = result[-1] + ' ' + line.strip()
        else:
            result.append(line)
    return '\n'.join(result)


def parse_header_file(filepath, search_paths=None, _processed=None):
    if _processed is None:
        _processed = set()
    if filepath in _processed:
        return {}, 'h', {}, None, set()
    _processed.add(filepath)

    with open(filepath) as f:
        content = f.read()

    # Normalize: join continuation lines and strip attributes
    content = _normalize_header_content(content)

    # Run generic pattern first, then let specific pattens overwrite for better accuracy
    symbols = {}
    for m in _HEADER_PATTERN_ALT.finditer(content):
        _add_symbol(symbols, m)
    for m in _HEADER_PATTERN.finditer(content):
        _add_symbol(symbols, m)
    for m in _HEADER_PATTERN_CF.finditer(content):
        _add_symbol(symbols, m)
    for m in _HEADER_PATTERN_CG.finditer(content):
        _add_symbol(symbols, m)
    # Extract exported constant variables (e.g., kCFRunLoopCommonModes)
    var_names = set()
    for m in _CONST_VAR_PATTERN.finditer(content):
        var_type = m.group(1).strip()
        var_name = m.group(2).strip()
        mapped = _c_type_to_lang(var_type)
        if mapped:
            symbols[var_name] = (mapped, [], False)
            var_names.add(var_name)

    # Extract constants from the current file first (defines only, not enums yet)
    constants, macros = _extract_defines(content)

    # Add function-like macros as symbols
    for name, desc in macros.items():
        symbols[name] = desc

    # Process includes recursively FIRST, so enum resolution can use their constants
    if search_paths:
        for m in _INCLUDE_PATTERN.finditer(content):
            inc_path = m.group(1)
            inc_file = _resolve_include(inc_path, filepath, search_paths)
            if inc_file:
                sub_sym, _, sub_const, _, sub_vars = parse_header_file(inc_file, search_paths, _processed)
                for k, v in sub_sym.items():
                    symbols.setdefault(k, v)
                var_names.update(sub_vars)
                constants.update(sub_const)

    # Now extract enum constants from current file, with access to all merged constants
    enum_consts = _extract_enum_constants(content, constants)
    constants.update(enum_consts)

    framework = _framework_name_from_path(filepath)
    return symbols, 'h', constants, framework, var_names


def _extract_defines(content):
    """Extract #define integer constants and function-like macros."""
    constants = {}
    macros = {}
    for m in re.finditer(r'#\s*define\s+(\w+)\s+(0[xX][0-9a-fA-F]+|\d+)', content):
        name, val = m.group(1), m.group(2)
        try:
            constants[name] = int(val, 0)
        except ValueError:
            pass
    # Function-like macros: #define NAME(params) body
    for m in re.finditer(r'#\s*define\s+(\w+)\s*\(([^)]*)\)\s*(.*?)(?:\n|$)', content):
        name, params, _body = m.group(1), m.group(2), m.group(3)
        param_list = [p.strip() for p in params.split(',') if p.strip()]
        if param_list:
            macros[name] = ('int', [(p, 'int') for p in param_list], False)
    return constants, macros


def _extract_enum_constants(content, known_constants=None):
    """Extract enum constant values, optionally resolving references via known_constants."""
    if known_constants is None:
        known_constants = {}
    constants = {}

    enum_block_re = re.compile(
        r'(?:typedef\s+)?'
        r'(?:CF_ENUM\s*\([^)]+\)|enum\s+(?:\w+\s*)?(?::\s*\w+\s*)?)'
        r'\s*(\{)',
        re.DOTALL
    )

    pos = 0
    while True:
        m = enum_block_re.search(content, pos)
        if not m:
            break
        brace_start = m.start(1)
        depth = 1
        i = brace_start + 1
        while i < len(content) and depth > 0:
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
            elif content[i] == '/' and i + 1 < len(content):
                if content[i+1] == '/':
                    nl = content.find('\n', i)
                    i = nl if nl != -1 else len(content)
                    continue
                elif content[i+1] == '*':
                    end = content.find('*/', i + 2)
                    i = end + 1 if end != -1 else len(content)
                    continue
            i += 1
        if depth == 0:
            body = content[brace_start + 1 : i - 1]
            items = _split_enum_body(body)
            auto_val = 0
            for item in items:
                parts = item.split('=', 1)
                name = parts[0].strip()
                if not name or not name.isidentifier():
                    continue
                if len(parts) > 1:
                    val = parts[1].strip().rstrip(',')
                    const_val = _resolve_int(val, known_constants)
                    if const_val is not None:
                        constants[name] = const_val
                        auto_val = const_val + 1
                    else:
                        auto_val += 1
                else:
                    constants[name] = auto_val
                    auto_val += 1
        pos = i if depth == 0 else brace_start + 1

    return constants


def _resolve_int(val, known_constants):
    """Try to resolve an integer value, following references to other constants."""
    val = val.strip()
    try:
        if val.startswith('0x') or val.startswith('0X'):
            return int(val, 16)
        if val.startswith('-') and val[1:].isdigit():
            return int(val)
        if val.isdigit():
            return int(val)
    except ValueError:
        pass
    if val in known_constants:
        return known_constants[val]
    for name, cval in known_constants.items():
        if name in val:
            try:
                expr = val.replace(name, str(cval))
                return ast.literal_eval(expr)
            except (ValueError, SyntaxError, MemoryError, TypeError):
                pass
    return None


def _split_enum_body(body):
    """Split enum body into individual items, respecting nested parens and comments."""
    items = []
    depth = 0
    start = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == '(' or ch == '<':
            depth += 1
        elif ch == ')' or ch == '>':
            depth -= 1
        elif ch == ',' and depth == 0:
            items.append(body[start:i])
            start = i + 1
        elif ch == '/' and i + 1 < len(body):
            if body[i+1] == '/':
                nl = body.find('\n', i)
                i = nl if nl != -1 else len(body)
            elif body[i+1] == '*':
                end = body.find('*/', i + 2)
                i = end + 1 if end != -1 else len(body)
        i += 1
    remaining = body[start:i].strip()
    if remaining:
        items.append(remaining)
    # Strip comments from each item
    result = []
    for item in items:
        item = re.sub(r'/\*.*?\*/', '', item, flags=re.DOTALL)
        item = re.sub(r'//.*', '', item)
        item = item.strip()
        if item:
            result.append(item)
    return result


def _framework_name_from_path(filepath):
    parts = filepath.replace('\\', '/').split('/')
    for i, p in enumerate(parts):
        if p.endswith('.framework'):
            return p[:-len('.framework')]
    return None


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
    'CGColorRef': 'void*',
    'CGColorSpaceRef': 'void*',
    'CGEventTapCallBack': 'void*',
    'CGContextRef': 'void*',
    'CGDataProviderRef': 'void*',
    'CGDisplayStreamRef': 'void*',
    'CGEventRef': 'void*',
    'CGEventSourceRef': 'void*',
    'CGImageRef': 'void*',
    'CGPathRef': 'void*',
    'CGPatternRef': 'void*',
    'CGPDFDocumentRef': 'void*',
    'CGPDFPageRef': 'void*',
    'CGFontRef': 'void*',
    'CGLayerRef': 'void*',
    'CGPSConverterRef': 'void*',
    'CFAllocatorRef': 'void*',
    'CFArrayRef': 'void*',
    'CFAttributedStringRef': 'void*',
    'CFBooleanRef': 'void*',
    'CFCalendarRef': 'void*',
    'CFCharacterSetRef': 'void*',
    'CFDataRef': 'void*',
    'CFDateRef': 'void*',
    'CFDictionaryRef': 'void*',
    'CFErrorRef': 'void*',
    'CFLocaleRef': 'void*',
    'CFMachPortRef': 'void*',
    'CFMutableArrayRef': 'void*',
    'CFMutableDataRef': 'void*',
    'CFMutableDictionaryRef': 'void*',
    'CFMutableSetRef': 'void*',
    'CFMutableStringRef': 'void*',
    'CFNotificationCenterRef': 'void*',
    'CFNullRef': 'void*',
    'CFNumberRef': 'void*',
    'CFPropertyListRef': 'void*',
    'CFReadStreamRef': 'void*',
    'CFRunLoopRef': 'void*',
    'CFRunLoopSourceRef': 'void*',
    'CFRunLoopTimerRef': 'void*',
    'CFRunLoopObserverRef': 'void*',
    'CFSetRef': 'void*',
    'CFStringRef': 'void*',
    'CFTimeZoneRef': 'void*',
    'CFTypeRef': 'void*',
    'CFURLRef': 'void*',
    'CFUUIDRef': 'void*',
    'CFWriteStreamRef': 'void*',
    'SecIdentityRef': 'void*',
    'SecCertificateRef': 'void*',
    'SecKeyRef': 'void*',
    'SecTrustRef': 'void*',
    'SecPolicyRef': 'void*',
    'IOSurfaceRef': 'void*',
    'CVPixelBufferRef': 'void*',
    'CVBufferRef': 'void*',
    'CVImageBufferRef': 'void*',
    'CVOpenGLBufferRef': 'void*',
    'CVOpenGLTextureRef': 'void*',
    'CVDisplayLinkRef': 'void*',
    'MIDIEndpointRef': 'void*',
    'MIDIClientRef': 'void*',
    'MIDIPortRef': 'void*',
    'AudioQueueRef': 'void*',
    'AudioUnit': 'void*',
    'AudioComponentInstance': 'void*',
    'SecAccessRef': 'void*',
    'SecKeychainRef': 'void*',
    'SecKeychainItemRef': 'void*',
    'SecTrustedApplicationRef': 'void*',
    'SecAccessControlRef': 'void*',
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
        # For unknown opaque pointer types, map to void*
        if base.endswith('Ref') or base == 'OpaqueRef' or base.startswith('Opaque'):
            return 'void*'
        return 'void*'
    if base in _C_UNSIGNED_TYPES:
        return _C_UNSIGNED_TYPES[base]
    mapped = _C_TYPE_MAP.get(base)
    if mapped:
        return mapped
    # For known base names, try with fallback
    # Mac types that end in Ref → void* (opaque pointer type)
    if base.endswith('Ref') or base == 'Boolean':
        return 'void*'
    # Try bool-like types  
    if base in ('bool', 'BOOL', 'Boolean', '_Bool'):
        return 'int'
    if base in ('uint8_t', 'uint16_t', 'uint32_t', 'uint64_t',
                 'int8_t', 'int16_t', 'int32_t', 'int64_t',
                 'CFIndex', 'pid_t', 'size_t', 'NSInteger', 'NSUInteger'):
        return 'int'
    if base in ('float', 'CGFloat'):
        return 'float'
    if base in ('double',):
        return 'float'
    # Unknown types → void* (safe for pointers, works for ints on 64-bit)
    if base:
        return 'void*'
    return None


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
