"""Scorpion ABI, built into the language behind the `#scorpion` directive.

A source file whose first non-blank line is `#scorpion` gets the whole Scorpion
ABI in scope: the `SYS_*` syscall numbers, the `SCORPION_*` loader codes and
flags, the `ScorpionLibInfo` struct, and every `scorpion_*` syscall wrapper.

Tables are plain Python data mirroring the Scorpion repository's
`abi/syscall.h` and `abi/scorpion.h` exactly; keep them in step with those
files. This module is deliberately dependency-free so `mainpie`,
`semantic_analasis`, `bytecoding` and `lsp_server` can all import it.

The userland wrappers in `abi/scorpion.h` are Xtensa-only inline asm, but the
`a0`..`a7` register names they bind are also the RISC-V argument registers, so
the same `ecall` encoding works on the RV32 target cpyte compiles to
(`runtime_scorpion.c` ships a non-static copy of every wrapper).
"""

# Directives may be mixed; `#scorpion` is what turns this ABI on.
SCORPION_DIRECTIVE = "#scorpion"

# --- abi/syscall.h: the numbering is the single source of truth --------------
SYS_YIELD = 0
SYS_EXIT = 1
SYS_BLOCK = 2
SYS_WAKE = 3
SYS_SLEEP = 4
SYS_SEND = 5
SYS_RECV = 6
SYS_OPEN = 7
SYS_READ = 8
SYS_WRITE = 9
SYS_CLOSE = 10
SYS_PUTC = 11
SYS_SPAWN = 12
SYS_TERMINATE = 13
SYS_LOADLIB = 14
SYS_UNLOADLIB = 15
SYS_DLSYM = 16
SYS_LIBINFO = 17
SYS_MAX = 17

# --- abi/scorpion.h: loader return codes ------------------------------------
SCORPION_OK = 0
SCORPION_EPERM = -1
SCORPION_EINVAL = -2
SCORPION_ENOENT = -3
SCORPION_ENOSPC = -4
SCORPION_ENOMEM = -5
SCORPION_EBUSY = -6
SCORPION_ENOEXEC = -7

# Flags for scorpion_loadlib() / scorpion_unloadlib().
SCORPION_LOAD_GLOBAL = 0x1
SCORPION_LOAD_LOCAL = 0x2
SCORPION_LOAD_NOW = 0x4
SCORPION_UNLOAD_GLOBAL = 0x1
SCORPION_VER_ANY = 0

#: Every integer constant the `#scorpion` ABI publishes to cpyte source.
CONSTANTS: dict[str, int] = {
    "SYS_YIELD": SYS_YIELD,
    "SYS_EXIT": SYS_EXIT,
    "SYS_BLOCK": SYS_BLOCK,
    "SYS_WAKE": SYS_WAKE,
    "SYS_SLEEP": SYS_SLEEP,
    "SYS_SEND": SYS_SEND,
    "SYS_RECV": SYS_RECV,
    "SYS_OPEN": SYS_OPEN,
    "SYS_READ": SYS_READ,
    "SYS_WRITE": SYS_WRITE,
    "SYS_CLOSE": SYS_CLOSE,
    "SYS_PUTC": SYS_PUTC,
    "SYS_SPAWN": SYS_SPAWN,
    "SYS_TERMINATE": SYS_TERMINATE,
    "SYS_LOADLIB": SYS_LOADLIB,
    "SYS_UNLOADLIB": SYS_UNLOADLIB,
    "SYS_DLSYM": SYS_DLSYM,
    "SYS_LIBINFO": SYS_LIBINFO,
    "SYS_MAX": SYS_MAX,
    "SCORPION_OK": SCORPION_OK,
    "SCORPION_EPERM": SCORPION_EPERM,
    "SCORPION_EINVAL": SCORPION_EINVAL,
    "SCORPION_ENOENT": SCORPION_ENOENT,
    "SCORPION_ENOSPC": SCORPION_ENOSPC,
    "SCORPION_ENOMEM": SCORPION_ENOMEM,
    "SCORPION_EBUSY": SCORPION_EBUSY,
    "SCORPION_ENOEXEC": SCORPION_ENOEXEC,
    "SCORPION_LOAD_GLOBAL": SCORPION_LOAD_GLOBAL,
    "SCORPION_LOAD_LOCAL": SCORPION_LOAD_LOCAL,
    "SCORPION_LOAD_NOW": SCORPION_LOAD_NOW,
    "SCORPION_UNLOAD_GLOBAL": SCORPION_UNLOAD_GLOBAL,
    "SCORPION_VER_ANY": SCORPION_VER_ANY,
}

