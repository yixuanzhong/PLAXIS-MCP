"""Verify the direct build lock and pip consistency without touching PLAXIS."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import re
import subprocess
import sys


_PIN = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s;#]+)")


def read_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match:
            pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    if not pins:
        raise ValueError("Requirement lock contains no exact pins.")
    return pins


def verify_pins(pins: dict[str, str]) -> list[str]:
    failures: list[str] = []
    for name, expected in sorted(pins.items()):
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            failures.append("%s is not installed (expected %s)" % (name, expected))
        else:
            if actual != expected:
                failures.append("%s is %s (expected %s)" % (name, actual, expected))
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, default=Path("requirements-build.lock"))
    parser.add_argument("--skip-pip-check", action="store_true")
    args = parser.parse_args(argv)
    failures = verify_pins(read_pins(args.requirements))
    if not args.skip_pip_check:
        result = subprocess.run([sys.executable, "-m", "pip", "check"], check=False, text=True)
        if result.returncode:
            failures.append("pip check failed")
    if failures:
        for failure in failures:
            print("ERROR: " + failure, file=sys.stderr)
        return 1
    print("Dependency lock and pip consistency verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
