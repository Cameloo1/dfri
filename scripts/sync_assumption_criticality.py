"""Synchronize computed criticality metadata into a methodology registry."""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

from dfri.attribution.criticality import compute_assumption_criticality
from dfri.attribution.registry import load_attribution_bundle

DEFAULT_REGISTRY = Path("src/dfri/attribution/assumption_registry_v1_2_1.json")


def synchronize(registry_path: Path, *, check: bool) -> bool:
    payload = json.loads(registry_path.read_text("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError("Assumption registry payload is malformed")
    methodology_version = payload.get("methodology_version")
    if not isinstance(methodology_version, str) or not methodology_version:
        raise RuntimeError("Assumption registry methodology version is missing")
    rows = {
        row.assumption_id: row
        for row in compute_assumption_criticality(load_attribution_bundle(methodology_version))
    }
    changed = False
    for item in payload["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("assumption_id"), str):
            raise RuntimeError("Assumption registry item is malformed")
        assumption_id = item["assumption_id"]
        row = rows.pop(assumption_id, None)
        if row is None:
            raise RuntimeError(f"Uncomputed assumption: {assumption_id}")
        expected = {
            "criticality_rating": row.rating,
            "criticality_dependency_share": row.dependency_share,
        }
        for key, value in expected.items():
            if item.get(key) != value:
                item[key] = value
                changed = True
    if rows:
        raise RuntimeError(f"Criticality rows absent from registry: {sorted(rows)}")
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if check:
        if changed or registry_path.read_text("utf-8") != rendered:
            raise RuntimeError("Assumption criticality metadata is stale")
        return False
    if changed or registry_path.read_text("utf-8") != rendered:
        temporary = registry_path.with_name(f".{registry_path.name}.tmp-{uuid.uuid4().hex}")
        temporary.write_text(rendered, encoding="utf-8", newline="\n")
        temporary.replace(registry_path)
        return True
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    print("UPDATED" if synchronize(args.registry, check=args.check) else "CURRENT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
