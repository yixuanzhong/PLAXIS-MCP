# -*- mode: python ; coding: utf-8 -*-
"""One-directory Windows build for the host and its Python-3.7-safe worker."""

from pathlib import Path


# SPECPATH is the directory holding this spec (packaging/), so the repository
# root is exactly one level up. Going two levels up escapes the repository
# entirely, which is easy to miss because the checkout directory repeats the
# project name (PLAXIS-MCP/PLAXIS-MCP).
ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"

a = Analysis(
    [str(ROOT / "packaging" / "entrypoint.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        (str(SRC / "plaxis_mcp" / "worker.py"), "worker"),
        (str(SRC / "plaxis_mcp" / "worker_runtime.py"), "worker"),
        (str(ROOT / "profiles"), "profiles"),
        (str(ROOT / "LICENSE"), "."),
    ],
    hiddenimports=["mcp.server.fastmcp", "plaxis_mcp.bridge", "plaxis_mcp.deployment"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["plxscripting", "plxscripting.easy", "plxscripting.server"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="plaxis-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, name="plaxis-mcp")
