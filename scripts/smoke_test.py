from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    payload: dict[str, object] = {
        "python": sys.version.split()[0],
        "repo_root": str(repo_root),
        "plaxis_host": os.getenv("PLAXIS_HOST", "127.0.0.1"),
        "plaxis_port": os.getenv("PLAXIS_PORT", "10000"),
        "plaxis_scripting_path": os.getenv("PLAXIS_SCRIPTING_PATH"),
    }

    try:
        from plaxis_mcp.core import PlaxisSession, load_new_server
    except Exception as exc:
        payload["smoke_test_ok"] = False
        payload["error"] = str(exc)
        print(json.dumps(payload, indent=2))
        return 1

    try:
        load_new_server()
    except Exception as exc:
        payload["plxscripting_importable"] = False
        payload["plxscripting_error"] = str(exc)
    else:
        payload["plxscripting_importable"] = True

    payload["mcp_sdk_importable"] = True
    try:
        import mcp  # noqa: F401
    except Exception as exc:
        payload["mcp_sdk_importable"] = False
        payload["mcp_sdk_error"] = str(exc)

    payload["session_status"] = PlaxisSession().status()
    payload["smoke_test_ok"] = True
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
