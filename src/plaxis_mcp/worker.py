"""Entrypoint for the Python interpreter bundled with PLAXIS.

This module must remain importable by Python 3.7.  ``bridge.py`` starts it
with ``-I -u`` and sets its current directory to this file's directory so the
sibling ``worker_runtime.py`` can be imported without altering ``sys.path``.
"""

from __future__ import print_function

import importlib.util
import os
import sys


def _load_runtime():
    """Load the sibling module without relying on ``sys.path`` under ``-I``."""
    runtime_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker_runtime.py")
    spec = importlib.util.spec_from_file_location("plaxis_mcp_worker_runtime", runtime_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load the PLAXIS worker runtime.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.WorkerRuntime


def main():
    WorkerRuntime = _load_runtime()
    runtime = WorkerRuntime(role=os.environ.get("PLAXIS_ROLE", "unknown"))
    runtime.serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
