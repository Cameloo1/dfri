"""Persist the public scoreboard ledgers in append-only repository history."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, cast

import pyarrow.parquet as pq

from dfri.lake.schemas import schema_for
from dfri.lake.store import AppendOnlyParquetStore, canonical_rows
from dfri.publish.ledger import GradeLedger, PredictionLedger, PublicationLedger

MANIFEST_NAME: Final = "MANIFEST.json"
MANIFEST_SCHEMA_VERSION: Final = 1
LEDGER_TABLES: Final = ("predictions", "grades", "publication_records")


class RepositoryLedgerError(RuntimeError):
    """Repository ledger state is missing, mutable, or internally inconsistent."""


@dataclass(frozen=True)
class RepositoryLedgerReceipt:
    root: Path
    file_count: int
    row_count: int
    manifest_hash: str
    added_files: int


def snapshot_repository_ledgers(runtime_root: Path, output_root: Path) -> RepositoryLedgerReceipt:
    """Copy a verified runtime ledger into a new repository-format snapshot."""

    if output_root.exists() and (not output_root.is_dir() or any(output_root.iterdir())):
        raise RepositoryLedgerError(f"Snapshot destination is not empty: {output_root}")
    manifest = _computed_manifest(runtime_root)
    _validate_semantics(runtime_root, manifest)
    entries = _manifest_entries(manifest)
    for entry in entries:
        relative = Path(cast(str, entry["path"]))
        _copy_exact(runtime_root / relative, output_root / relative)
    manifest_bytes = _manifest_bytes(manifest)
    _write_if_changed(output_root / MANIFEST_NAME, manifest_bytes)
    receipt = verify_repository_ledgers(output_root)
    return RepositoryLedgerReceipt(
        root=output_root,
        file_count=receipt.file_count,
        row_count=receipt.row_count,
        manifest_hash=receipt.manifest_hash,
        added_files=receipt.file_count,
    )


def verify_repository_ledgers(repository_root: Path) -> RepositoryLedgerReceipt:
    """Verify manifest bytes, canonical batch names, file bytes, and ledger relationships."""

    manifest_path = repository_root / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RepositoryLedgerError(f"Repository ledger manifest is missing: {manifest_path}")
    stored_bytes = manifest_path.read_bytes()
    try:
        stored = json.loads(stored_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepositoryLedgerError("Repository ledger manifest is not valid JSON") from exc
    computed = _computed_manifest(repository_root)
    if stored != computed:
        raise RepositoryLedgerError("Repository ledger manifest does not match ledger bytes")
    canonical_bytes = _manifest_bytes(computed)
    if stored_bytes != canonical_bytes:
        raise RepositoryLedgerError("Repository ledger manifest is not canonically encoded")
    _validate_semantics(repository_root, computed)
    entries = _manifest_entries(computed)
    return RepositoryLedgerReceipt(
        root=repository_root,
        file_count=len(entries),
        row_count=sum(cast(int, entry["rows"]) for entry in entries),
        manifest_hash=hashlib.sha256(canonical_bytes).hexdigest(),
        added_files=0,
    )


def restore_repository_ledgers(
    repository_root: Path, runtime_root: Path
) -> RepositoryLedgerReceipt:
    """Restore Git-authoritative ledgers over an empty or matching artifact cache."""

    receipt = verify_repository_ledgers(repository_root)
    manifest = _load_manifest(repository_root)
    expected = {cast(str, item["path"]): item for item in _manifest_entries(manifest)}
    added = 0
    for table in LEDGER_TABLES:
        table_root = runtime_root / table
        if table_root.is_symlink():
            raise RepositoryLedgerError(f"Runtime ledger table is a symlink: {table_root}")
        if table_root.exists() and not table_root.is_dir():
            raise RepositoryLedgerError(f"Runtime ledger table is not a directory: {table_root}")
        existing = sorted(table_root.glob("batch-*.parquet")) if table_root.exists() else []
        for path in existing:
            relative = path.relative_to(runtime_root).as_posix()
            entry = expected.get(relative)
            if entry is None:
                raise RepositoryLedgerError(
                    f"Artifact cache is ahead of repository history: {relative}"
                )
            if _sha256(path) != entry["sha256"]:
                raise RepositoryLedgerError(f"Artifact cache disagrees with Git: {relative}")
        unexpected = (
            sorted(
                path for path in table_root.rglob("*") if path.is_file() and path not in existing
            )
            if table_root.exists()
            else []
        )
        if unexpected:
            raise RepositoryLedgerError(
                f"Runtime ledger table contains an unmanaged file: {unexpected[0]}"
            )
    for relative, entry in expected.items():
        destination = runtime_root / Path(relative)
        if destination.exists():
            continue
        _copy_exact(repository_root / Path(relative), destination)
        if _sha256(destination) != entry["sha256"]:
            raise RepositoryLedgerError(f"Restored ledger checksum mismatch: {relative}")
        added += 1
    restored = _computed_manifest(runtime_root)
    if restored != manifest:
        raise RepositoryLedgerError("Restored runtime ledger is not byte-identical to Git")
    _validate_semantics(runtime_root, restored)
    return RepositoryLedgerReceipt(
        root=runtime_root,
        file_count=receipt.file_count,
        row_count=receipt.row_count,
        manifest_hash=receipt.manifest_hash,
        added_files=added,
    )


def merge_repository_candidate(
    candidate_root: Path, repository_root: Path
) -> RepositoryLedgerReceipt:
    """Promote only new immutable batches from a verified candidate into Git state."""

    candidate_receipt = verify_repository_ledgers(candidate_root)
    verify_repository_ledgers(repository_root)
    candidate = _load_manifest(candidate_root)
    repository = _load_manifest(repository_root)
    candidate_entries = {cast(str, entry["path"]): entry for entry in _manifest_entries(candidate)}
    repository_entries = {
        cast(str, entry["path"]): entry for entry in _manifest_entries(repository)
    }
    candidate_tables = cast(dict[str, dict[str, object]], candidate["tables"])
    repository_tables = cast(dict[str, dict[str, object]], repository["tables"])
    missing = sorted(set(repository_entries) - set(candidate_entries))
    if missing:
        raise RepositoryLedgerError(f"Candidate deletes repository ledger batch: {missing[0]}")
    for relative, current in repository_entries.items():
        proposed = candidate_entries[relative]
        if current != proposed:
            raise RepositoryLedgerError(f"Candidate modifies repository ledger batch: {relative}")
        if (repository_root / Path(relative)).read_bytes() != (
            candidate_root / Path(relative)
        ).read_bytes():
            raise RepositoryLedgerError(f"Candidate changes immutable ledger bytes: {relative}")
    for table in LEDGER_TABLES:
        current_ids = set(cast(list[str], repository_tables[table]["record_ids"]))
        candidate_ids = set(cast(list[str], candidate_tables[table]["record_ids"]))
        if not current_ids <= candidate_ids:
            raise RepositoryLedgerError(f"Candidate deletes {table} record IDs")
    added_paths = sorted(set(candidate_entries) - set(repository_entries))
    for relative in added_paths:
        _copy_exact(candidate_root / Path(relative), repository_root / Path(relative))
    candidate_manifest_bytes = (candidate_root / MANIFEST_NAME).read_bytes()
    _write_if_changed(repository_root / MANIFEST_NAME, candidate_manifest_bytes)
    merged = verify_repository_ledgers(repository_root)
    if merged.manifest_hash != candidate_receipt.manifest_hash:
        raise RepositoryLedgerError("Merged repository ledger differs from candidate")
    return RepositoryLedgerReceipt(
        root=repository_root,
        file_count=merged.file_count,
        row_count=merged.row_count,
        manifest_hash=merged.manifest_hash,
        added_files=len(added_paths),
    )


def _computed_manifest(root: Path) -> dict[str, object]:
    tables: dict[str, object] = {}
    for table in LEDGER_TABLES:
        table_root = root / table
        paths = sorted(table_root.glob("batch-*.parquet")) if table_root.is_dir() else []
        files: list[dict[str, object]] = []
        record_ids: list[str] = []
        for path in paths:
            if path.is_symlink():
                raise RepositoryLedgerError(f"Repository ledger refuses symlink: {path}")
            arrow = pq.read_table(path, schema=schema_for(table))
            rows = cast(list[dict[str, object]], arrow.to_pylist())
            canonical_hash = hashlib.sha256(canonical_rows(rows)).hexdigest()
            expected_name = f"batch-{canonical_hash}.parquet"
            if path.name != expected_name:
                raise RepositoryLedgerError(
                    f"Ledger batch name does not match canonical rows: {path.name}"
                )
            ids = [str(row["prediction_id"]) for row in rows]
            if len(ids) != len(set(ids)):
                raise RepositoryLedgerError(f"Ledger batch contains duplicate IDs: {path.name}")
            record_ids.extend(ids)
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                    "rows": arrow.num_rows,
                    "canonical_hash": canonical_hash,
                    "sha256": _sha256(path),
                }
            )
        tables[table] = {
            "files": files,
            "record_ids": sorted(record_ids),
        }
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "tables": tables}


def _validate_semantics(root: Path, manifest: dict[str, object]) -> None:
    predictions = PredictionLedger(AppendOnlyParquetStore(root)).read_all()
    grades = GradeLedger(AppendOnlyParquetStore(root)).read_all()
    publications = PublicationLedger(AppendOnlyParquetStore(root)).read_all()
    prediction_ids = {item.prediction_id for item in predictions}
    grade_ids = {item.prediction_id for item in grades}
    publication_ids = {item.prediction_id for item in publications}
    if not prediction_ids:
        raise RepositoryLedgerError("Repository prediction ledger is empty")
    if not grade_ids <= prediction_ids:
        raise RepositoryLedgerError("Repository grade ledger contains an orphan")
    if publication_ids != prediction_ids:
        raise RepositoryLedgerError("Every repository prediction must have one publication record")
    tables = cast(dict[str, dict[str, object]], manifest["tables"])
    expected = {
        "predictions": sorted(prediction_ids),
        "grades": sorted(grade_ids),
        "publication_records": sorted(publication_ids),
    }
    for table, ids in expected.items():
        if cast(list[str], tables[table]["record_ids"]) != ids:
            raise RepositoryLedgerError(f"Repository {table} manifest IDs disagree with rows")


def _load_manifest(root: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads((root / MANIFEST_NAME).read_bytes()))


def _manifest_entries(manifest: dict[str, object]) -> list[dict[str, object]]:
    tables = cast(dict[str, dict[str, object]], manifest["tables"])
    return [
        entry
        for table in LEDGER_TABLES
        for entry in cast(list[dict[str, object]], tables[table]["files"])
    ]


def _manifest_bytes(manifest: dict[str, object]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def _copy_exact(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise RepositoryLedgerError(f"Ledger source is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        if destination.exists():
            if destination.read_bytes() != temporary.read_bytes():
                raise RepositoryLedgerError(f"Refusing to overwrite ledger file: {destination}")
            return
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_if_changed(path: Path, content: bytes) -> None:
    if path.exists() and path.read_bytes() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--repository-root", type=Path, default=Path("state/ledgers"))
    restore = subparsers.add_parser("restore")
    restore.add_argument("--repository-root", type=Path, default=Path("state/ledgers"))
    restore.add_argument("--runtime-root", type=Path, default=Path(".local/lake/curated"))
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--runtime-root", type=Path, required=True)
    snapshot.add_argument("--output-root", type=Path, required=True)
    merge = subparsers.add_parser("merge")
    merge.add_argument("--candidate-root", type=Path, required=True)
    merge.add_argument("--repository-root", type=Path, default=Path("state/ledgers"))
    args = parser.parse_args()
    if args.command == "verify":
        receipt = verify_repository_ledgers(args.repository_root)
    elif args.command == "restore":
        receipt = restore_repository_ledgers(args.repository_root, args.runtime_root)
    elif args.command == "snapshot":
        receipt = snapshot_repository_ledgers(args.runtime_root, args.output_root)
    else:
        receipt = merge_repository_candidate(args.candidate_root, args.repository_root)
    print(json.dumps({**asdict(receipt), "root": str(receipt.root)}, sort_keys=True))


if __name__ == "__main__":
    main()
