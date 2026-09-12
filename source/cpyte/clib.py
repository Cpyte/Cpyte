import ast
import ctypes
import os
import re
import subprocess
import sys
import tempfile

# Function descriptor: (return_type, [(param_name, type), ...], vararg=bool)
# vararg defaults to False. In C_LIBRARIES the types are C source-level types
# (e.g. 'char*', 'size_t', 'double'); they are converted to cpyte types through
# the same strict, platform-aware mapper used for parsed headers, so a hand
# written entry can never disagree with what libclang would have produced.

# ── libclang setup (cross-platform) ─────────────────────────────
_LIBCLANG_PATHS = [
    # macOS: Xcode Command Line Tools / Xcode toolchains
    "/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
    "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/libclang.dylib",
    # Linux: distro LLVM installs (Debian/Ubuntu/Fedora/Arch, x86-64 + arm64)
    "/usr/lib/llvm-*/lib/libclang.so",
    "/usr/lib/llvm-*/lib/libclang.so.*",
    "/usr/lib/x86_64-linux-gnu/libclang-*.so.*",
    "/usr/lib/aarch64-linux-gnu/libclang-*.so.*",
    "/usr/lib/libclang.so",
    "/usr/lib64/libclang.so",
    "/usr/local/lib/libclang.so",
    # Windows / generic (also tried by name on PATH, then clang.cindex defaults)
    "libclang.dll",
    "libclang.so",
    "libclang.dylib",
]
_libclang_loaded = False


def _find_libclang():
    """Locate a libclang shared library on any platform, or None."""
    try:
        import ctypes.util as _cutil

        found = _cutil.find_library("clang")
        if found:
            return found
    except Exception:
        pass
    import shutil

    for p in _LIBCLANG_PATHS:
        expanded = os.path.expanduser(p)
        if os.path.isabs(expanded) and "*" in expanded:
            from glob import glob

            matches = sorted(glob(expanded))
            if not matches:
                continue
            expanded = matches[0]
        if os.path.exists(expanded):
            return expanded
        if not os.path.isabs(expanded) and shutil.which(expanded):
            return expanded
    return None


def _init_libclang():
    global _libclang_loaded
    if _libclang_loaded:
        return True
    library_file = _find_libclang()
    try:
        import clang.cindex  # type: ignore[reportMissingImports]

        if library_file:
            clang.cindex.Config.set_library_file(library_file)
        _libclang_loaded = True
        return True
    except Exception:
        return False


# Hand-written fallback libraries. Types here are C source-level types (e.g.
# 'char*', 'size_t', 'double') — they are converted to cpyte types through the
# strict C→cpyte mapper (see `_hardcoded_symbols`), so a curated entry can
# never disagree with what parsing the real header with libclang would produce.
C_LIBRARIES = {
    "stdio": {
        "printf": ("int", [("fmt", "char*")], True),
        "putchar": ("int", [("c", "int")]),
        "getchar": ("int", []),
        "puts": ("int", [("s", "char*")]),
        "sprintf": ("int", [("buf", "char*"), ("fmt", "char*")], True),
        "snprintf": (
            "int",
            [("buf", "char*"), ("n", "size_t"), ("fmt", "char*")],
            True,
        ),
        "fprintf": ("int", [("stream", "FILE*"), ("fmt", "char*")], True),
        "scanf": ("int", [("fmt", "char*")], True),
        "sscanf": ("int", [("s", "char*"), ("fmt", "char*")], True),
    },
    "stdlib": {
        "abs": ("int", [("x", "int")]),
        "labs": ("long", [("x", "long")]),
        "rand": ("int", []),
        "srand": ("void", [("seed", "unsigned")]),
        "malloc": ("void*", [("size", "size_t")]),
        "calloc": ("void*", [("nmemb", "size_t"), ("size", "size_t")]),
        "realloc": ("void*", [("ptr", "void*"), ("size", "size_t")]),
        "free": ("void", [("ptr", "void*")]),
        "atoi": ("int", [("s", "char*")]),
        "atol": ("long", [("s", "char*")]),
        "atof": ("double", [("s", "char*")]),
        "exit": ("void", [("status", "int")]),
        "system": ("int", [("cmd", "char*")]),
    },
    "math": {
        "sqrt": ("double", [("x", "double")]),
        "sin": ("double", [("x", "double")]),
        "cos": ("double", [("x", "double")]),
        "tan": ("double", [("x", "double")]),
        "asin": ("double", [("x", "double")]),
        "acos": ("double", [("x", "double")]),
        "atan": ("double", [("x", "double")]),
        "atan2": ("double", [("y", "double"), ("x", "double")]),
        "pow": ("double", [("x", "double"), ("y", "double")]),
        "exp": ("double", [("x", "double")]),
        "log": ("double", [("x", "double")]),
        "log10": ("double", [("x", "double")]),
        "floor": ("double", [("x", "double")]),
        "ceil": ("double", [("x", "double")]),
        "round": ("double", [("x", "double")]),
        "fabs": ("double", [("x", "double")]),
        "fmod": ("double", [("x", "double"), ("y", "double")]),
    },
    "string": {
        "strlen": ("size_t", [("s", "char*")]),
        "strcmp": ("int", [("s1", "char*"), ("s2", "char*")]),
        "strncmp": ("int", [("s1", "char*"), ("s2", "char*"), ("n", "size_t")]),
        "strcpy": ("char*", [("dst", "char*"), ("src", "char*")]),
        "strncpy": ("char*", [("dst", "char*"), ("src", "char*"), ("n", "size_t")]),
        "strcat": ("char*", [("dst", "char*"), ("src", "char*")]),
        "strncat": ("char*", [("dst", "char*"), ("src", "char*"), ("n", "size_t")]),
        "strchr": ("char*", [("s", "char*"), ("c", "int")]),
        "strstr": ("char*", [("haystack", "char*"), ("needle", "char*")]),
        "strdup": ("char*", [("s", "char*")]),
        "memset": ("void*", [("s", "void*"), ("c", "int"), ("n", "size_t")]),
        "memcpy": ("void*", [("dst", "void*"), ("src", "void*"), ("n", "size_t")]),
        "memcmp": ("int", [("s1", "void*"), ("s2", "void*"), ("n", "size_t")]),
    },
    "time": {
        "time": ("time_t", [("t", "time_t*")]),
        "clock": ("time_t", []),
        "difftime": ("double", [("t1", "time_t"), ("t2", "time_t")]),
        "ctime": ("char*", [("t", "time_t*")]),
    },
}

