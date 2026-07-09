# -*- mode: python ; coding: utf-8 -*-
import os
_runtime_c = os.path.join(os.path.dirname(SPEC), 'runtime.c')
_datas = []
if os.path.exists(_runtime_c):
    _datas.append((_runtime_c, '.'))

a = Analysis(
    ['mainpie.py'],
    pathex=[os.path.dirname(SPEC)],
    binaries=[],
    datas=_datas,
    hiddenimports=[
        'llvmlite',
        'llvmlite.binding',
        'llvmlite.binding.analysis',
        'llvmlite.binding.common',
        'llvmlite.binding.config',
        'llvmlite.binding.context',
        'llvmlite.binding.dylib',
        'llvmlite.binding.executionengine',
        'llvmlite.binding.ffi',
        'llvmlite.binding.initfini',
        'llvmlite.binding.linker',
        'llvmlite.binding.module',
        'llvmlite.binding.newpassmanagers',
        'llvmlite.binding.object_file',
        'llvmlite.binding.options',
        'llvmlite.binding.orcjit',
        'llvmlite.binding.targets',
        'llvmlite.binding.typeref',
        'llvmlite.binding.value',
        'llvmlite.ir',
        'ctypes',
        'ctypes.util',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'unittest',
        'email',
        'http',
        'xml',
        'pydoc',
        'distutils',
        'asyncio',
        'concurrent',
        'pygame',
        'matplotlib',
        'numpy',
        'Cython',
        'setuptools',
        'pip',
        'wheel',
        'curses',
        'venv',
        'PIL',
        'pandas',
        'scipy',
        'IPython',
    ],
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='cpy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='cpy',
)

# -- onefile variant for distribution: produces a 47 MB standalone binary
#    that self-extracts on each invocation (slower startup, but relocatable).
#    Build with: pyinstaller source/cpy_compiler.spec --onefile
#
# exe_onefile = EXE(
#     pyz,
#     a.scripts,
#     a.binaries,
#     a.datas,
#     [],
#     name='cpy-onefile',
#     debug=False,
#     bootloader_ignore_signals=False,
#     strip=False,
#     upx=True,
#     upx_exclude=[],
#     console=True,
#     disable_windowed_traceback=False,
# )
