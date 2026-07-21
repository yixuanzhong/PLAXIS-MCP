"""Create a SHA-256 manifest and prove no vendor files were packaged."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


_FORBIDDEN_VENDOR_PATH_TOKENS = ("bentley", "seequent", "plxscripting")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def collect_files(artifact_root: Path, manifest_name: str) -> tuple[list[dict[str, object]], list[str]]:
    files: list[dict[str, object]] = []
    forbidden: list[str] = []
    candidates = sorted((item for item in artifact_root.rglob("*") if item.is_file()), key=lambda item: str(item).lower())
    for path in candidates:
        relative = path.relative_to(artifact_root).as_posix()
        if relative == manifest_name:
            continue
        if any(token in part.lower() for part in path.relative_to(artifact_root).parts for token in _FORBIDDEN_VENDOR_PATH_TOKENS):
            forbidden.append(relative)
        files.append({"path": relative, "size": path.stat().st_size, "sha256": sha256(path)})
    return files, forbidden


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    root = args.artifact_root.resolve()
    output = (args.output or root / "artifact-manifest.json").resolve()
    if output.parent != root:
        parser.error("--output must be directly inside --artifact-root.")
    files, forbidden = collect_files(root, output.name)
    if forbidden:
        print("ERROR: artifact contains vendor-owned path(s): " + ", ".join(forbidden))
        return 1
    payload = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifact_root": root.name,
        "files": files,
        "vendor_redistribution_check": {"passed": True, "forbidden_paths": []},
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Wrote %s (%d files; no Bentley/Seequent/plxscripting paths)." % (output, len(files)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