# Maps bare import names to their C header files for system header parsing
_BUILTIN_LIB_HEADERS = {
    "stdio": "stdio.h",
    "stdlib": "stdlib.h",
    "math": "math.h",
    "string": "string.h",
    "time": "time.h",
    "fcntl": "fcntl.h",
    "unistd": "unistd.h",
    "sys/stat": "sys/stat.h",
    "sys/types": "sys/types.h",
    "sys/socket": "sys/socket.h",
    "sys/mman": "sys/mman.h",
    "signal": "signal.h",
    "errno": "errno.h",
    "assert": "assert.h",
    "ctype": "ctype.h",
    "dirent": "dirent.h",
    "dlfcn": "dlfcn.h",
    "glob": "glob.h",
    "pthread": "pthread.h",
    "pwd": "pwd.h",
    "setjmp": "setjmp.h",
    "stdarg": "stdarg.h",
    "stdint": "stdint.h",
    "stddef": "stddef.h",
    "limits": "limits.h",
    "float": "float.h",
    "locale": "locale.h",
    "tar": "tar.h",
    "zlib": "zlib.h",
    "sys/time": "sys/time.h",
    "sys/wait": "sys/wait.h",
    "sys/resource": "sys/resource.h",
    "sys/ioctl": "sys/ioctl.h",
    "sys/un": "sys/un.h",
    "netdb": "netdb.h",
    "netinet/in": "netinet/in.h",
    "netinet/tcp": "netinet/tcp.h",
    "arpa/inet": "arpa/inet.h",
}

# Cache for parsed system headers (header_name -> symbols dict)
_parsed_system_header_cache = {}

# SDK / include path caches (avoid circular import with semantic_analasis)
_sdk_paths_cache = None
_include_dirs_cache = None


