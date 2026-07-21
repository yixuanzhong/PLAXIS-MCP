"""Write a machine-readable license inventory from installed Python metadata."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
from pathlib import Path
import sys


def _metadata_value(distribution: metadata.Distribution, name: str) -> str | None:
    value = distribution.metadata.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def inventory() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for distribution in metadata.distributions():
        name = _metadata_value(distribution, "Name")
        if not name:
            continue
        classifiers = distribution.metadata.get_all("Classifier") or []
        records.append(
            {
                "name": name,
                "version": distribution.version,
                "license": _metadata_value(distribution, "License"),
                "license_classifiers": [item for item in classifiers if item.startswith("License ::")],
                "home_page": _metadata_value(distribution, "Home-page"),
            }
        )
    return sorted(records, key=lambda item: str(item["name"]).lower())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    payload = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "distributions": inventory(),
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Wrote license inventory for %d distributions." % len(payload["distributions"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
