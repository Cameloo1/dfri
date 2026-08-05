"""Deterministic M0 replay over a frozen, real Board source snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import date
from importlib import resources
from pathlib import Path
from typing import cast

from dfri.ingest.calendar import release_calendar_evidence, serializable_calendar_rows


class ReplayError(RuntimeError):
    """Replay inputs or an existing publication violate deterministic contracts."""


def replay(*, as_of: date, output: Path) -> str:
    snapshot = _load_snapshot()
    observations_raw = snapshot.get("observations")
    series_raw = snapshot.get("series")
    if not isinstance(observations_raw, list) or not isinstance(series_raw, dict):
        raise ReplayError("Seed snapshot is missing series or observations")
    observations: list[dict[str, str]] = []
    for raw in observations_raw:
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise ReplayError("Seed observations must contain only string fields")
        period_raw = raw.get("period")
        if period_raw is None:
            raise ReplayError("Seed observation is missing period")
        try:
            period = date.fromisoformat(cast(str, period_raw))
        except ValueError as exc:
            raise ReplayError(f"Invalid seed period: {period_raw!r}") from exc
        if period <= as_of:
            observations.append(cast(dict[str, str], raw))
    observations.sort(key=lambda item: (item["period"], item["value"], item["status"]))

    manifest = {
        "as_of": as_of.isoformat(),
        "methodology_version": "m0-seed-v1",
        "snapshot_id": snapshot.get("snapshot_id"),
        "source_checksum": snapshot.get("source_checksum"),
        "status": "SEED_REPLAY",
    }
    publication = {
        "calendar.json": _json_bytes(serializable_calendar_rows()),
        "calendar_evidence.json": _json_bytes(release_calendar_evidence()),
        "manifest.json": _json_bytes(manifest),
        "observations.json": _json_bytes(observations),
        "series.json": _json_bytes(series_raw),
    }
    return _publish_tree(output, publication)


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _load_snapshot() -> dict[str, object]:
    text = resources.files("dfri.seed").joinpath("snapshot.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ReplayError("Seed snapshot must be a JSON object")
    return cast(dict[str, object], payload)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _publish_tree(output: Path, publication: dict[str, bytes]) -> str:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for relative, content in sorted(publication.items()):
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        candidate_hash = tree_hash(temporary)
        if output.exists():
            if not output.is_dir():
                raise ReplayError(f"Replay output exists and is not a directory: {output}")
            existing_hash = tree_hash(output)
            if existing_hash != candidate_hash:
                raise ReplayError(
                    f"Append-only replay collision at {output}; existing={existing_hash}, "
                    f"candidate={candidate_hash}"
                )
            return candidate_hash
        temporary.replace(output)
        return candidate_hash
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    digest = replay(as_of=args.as_of, output=args.output)
    print(json.dumps({"output": str(args.output), "tree_hash": digest}, sort_keys=True))


if __name__ == "__main__":
    main()