#: `ScorpionLibInfo` from abi/scorpion.h. The field order (and the uint16 tail
#: after seven uint32s) is load-bearing: the kernel writes this exact 32-byte
#: layout through scorpion_lib_info(), so `uint16` must lower to i16.
LIB_INFO_NAME = "ScorpionLibInfo"
LIB_INFO_FIELDS: list[tuple[str, str]] = [
    ("base", "uint32"),
    ("plt_base", "uint32"),
    ("plt_size", "uint32"),
    ("export_count", "uint32"),
    ("import_count", "uint32"),
    ("ver_count", "uint32"),
    ("bound_count", "uint32"),
    ("refcount", "uint16"),
    ("flags", "uint16"),
]

#: name -> (return type, [(param name, param type)], vararg)
#:
#: Types are cpyte type names so both the semantic analyzer (which reports
#: arity and stamps `Call.param_types`) and the bytecoder (which declares the
#: externs) can consume the same table. Sizes match the C ABI on the RV32
#: target: every scalar is a 32-bit word, pointers are words.
FUNCTIONS: dict[str, tuple[str, list[tuple[str, str]], bool]] = {
    "scorpion_yield": ("void", [], False),
    "scorpion_exit": ("void", [], False),
    "scorpion_block": ("void", [], False),
    "scorpion_sleep": ("void", [("ticks", "int")], False),
    "scorpion_wake": ("void", [("pid", "int")], False),
    "scorpion_send": (
        "int",
        [("pid", "int"), ("type", "int"), ("data", "void*"), ("len", "int")],
        False,
    ),
    "scorpion_recv": (
        "int",
        [("type", "int*"), ("buf", "void*"), ("len", "int"), ("sender_pid", "int*")],
        False,
    ),
    "scorpion_putc": ("void", [("s", "str"), ("len", "int")], False),
    "scorpion_open": ("int", [("name", "str"), ("mode", "int")], False),
    "scorpion_read": (
        "int",
        [("fd", "int"), ("buf", "void*"), ("size", "int")],
        False,
    ),
    "scorpion_write": (
        "int",
        [("fd", "int"), ("buf", "void*"), ("size", "int")],
        False,
    ),
    "scorpion_close": ("int", [("fd", "int")], False),
    "scorpion_spawn": (
        "int",
        [("sef_data", "void*"), ("size", "int"), ("priority", "int")],
        False,
    ),
    "scorpion_terminate": ("int", [("pid", "int")], False),
    "scorpion_loadlib": (
        "int",
        [("sef_data", "void*"), ("size", "int"), ("flags", "int")],
        False,
    ),
    "scorpion_loadlib_global": (
        "int",
        [("sef_data", "void*"), ("size", "int")],
        False,
    ),
    "scorpion_loadlib_local": (
        "int",
        [("sef_data", "void*"), ("size", "int")],
        False,
    ),
    "scorpion_unloadlib": ("int", [("handle", "int"), ("flags", "int")], False),
    "scorpion_sym": ("void*", [("name", "str"), ("name_len", "int")], False),
    "scorpion_ver_hash": ("int", [("v", "str"), ("len", "int")], False),
    "scorpion_sym_ver": (
        "void*",
        [
            ("name", "str"),
            ("name_len", "int"),
            ("version", "str"),
            ("version_len", "int"),
        ],
        False,
    ),
    "scorpion_lib_info": (
        "int",
        [("handle", "int"), ("global", "int"), ("out", f"{LIB_INFO_NAME}*")],
        False,
    ),
}

#: name -> (return type, [param type], vararg) — the shape the import/ABI
#: registration paths consume (see SemanticAnalyzer._register_import_symbols).
SYMBOLS: dict[str, tuple[str, list[tuple[str, str]], bool]] = dict(FUNCTIONS)


def detect(source: str) -> tuple[str, bool]:
    """Strip the `#scorpion` directive and report whether it was present.

    Like `#nogc`, the directive must be the first non-blank line (blank lines
    and nothing else may precede it). It is replaced with an empty line rather
    than deleted so reported line numbers stay put.
    """
    lines = source.split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and lines[idx].strip() == SCORPION_DIRECTIVE:
        lines[idx] = ""
        return "\n".join(lines), True
    return source, False


def lib_info_fields() -> list:
    """`ScorpionLibInfo` fields as astparse `Field` nodes."""
    from .astparse import Field

    return [Field(name, type_expr) for name, type_expr in LIB_INFO_FIELDS]


def lib_info_struct(token=None):
    """A synthetic `StructDef` for `ScorpionLibInfo`."""
    from .astparse import StructDef

    return StructDef(LIB_INFO_NAME, lib_info_fields(), token=token)