def _sdk_roots():
    """SDK roots with a `usr/include` layout (macOS SDKs), cached.

    Returns [] on platforms without an SDK-layout tree; callers must treat an
    empty list as "no exclusive SDK", not as an error. On macOS this is the
    xcrun SDK plus any Xcode/CLT SDKs on disk. Never touches a compiler.
    """
    if sys.platform != "darwin":
        return []
    roots = []
    try:
        r = subprocess.run(
            ["xcrun", "--show-sdk-path"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            roots.append(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    for base in (
        "/Library/Developer/CommandLineTools/SDKs",
        "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs",
    ):
        if os.path.isdir(base):
            for entry in sorted(os.listdir(base), reverse=True):
                full = os.path.join(base, entry)
                if (
                    os.path.isdir(full)
                    and entry.startswith("MacOSX")
                    and full not in roots
                ):
                    roots.append(full)
    return roots


def _probe_cc_include_dirs():
    """Ask the system C compiler for its default include directories.

    Cross-platform (clang/gcc/mingw): parses the search-list block emitted by
    `cc -E -x c -v -`. This is the source of truth for where `<stdio.h>` etc.
    actually live on the host, so discovery never depends on a macOS SDK.
    """
    dirs = []
    for cc in ("cc", "gcc", "clang"):
        try:
            r = subprocess.run(
                [cc, "-E", "-x", "c", "-v", "-"],
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        output = r.stderr
        if not output:
            output = r.stdout
        m = re.search(
            r"#include <\.\.\.> search starts here:(.*?)\nEnd of search list\.",
            output,
            re.DOTALL,
        )
        if not m:
            continue
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"\s*\((framework|library) directory\)$", "", line)
            if not line.startswith("/") or not os.path.isdir(line):
                continue
            if line not in dirs:
                dirs.append(line)
        if dirs:
            break
    return dirs


def _find_include_dirs():
    """Deduped absolute directories suitable for `-I` / `-isystem`. Cached."""
    global _include_dirs_cache
    if _include_dirs_cache is not None:
        return _include_dirs_cache

    dirs = []
    for root in _sdk_roots():
        cand = os.path.join(root, "usr/include")
        if os.path.isdir(cand) and cand not in dirs:
            dirs.append(cand)
    for d in _probe_cc_include_dirs():
        if d not in dirs:
            dirs.append(d)
    for p in ("/usr/local/include", "/usr/include"):
        if os.path.isdir(p) and p not in dirs:
            dirs.append(p)

    _include_dirs_cache = dirs
    return dirs


def _find_sdk_paths():
    """System roots used for header/framework resolution. Cached.

    Broadest view: macOS SDK roots followed by the compiler's include dirs and
    the standard include directories, deduplicated. Callers that only need
    `-I` dirs should use `_find_include_dirs()` instead.
    """
    global _sdk_paths_cache
    if _sdk_paths_cache is not None:
        return _sdk_paths_cache

    paths = list(_sdk_roots())
    for d in _find_include_dirs():
        if d not in paths:
            paths.append(d)
    for p in ("/usr/local/include", "/usr/include"):
        if os.path.isdir(p) and p not in paths:
            paths.append(p)

    _sdk_paths_cache = paths
    return paths


def _parse_system_header(header_name):
    """Parse a C system header using libclang for full symbol extraction.

    Returns a dict of {func_name: (ret_type, [(param_name, param_type), ...], vararg)}.
    Falls back to None if libclang is unavailable or parsing fails.
    """
    if header_name in _parsed_system_header_cache:
        return _parsed_system_header_cache[header_name]

    if not _init_libclang():
        _parsed_system_header_cache[header_name] = None
        return None

    import clang.cindex as ci  # type: ignore[reportMissingImports]

    include_args = []
    for d in _find_include_dirs():
        include_args.extend(["-isystem", d])
    # Framework search roots (macOS SDKs) so Apple frameworks resolve too.
    for root in _sdk_roots():
        for fw_root in ("System/Library/Frameworks", "Library/Frameworks"):
            fw_dir = os.path.join(root, fw_root)
            if os.path.isdir(fw_dir):
                include_args.extend(["-F", fw_dir])

    fd, tmp_path = tempfile.mkstemp(suffix=".c", prefix="cpyte_sysheader_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"#include <{header_name}>\n")

        try:
            idx = ci.Index.create()
            tu = idx.parse(
                tmp_path,
                args=["-x", "c"] + include_args,
            )
        except Exception:
            _parsed_system_header_cache[header_name] = None
            return None

        symbols = {}
        for c in tu.cursor.get_children():
            if c.kind != ci.CursorKind.FUNCTION_DECL:
                continue
            if c.spelling in _C_KEYWORDS:
                continue

            ret_type = _c_type_to_lang(_canonical_spelling(c.result_type))
            if ret_type is None:
                continue

            vararg = False
            try:
                vararg = c.type.is_function_variadic()
            except Exception:
                pass

            params = []
            for p in c.get_arguments():
                ptype = _c_type_to_lang(_canonical_spelling(p.type))
                # A single unmappable parameter means the whole signature would
                # be wrong; drop the symbol rather than emit a broken extern.
                if ptype is None:
                    params = None
                    break
                pname = p.spelling or f"p{len(params)}"
                params.append((pname, ptype))
            if params is None:
                continue

            symbols[c.spelling] = (ret_type, params, vararg)

        _parsed_system_header_cache[header_name] = symbols
        return symbols

    except Exception:
        _parsed_system_header_cache[header_name] = None
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


_HEADER_PATTERN = re.compile(
    r"(?:CG_EXTERN|CF_EXPORT|EXTERN_C|extern)\s+"
    r"([\w\s\*]+?)\s+"  # return type (lazy)
    r"(?:__nullable|__nonnull|__null_unspecified|__kindof)\s+"
    r"(\w+)\s*"  # function name
    r"\(([^)]*)\)"  # parameters
    r"\s*;",
    re.DOTALL,
)

_HEADER_PATTERN_CF = re.compile(
    r"(?:CF_EXPORT)\s+"
    r"([\w\s\*]+)\s+"  # return type
    r"(\w+)\s*"  # function name
    r"\(([^)]*)\)"  # parameters
    r"\s*;",
    re.DOTALL,
)

_HEADER_PATTERN_CG = re.compile(
    r"(?:CG_EXTERN)\s+"
    r"([\w\s\*]+?)\s+"  # return type (lazy)
    r"(?:__nullable|__nonnull|__null_unspecified|__kindof)?\s*"
    r"(\w+)\s*"  # function name
    r"\(([^)]*)\)"  # parameters
    r"\s*;",
    re.DOTALL,
)

_HEADER_PATTERN_ALT = re.compile(r"(\w[\w\s\*]*)\s+(\w+)\s*\(([^)]*)\)\s*;", re.DOTALL)


def _add_symbol(symbols, m):
    raw_ret = m.group(1).strip()
    fname = m.group(2).strip()
    raw_params = m.group(3).strip()
    if fname in _C_KEYWORDS:
        return

    _, ret_type = _parse_decl(raw_ret)
    if ret_type is None:
        return

    if not raw_params or raw_params.strip() == "void":
        params = []
        vararg = False
    else:
        params = []
        vararg = False
        for part in _split_params(raw_params):
            part = part.strip()
            if part == "...":
                vararg = True
                continue
            pname, ptype = _parse_decl(part)
            # A single unmappable parameter means the whole signature would be
            # wrong; drop the symbol rather than emit a broken extern.
            if ptype is None:
                return
            params.append((pname or f"p{len(params)}", ptype))
    symbols[fname] = (ret_type, params, vararg)


# Pattern for CF_EXPORT const variable declarations (e.g., kCFRunLoopCommonModes)
_CONST_VAR_PATTERN = re.compile(
    r"(?:CF_EXPORT|CG_EXTERN|extern)\s+const\s+(\w+)\s+(\w+)\s*;", re.DOTALL
)


def _hardcoded_symbols(name):
    """Convert C-level C_LIBRARIES entries to cpyte signatures.

    Entries are pushed through the same strict, platform-width-aware mapper
    used for parsed headers, so a curated signature can never silently disagree
    with what libclang would have produced. Returned dict is in the standard
    `{fname: (ret_type, [(pname, ptype), ...], vararg)}` shape, or None when
    the library contributes no fully-representable symbols.
    """
    treated = C_LIBRARIES.get(name)
    if not treated:
        return None
    symbols = {}
    for fname, desc in treated.items():
        ret = _c_type_to_lang(desc[0])
        if ret is None:
            continue
        params = []
        ok = True
        for pname, ptype_c in desc[1]:
            pt = _c_type_to_lang(ptype_c)
            if pt is None:
                ok = False
                break
            params.append((pname, pt))
        if not ok:
            continue
        vararg = len(desc) > 2 and desc[2]
        symbols[fname] = (ret, params, vararg)
    return symbols or None


# Cache for validated hardcoded signatures (name -> symbols dict or None)
_hardcoded_symbols_cache = {}


def resolve_library(name):
    """Resolve a bare import name (e.g. 'stdlib') to (symbols_dict, 'c').

    Tries parsing the actual system header via libclang for full C compatibility.
    Falls back to the hardcoded C_LIBRARIES when libclang is unavailable or the
    header can't be found. All signatures — parsed or hand-written — go through
    the same strict, platform-width-aware C→cpyte conversion.
    """
    if name in _hardcoded_symbols_cache:
        hard = _hardcoded_symbols_cache[name]
    else:
        hard = _hardcoded_symbols(name)
        _hardcoded_symbols_cache[name] = hard

    header_name = _BUILTIN_LIB_HEADERS.get(name)
    if header_name is not None:
        # Try parsing the real system header for complete symbol coverage
        parsed = _parse_system_header(header_name)
        if parsed is not None:
            # Fill any symbols the parser missed (function pointers, macros,
            # static-inline wrappers, ...) from the curated fallback table.
            for fname, desc in (hard or {}).items():
                if fname not in parsed:
                    parsed[fname] = desc
            if parsed:
                return parsed, "c"

    if hard is not None:
        return hard, "c"
    return None


_INCLUDE_PATTERN = re.compile(r'#\s*include\s+[<"](\S+)[>"]')


def _resolve_include(include_path, current_file, search_paths):
    """Resolve a #include to an absolute file path."""
    # Quote includes: search relative to current file first
    if not include_path.startswith("<"):
        dirpath = os.path.dirname(current_file)
        candidate = os.path.normpath(os.path.join(dirpath, include_path))
        if os.path.exists(candidate):
            return candidate

    # Try framework paths (e.g., <CoreGraphics/CGEventTypes.h>). Framework
    # roots are searched generically (System/Library/Frameworks, Frameworks,
    # or the SDK root itself) so any SDK layout works.
    parts = include_path.split("/")
    if len(parts) >= 2:
        framework_name = parts[0]
        header_rel = "/".join(parts[1:])
        for sdk in search_paths:
            for fw_root in ("System/Library/Frameworks", "Frameworks", ""):
                for fw_subdir in (
                    f"{framework_name}.framework/Headers",
                    f"{framework_name}.framework/Versions/Current/Headers",
                    f"{framework_name}.framework/Versions/A/Headers",
                ):
                    candidate = os.path.join(sdk, fw_root, fw_subdir, header_rel)
                    if os.path.exists(candidate):
                        return candidate

    # Search standard SDK include paths
    for sdk in search_paths:
        for subdir in ("usr/include", ""):
            candidate = os.path.join(sdk, subdir, include_path)
            if os.path.exists(candidate):
                return candidate

    # Search framework private headers
    if len(parts) >= 2:
        framework_name = parts[0]
        header_rel = "/".join(parts[1:])
        for sdk in search_paths:
            for fw_root in ("System/Library/Frameworks", "Frameworks", ""):
                candidate = os.path.join(
                    sdk,
                    fw_root,
                    f"{framework_name}.framework/PrivateHeaders",
                    header_rel,
                )
                if os.path.exists(candidate):
                    return candidate

    return None


def _normalize_header_content(content):
    """Strip attributes and flatten multi-line declarations for easier parsing."""
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    # Remove known attribute macros with balanced parens
    attr_prefixes = [
        "API_AVAILABLE",
        "API_UNAVAILABLE",
        "API_DEPRECATED",
        "CF_AVAILABLE",
        "CG_AVAILABLE_STARTING",
        "NS_AVAILABLE",
        "NS_DEPRECATED",
        "__OSX_AVAILABLE_STARTING",
        "__TVOS_AVAILABLE_STARTING",
        "__IOS_AVAILABLE_STARTING",
        "SWIFT_UNAVAILABLE",
        "CF_BRIDGED_TYPE",
    ]
    for prefix in attr_prefixes:
        pattern = re.compile(re.escape(prefix) + r"\s*\(")
        while True:
            m = pattern.search(content)
            if not m:
                break
            start = m.start()
            depth = 1
            i = m.end()
            while i < len(content) and depth > 0:
                if content[i] == "(":
                    depth += 1
                elif content[i] == ")":
                    depth -= 1
                i += 1
            content = content[:start] + content[i:]
    # Remove __attribute__((...))
    content = re.sub(r"__attribute__\s*\(\([^)]*\)\)", "", content)
    # Remove single keywords
    for kw in [
        "nullable",
        "nonnull",
        "__nullable",
        "__nonnull",
        "__null_unspecified",
        "__kindof",
        "CF_RETURNS_RETAINED",
        "CF_RETURNS_NOT_RETAINED",
        "NS_REQUIRES_NIL_TERMINATION",
        "CF_BRIDGED_TRANSFER",
    ]:
        content = re.sub(r"\b" + kw + r"\b", "", content)
    # Remove remaining stray parens (from partially removed attributes): ) followed by )
    content = re.sub(r"\)\s*\)", ")", content)
    # Flatten continuation lines
    lines = content.split("\n")
    result = []
    for line in lines:
        if result and not line.strip():
            result.append(line)
            continue
        if result and (line.startswith("    ") or line.startswith("\t")):
            result[-1] = result[-1] + " " + line.strip()
        else:
            result.append(line)
    return "\n".join(result)


def parse_header_file(filepath, search_paths=None, _processed=None):
    if _processed is None:
        _processed = set()
    if filepath in _processed:
        return {}, "h", {}, set(), set()
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

    frameworks = _framework_names_from_path(filepath)

    # Process includes recursively FIRST, so enum resolution can use their constants
    if search_paths:
        for m in _INCLUDE_PATTERN.finditer(content):
            inc_path = m.group(1)
            inc_file = _resolve_include(inc_path, filepath, search_paths)
            if inc_file:
                sub_sym, _, sub_const, sub_fw, sub_vars = parse_header_file(
                    inc_file, search_paths, _processed
                )
                for k, v in sub_sym.items():
                    symbols.setdefault(k, v)
                var_names.update(sub_vars)
                constants.update(sub_const)
                frameworks.update(sub_fw)

    # Now extract enum constants from current file, with access to all merged constants
    enum_consts = _extract_enum_constants(content, constants)
    constants.update(enum_consts)

    return symbols, "h", constants, frameworks, var_names


def _extract_defines(content):
    """Extract #define integer constants and function-like macros."""
    constants = {}
    macros = {}
    for m in re.finditer(r"#\s*define\s+(\w+)\s+(0[xX][0-9a-fA-F]+|\d+)", content):
        name, val = m.group(1), m.group(2)
        try:
            constants[name] = int(val, 0)
        except ValueError:
            pass
    # Function-like macros: #define NAME(params) body
    for m in re.finditer(r"#\s*define\s+(\w+)\s*\(([^)]*)\)\s*(.*?)(?:\n|$)", content):
        name, params, _body = m.group(1), m.group(2), m.group(3)
        param_list = [p.strip() for p in params.split(",") if p.strip()]
        if param_list:
            macros[name] = ("int", [(p, "int") for p in param_list], False)
    return constants, macros


def _extract_enum_constants(content, known_constants=None):
    """Extract enum constant values, optionally resolving references via known_constants."""
    if known_constants is None:
        known_constants = {}
    constants = {}

    enum_block_re = re.compile(
        r"(?:typedef\s+)?"
        r"(?:CF_ENUM\s*\([^)]+\)|enum\s+(?:\w+\s*)?(?::\s*\w+\s*)?)"
        r"\s*(\{)",
        re.DOTALL,
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
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
            elif content[i] == "/" and i + 1 < len(content):
                if content[i + 1] == "/":
                    nl = content.find("\n", i)
                    i = nl if nl != -1 else len(content)
                    continue
                elif content[i + 1] == "*":
                    end = content.find("*/", i + 2)
                    i = end + 1 if end != -1 else len(content)
                    continue
            i += 1
        if depth == 0:
            body = content[brace_start + 1 : i - 1]
            items = _split_enum_body(body)
            auto_val = 0
            for item in items:
                parts = item.split("=", 1)
                name = parts[0].strip()
                if not name or not name.isidentifier():
                    continue
                if len(parts) > 1:
                    val = parts[1].strip().rstrip(",")
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
        if val.startswith("0x") or val.startswith("0X"):
            return int(val, 16)
        if val.startswith("-") and val[1:].isdigit():
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
        if ch == "(" or ch == "<":
            depth += 1
        elif ch == ")" or ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(body[start:i])
            start = i + 1
        elif ch == "/" and i + 1 < len(body):
            if body[i + 1] == "/":
                nl = body.find("\n", i)
                i = nl if nl != -1 else len(body)
            elif body[i + 1] == "*":
                end = body.find("*/", i + 2)
                i = end + 1 if end != -1 else len(body)
        i += 1
    remaining = body[start:i].strip()
    if remaining:
        items.append(remaining)
    # Strip comments from each item
    result = []
    for item in items:
        item = re.sub(r"/\*.*?\*/", "", item, flags=re.DOTALL)
        item = re.sub(r"//.*", "", item)
        item = item.strip()
        if item:
            result.append(item)
    return result


def _framework_name_from_path(filepath):
    parts = filepath.replace("\\", "/").split("/")
    for i, p in enumerate(parts):
        if p.endswith(".framework"):
            return p[: -len(".framework")]
    return None


def _framework_names_from_path(filepath):
    """All framework names appearing in a header path (root + nested includes).

    A header may live in one framework while transitively including headers
    from others (e.g. ApplicationServices pulls in CoreFoundation/CoreGraphics).
    Every such framework must be linked with `-framework` so the AOT linker can
    resolve the symbols the program actually uses.
    """
    names = set()
    for p in filepath.replace("\\", "/").split("/"):
        if p.endswith(".framework"):
            names.add(p[: -len(".framework")])
    return names


# ── Strict, structured C → cpyte type conversion ────────────────
#
# cpyte has a small fixed type vocabulary: void, bool, char, int, int64,
# uint64, float, double, str (char*), and the pointer forms int*, int64*,
# uint64*, float*, double*, plus generic void*. This mapper converts a C type
# *exactly* into that vocabulary — never silently widening, narrowing, or
# fabricating a type. Anything unrepresentable yields None, and callers skip
# the whole symbol instead of emitting a wrong signature.
#
# Integer widths come from ctypes, so they are correct for whatever host is
# running (LP64: mac/linux, LLP64: windows) rather than guessed.

# qualifiers with no ABI effect
_C_QUALIFIERS = frozenset(
    {
        "const",
        "volatile",
        "restrict",
        "__restrict",
        "__restrict__",
        "_Nonnull",
        "_Nullable",
        "_Null_unspecified",
        "__nonnull",
        "__nullable",
        "inline",
        "__inline",
        "__inline__",
        "static",
        "extern",
        "register",
        "auto",
        "typedef",
        "_Noreturn",
        "_Atomic",
        "_Alignas",
        "_Thread_local",
        "__thread",
        "__signed__",
        "__extension__",
        "__asm__",
        "asm",
    }
)

# base-type keywords that start a C type
_C_BASE_KEYWORDS = frozenset(
    {
        "void",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
        "signed",
        "unsigned",
        "_Bool",
        "bool",
        "struct",
        "union",
        "enum",
    }
)

# Platform integer widths, derived from the running interpreter (ctypes).
_LONG_WIDTH = ctypes.sizeof(ctypes.c_long)  # 8 on mac/linux, 4 on windows (LLP64)
_SSIZE_T_SIZE = ctypes.sizeof(ctypes.c_ssize_t)
_SIZE_T_SIZE = ctypes.sizeof(ctypes.c_size_t)

# Standard typedefs whose width genuinely varies by platform (or that cpyte
# needs to name explicitly). Mapping them through ctypes keeps these correct
# on both LP64 and LLP64 hosts.
_STD_INT_TYPES = {
    "size_t": (_SIZE_T_SIZE, True),
    "ssize_t": (_SSIZE_T_SIZE, False),
    "ptrdiff_t": (_SSIZE_T_SIZE, False),
    "intptr_t": (_SSIZE_T_SIZE, False),
    "uintptr_t": (_SIZE_T_SIZE, True),
    "time_t": (8, False),  # 8 everywhere cpyte targets
    # <stdint.h> fixed-width types
    "int8_t": (1, False),
    "uint8_t": (1, True),
    "int16_t": (2, False),
    "uint16_t": (2, True),
    "int32_t": (4, False),
    "uint32_t": (4, True),
    "int64_t": (8, False),
    "uint64_t": (8, True),
    "intmax_t": (8, False),
    "uintmax_t": (8, True),
    # Apple / POSIX aliases (64-bit dev targets: macOS, Linux)
    "CFIndex": (_LONG_WIDTH, False),
    "NSInteger": (_LONG_WIDTH, False),
    "NSUInteger": (_LONG_WIDTH, True),
    "pid_t": (4, False),  # int on mac/linux
    # Apple _types.h short names
    "int32": (4, False),
    "uint32": (4, True),
    "int64": (8, False),
    "uint64": (8, True),
}

# Opaque pointer typedefs (libc + Apple frameworks). These deliberately
# degrade to cpyte `void*`: pointer-typed, ABI-safe, and the only way to use
# framework APIs from cpyte. Used by the regex fallback path; with libclang the
# canonical spelling already resolves them to plain pointers.
_OPAQUE_POINTER_TYPES = frozenset(
    {
        "FILE",
        "CGColorRef",
        "CGColorSpaceRef",
        "CGEventTapCallBack",
        "CGContextRef",
        "CGDataProviderRef",
        "CGDisplayStreamRef",
        "CGEventRef",
        "CGEventSourceRef",
        "CGImageRef",
        "CGPathRef",
        "CGPatternRef",
        "CGPDFDocumentRef",
        "CGPDFPageRef",
        "CGFontRef",
        "CGLayerRef",
        "CGPSConverterRef",
        "CGWindowRef",
        "CFAllocatorRef",
        "CFArrayRef",
        "CFAttributedStringRef",
        "CFBooleanRef",
        "CFCalendarRef",
        "CFCharacterSetRef",
        "CFDataRef",
        "CFDateRef",
        "CFDictionaryRef",
        "CFErrorRef",
        "CFLocaleRef",
        "CFMachPortRef",
        "CFMutableArrayRef",
        "CFMutableDataRef",
        "CFMutableDictionaryRef",
        "CFMutableSetRef",
        "CFMutableStringRef",
        "CFNotificationCenterRef",
        "CFNullRef",
        "CFNumberRef",
        "CFPropertyListRef",
        "CFReadStreamRef",
        "CFRunLoopRef",
        "CFRunLoopSourceRef",
        "CFRunLoopTimerRef",
        "CFRunLoopObserverRef",
        "CFSetRef",
        "CFStringRef",
        "CFTimeZoneRef",
        "CFTypeRef",
        "CFURLRef",
        "CFUUIDRef",
        "CFWriteStreamRef",
        "SecIdentityRef",
        "SecCertificateRef",
        "SecKeyRef",
        "SecTrustRef",
        "SecPolicyRef",
        "SecAccessRef",
        "SecKeychainRef",
        "SecKeychainItemRef",
        "SecTrustedApplicationRef",
        "SecAccessControlRef",
        "SecItemRef",
        "IOSurfaceRef",
        "CVPixelBufferRef",
        "CVBufferRef",
        "CVImageBufferRef",
        "CVOpenGLBufferRef",
        "CVOpenGLTextureRef",
        "CVDisplayLinkRef",
        "MIDIEndpointRef",
        "MIDIClientRef",
        "MIDIPortRef",
        "AudioQueueRef",
        "AudioUnit",
        "AudioComponentInstance",
    }
)

# 8-bit scalars (macOS), ABI-safe as cpyte `char` (i8). Not pointers.
_8BIT_SCALAR_TYPES = frozenset(
    {
        "BOOL",
        "Boolean",
        "DarwinBoolean",
        "SInt8",
        "UInt8",
    }
)

_C_TOKEN_RE = re.compile(r"\w+|\*+|\[+|\]+|\(|\)|\.\.\.|,")


def _tokenize_type(raw):
    return [t for t in _C_TOKEN_RE.findall(raw) if t]


def _consume_int_spec(words, i):
    """Consume a C integer spec from words[i:] tolerating the free ordering
    clang emits in canonical spellings (e.g. 'long unsigned int').

    Returns ((kind, unsigned_flag), consumed) where kind is one of
    'char'/'short'/'int'/'long'/'longlong', or (None, i) when words[i:] does
    not begin with an integer-type word.
    """
    if i >= len(words) or words[i] not in (
        "signed",
        "unsigned",
        "char",
        "short",
        "int",
        "long",
    ):
        return None, i
    unsigned = False
    has_long = 0
    kind = None
    j = i
    while j < len(words) and words[j] in (
        "signed",
        "unsigned",
        "char",
        "short",
        "int",
        "long",
    ):
        w = words[j]
        if w == "unsigned":
            unsigned = True
        elif w == "long":
            has_long += 1
        elif w == "char":
            kind = "char"
        elif w == "short":
            kind = "short"
        elif w == "int":
            if kind is None:
                kind = "int"
        j += 1
    if kind is None:
        kind = "int"
    if has_long:
        kind = "longlong" if has_long >= 2 else "long"
    else:
        # 'char *' -> the char op on 'const char *' is handled by caller
        pass
    return (kind, unsigned), j


def _int_width_lang(width, unsigned):
    """Exact-width integer → cpyte scalar type, or None when cpyte has no type
    of that width (16-bit)."""
    if width == 1:
        return "char"
    if width == 2:
        return None
    if width == 4:
        return "int"
    if width == 8:
        return "uint64" if unsigned else "int64"
    return None


def _int_width_ptr(width, unsigned):
    """Exact-width integer → cpyte pointer type, or None."""
    if width == 1:
        # char* is the cpyte string contract; byte-happy `unsigned char*` stays
        # generic so it is not mistaken for a NUL-terminated string.
        return "str" if not unsigned else None
    if width == 2:
        return None
    if width == 4:
        return "int*"
    if width == 8:
        return "uint64*" if unsigned else "int64*"
    return None


def _int_spec_lang(kind, unsigned):
    widths = {"char": 1, "short": 2, "int": 4, "long": _LONG_WIDTH, "longlong": 8}
    return _int_width_lang(widths[kind], unsigned)


def _int_spec_ptr(kind, unsigned):
    widths = {"char": 1, "short": 2, "int": 4, "long": _LONG_WIDTH, "longlong": 8}
    return _int_width_ptr(widths[kind], unsigned)


def _canonical_spelling(ctype):
    """Best-effort canonical spelling from a libclang Type object."""
    try:
        return ctype.get_canonical().spelling
    except Exception:
        try:
            return ctype.spelling
        except Exception:
            return ""


def _parse_decl(raw):
    """Parse a C type or `type name` declarator into (name, cpy_type).

    `raw` accepts both pure type spellings (from libclang) and full declarators
    such as `const char *name` or `unsigned char buf[16]` (from the regex
    fallback). Returns (None, None) when the type cannot be represented
    exactly in cpyte, so callers skip the symbol entirely.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None

    # Function pointers have no cpyte type, but as a parameter or return value
    # they are plain pointers under every relevant calling convention, so they
    # degrade ABI-safely to void*.
    if "(" in raw:
        m = re.search(r"\(\s*\*\s*(\w+)\s*\)", raw)
        name = m.group(1) if m else ""
        return name, "void*"

    words = []
    stars = 0
    array = False
    for t in _tokenize_type(raw):
        if t in _C_QUALIFIERS:
            continue
        if t.startswith("*"):
            stars += t.count("*")
        elif t in ("[", "]"):
            array = True
        elif t.isdigit():
            continue
        else:
            words.append(t)

    if not words:
        return None, None

    first = words[0]
    base_lang = None  # scalar cpyte type
    base_ptr_lang = None  # single-pointer cpyte type
    consumed = 1

    if first in ("void", "float", "double", "_Bool", "bool", "struct", "union", "enum"):
        if first in ("_Bool", "bool"):
            base_lang, base_ptr_lang = "bool", None
        elif first == "void":
            base_lang, base_ptr_lang = "void", "void*"
        elif first == "float":
            base_lang, base_ptr_lang = "float", "float*"
        elif first == "double":
            base_lang, base_ptr_lang = "double", "double*"
        else:  # struct/union/enum: representable only by pointer
            base_lang, base_ptr_lang = None, "void*"
            if len(words) > 1 and words[1] not in _C_BASE_KEYWORDS:
                consumed = 2
    else:
        spec, c = _consume_int_spec(words, 0)
        if spec is not None:
            kind, unsigned = spec
            consumed = c
            base_lang = _int_spec_lang(kind, unsigned)
            base_ptr_lang = _int_spec_ptr(kind, unsigned)
        elif first in _OPAQUE_POINTER_TYPES:
            base_lang, base_ptr_lang = "void*", "void*"
        elif first in _STD_INT_TYPES:
            width, unsigned = _STD_INT_TYPES[first]
            base_lang = _int_width_lang(width, unsigned)
            base_ptr_lang = _int_width_ptr(width, unsigned)
            if base_lang is None and base_ptr_lang is None:
                return None, None
        elif first in _8BIT_SCALAR_TYPES:
            base_lang = "char"
        else:
            # Unknown typedef: cannot know width or pointerness. A bare
            # unrepresentable scalar must be skipped, not guessed.
            return None, None

    rest = words[consumed:]
    for w in rest:
        if w in _C_BASE_KEYWORDS:
            # A trailing keyword means the base spec was under-consumed
            # (e.g. 'long double'); treat as unrepresentable.
            return None, None
    if len(rest) > 1:
        return None, None
    name = rest[0] if rest else ""

    if stars or array:
        if base_ptr_lang is None:
            return name, "void*"
        if stars + (1 if array else 0) == 1:
            return name, base_ptr_lang
        return name, "void*"
    if base_lang is None:
        return None, None
    return name, base_lang


def _c_type_to_lang(raw):
    """Strict C type string → cpyte type string, or None if unrepresentable."""
    _, lang = _parse_decl(raw)
    return lang


def _split_params(s):
    depth = 0
    parts = []
    cur = []
    for ch in s:
        if ch in "({[":
            depth += 1
            cur.append(ch)
        elif ch in ")}]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def parse_c_source(filepath):
    if not _init_libclang():
        return _parse_c_source_regex(filepath)

    import clang.cindex as ci  # type: ignore[reportMissingImports]

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
        if c.spelling == "main":
            continue
        if c.spelling in _C_KEYWORDS:
            continue

        ret_type = _c_type_to_lang(_canonical_spelling(c.result_type))
        if ret_type is None:
            continue

        vararg = False
        try:
            vararg = c.type.is_function_variadic()
        except AssertionError:
            vararg = False

        params = []
        for p in c.get_arguments():
            ptype = _c_type_to_lang(_canonical_spelling(p.type))
            # A single unmappable parameter means the whole signature would be
            # wrong; drop the symbol rather than emit a broken extern.
            if ptype is None:
                params = None
                break
            params.append((p.spelling or f"p{len(params)}", ptype))
        if params is None:
            continue

        symbols[c.spelling] = (ret_type, params, vararg)

    return symbols, "c"


def _parse_c_source_regex(filepath):
    with open(filepath) as f:
        content = f.read()
    symbols = {}
    for m in _C_SRC_RE.finditer(content):
        raw_ret = m.group(1).strip()
        fname = m.group(2).strip()
        raw_params = m.group(3).strip()

        if fname in _C_KEYWORDS or fname == "main":
            continue

        _, ret_type = _parse_decl(raw_ret)
        if ret_type is None:
            continue

        if not raw_params or raw_params == "void":
            params = []
            vararg = False
        else:
            parts = _split_params(raw_params)
            params = []
            vararg = False
            for p in parts:
                p = p.strip()
                if p == "...":
                    vararg = True
                    continue
                pname, ptype = _parse_decl(p)
                # Never emit a signature with a hole in it.
                if ptype is None:
                    params = None
                    break
                params.append((pname or f"p{len(params)}", ptype))
            if params is None:
                continue
        symbols[fname] = (ret_type, params, vararg)
    return symbols, "c"


_C_KEYWORDS = {
    "if",
    "while",
    "for",
    "switch",
    "return",
    "sizeof",
    "typedef",
    "struct",
    "union",
    "enum",
    "case",
    "default",
    "break",
    "continue",
    "goto",
    "do",
    "else",
}

_C_SRC_RE = re.compile(
    r"(?:(?:static|inline|extern)\s+)*"
    r"([\w\s\*]+?)\s+"
    r"(\w+)\s*\(([^)]*)\)\s*(?:\[[^\]]*\])?\s*\{"
)

_LLVM_DEF_RE = re.compile(r"define\s+(.*?)\s@(\w+)\s*\(([^)]*)\)")

_LLVM_RET_PREFIX_KEYWORDS = frozenset(
    {
        "dso_local",
        "dso_preemptable",
        "external",
        "private",
        "internal",
        "available_externally",
        "linkonce",
        "linkonce_odr",
        "weak",
        "weak_odr",
        "common",
        "appending",
        "extern_weak",
        "global",
        "hidden",
        "protected",
        "default",
        "dllimport",
        "dllexport",
        "thread_local",
        "local_unnamed_addr",
        "unnamed_addr",
        "nocomdat",
        "preemptable",
    }
)


def parse_llvm_ir_text(text):
    """Extract function signatures from raw LLVM IR text.

    Mirrors parse_c_source: returns (symbols, 'llvm') where symbols maps each
    `define`d function name to (ret_type, [(pname, ptype), ...], vararg). Only
    defined (not merely declared) functions are registered, and signatures
    whose types have no cpyte equivalent are skipped.
    """
    symbols = {}
    for m in _LLVM_DEF_RE.finditer(text):
        ret_tokens = m.group(1).strip().split()
        while ret_tokens and ret_tokens[0] in _LLVM_RET_PREFIX_KEYWORDS:
            ret_tokens.pop(0)
        raw_ret = " ".join(ret_tokens)
        fname = m.group(2).strip()
        raw_params = m.group(3).strip()

        ret_type = _ir_type_to_lang(raw_ret)
        if ret_type is None:
            continue

        params = []
        vararg = False
        ok = True
        if raw_params and raw_params != "...":
            for p in _split_ir_params(raw_params):
                p = p.strip()
                if p == "...":
                    vararg = True
                    continue
                ptype = _ir_param_type_to_lang(p)
                if ptype is None:
                    ok = False
                    break
                params.append((f"p{len(params)}", ptype))
        if not ok:
            continue
        symbols[fname] = (ret_type, params, vararg)
    return symbols, "llvm"


def _split_ir_params(raw):
    parts = []
    depth = 0
    cur = []
    for ch in raw:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur or raw:
        parts.append("".join(cur))
    return parts


def _ir_param_type_to_lang(param):
    parts = param.split()
    idx = None
    for i, t in enumerate(parts):
        if t.startswith("%"):
            idx = i
            break
    if idx is None:
        raw = param
    else:
        raw = " ".join(parts[:idx])
    return _ir_type_to_lang(raw)


def _ir_type_to_lang(t):
    """Exact LLVM IR type → cpyte type, or None when unrepresentable.

    `double` and `float` are distinct 64/32-bit cpyte types, so IR `double`
    maps to `double` and IR `float` maps to `float`; anything else that is not
    in the cpyte vocabulary (i16, i128, aggregates, vectors, multi-indirection)
    yields None and the enclosing function is skipped.
    """
    t = t.strip()
    if t == "void":
        return "void"
    if t == "i1":
        return "bool"
    if t == "i8":
        return "char"
    if t == "i32":
        return "int"
    if t == "i64":
        return "int64"
    if t == "float":
        return "float"
    if t == "double":
        return "double"
    if t == "i8*":
        return "str"
    if t == "i32*":
        return "int*"
    if t == "i64*":
        return "int64*"
    if t == "float*":
        return "float*"
    if t == "double*":
        return "double*"
    if t == "ptr":
        return "void*"
    if t == "i1*":
        return "void*"
    if t.endswith("*"):
        return "void*"
    return None
