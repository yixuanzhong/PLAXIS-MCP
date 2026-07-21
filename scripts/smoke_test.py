"""Diagnostic for the client-neutral MCP host configuration.

This script deliberately does not start PLAXIS or import the vendor SDK.  The
MCP host must remain independent of the Python runtime bundled with PLAXIS.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _role_endpoint(role: str) -> tuple[str, str]:
    prefix = f"PLAXIS_{role.upper()}"
    host = os.getenv(f"{prefix}_HOST") or os.getenv("PLAXIS_HOST") or "127.0.0.1"
    default_port = "10000" if role == "input" else "10001"
    port = os.getenv(f"{prefix}_PORT") or os.getenv("PLAXIS_PORT") or default_port
    return host, port


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    role = os.getenv("PLAXIS_ROLE", "input").strip().lower()
    if role not in {"input", "output"}:
        role = "invalid"

    host, port = _role_endpoint(role if role in {"input", "output"} else "input")
    bundle_python = os.getenv("PLAXIS_BUNDLE_PYTHON")
    payload: dict[str, object] = {
        "host_python": sys.version.split()[0],
        "host_python_supported": sys.version_info[:2] == (3, 13),
        "repo_root": str(repo_root),
        "role": role,
        "endpoint": {"host": host, "port": port},
        "bundle_python_configured": bool(bundle_python),
        "bundle_python_exists": bool(bundle_python and Path(bundle_python).is_file()),
        "vendor_sdk_imported_in_host": "plxscripting" in sys.modules,
    }

    try:
        import mcp  # noqa: F401
        from plaxis_mcp.bridge import BridgeSessionConfig  # noqa: F401
    except Exception as exc:
        payload["host_imports_ok"] = False
        payload["error"] = str(exc)
        print(json.dumps(payload, indent=2))
        return 1

    payload["host_imports_ok"] = True
    payload["smoke_test_ok"] = bool(payload["host_python_supported"]) and role != "invalid"
    print(json.dumps(payload, indent=2))
    return 0 if payload["smoke_test_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
