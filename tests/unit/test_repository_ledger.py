from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dfri.lake.store import AppendOnlyParquetStore
from dfri.ops.repository_ledger import (
    RepositoryLedgerError,
    main,
    merge_repository_candidate,
    restore_repository_ledgers,
    snapshot_repository_ledgers,
    verify_repository_ledgers,
)
from dfri.publish.ledger import GradeRecord, PredictionRecord, PublicationRecord

PREDICTION_ID = "prd_" + "a" * 64


def seed_runtime(root: Path) -> dict[Path, bytes]:
    store = AppendOnlyParquetStore(root)
    prediction = PredictionRecord(
        prediction_id=PREDICTION_ID,
        made_at=datetime(2026, 7, 31, 21, 15, tzinfo=UTC),
        model_version="nowcast-v2",
        inputs_hash="b" * 64,
        target_series="DELTA_REVOLSL.M",
        target_period=date(2026, 7, 31),
        point=-5_529.0,
        low80=-9_000.0,
        high80=-2_000.0,
        low95=-12_000.0,
        high95=1_000.0,
    )
    grade = GradeRecord(
        prediction_id=PREDICTION_ID,
        actual_first_print=6_800.0,
        vintage_url="https://www.federalreserve.gov/releases/g19/20260807/",
        abs_error=12_329.0,
        graded_at=datetime(2026, 8, 7, 19, 0, tzinfo=UTC),
    )
    publication = PublicationRecord(
        prediction_id=PREDICTION_ID,
        published_at=datetime(2026, 7, 31, 22, 0, tzinfo=UTC),
        data_vintage=datetime(2026, 7, 31, 20, 15, tzinfo=UTC),
        methodology_version="1.1.1",
    )
    store.append("predictions", [prediction.row()])
    store.append("grades", [grade.row()])
    store.append("publication_records", [publication.row()])
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("batch-*.parquet")}


def tree_bytes(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_snapshot_and_fresh_clone_restore_are_byte_identical(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    original = seed_runtime(runtime)
    repository = tmp_path / "repository" / "state" / "ledgers"

    snapshot = snapshot_repository_ledgers(runtime, repository)
    assert snapshot.file_count == 3
    assert snapshot.row_count == 3
    assert {path: (repository / path).read_bytes() for path in original} == original

    fresh_clone_runtime = tmp_path / "fresh-clone" / ".local" / "lake" / "curated"
    restored = restore_repository_ledgers(repository, fresh_clone_runtime)
    assert restored.added_files == 3
    assert {path: (fresh_clone_runtime / path).read_bytes() for path in original} == original
    assert verify_repository_ledgers(repository).manifest_hash == snapshot.manifest_hash


def test_restore_accepts_matching_cache_and_rejects_cache_ahead_of_git(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    seed_runtime(runtime)
    repository = tmp_path / "repository"
    snapshot_repository_ledgers(runtime, repository)

    cached = tmp_path / "cached"
    shutil.copytree(runtime, cached)
    before = tree_bytes(cached)
    receipt = restore_repository_ledgers(repository, cached)
    assert receipt.added_files == 0
    assert tree_bytes(cached) == before

    extra = cached / "predictions" / ("batch-" + "c" * 64 + ".parquet")
    extra.write_bytes(next((cached / "predictions").glob("batch-*.parquet")).read_bytes())
    with pytest.raises(RepositoryLedgerError, match="ahead of repository"):
        restore_repository_ledgers(repository, cached)


def test_candidate_merge_is_append_only_and_noop_does_not_rewrite(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    seed_runtime(runtime)
    repository = tmp_path / "repository"
    snapshot_repository_ledgers(runtime, repository)

    candidate = tmp_path / "candidate"
    snapshot_repository_ledgers(runtime, candidate)
    before = tree_bytes(repository)
    mtimes = {path: path.stat().st_mtime_ns for path in repository.rglob("*") if path.is_file()}
    merged = merge_repository_candidate(candidate, repository)
    assert merged.added_files == 0
    assert tree_bytes(repository) == before
    assert {path: path.stat().st_mtime_ns for path in mtimes} == mtimes

    prediction = next((candidate / "predictions").glob("batch-*.parquet"))
    content = bytearray(prediction.read_bytes())
    content[len(content) // 2] ^= 1
    prediction.write_bytes(bytes(content))
    with pytest.raises(RepositoryLedgerError, match="manifest"):
        merge_repository_candidate(candidate, repository)


def test_manifest_records_canonical_hashes_and_rejects_deletion(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    seed_runtime(runtime)
    repository = tmp_path / "repository"
    snapshot_repository_ledgers(runtime, repository)
    manifest = json.loads((repository / "MANIFEST.json").read_text())
    for table in manifest["tables"].values():
        for entry in table["files"]:
            assert Path(entry["path"]).name == f"batch-{entry['canonical_hash']}.parquet"

    candidate = tmp_path / "candidate"
    snapshot_repository_ledgers(runtime, candidate)
    grade = next((candidate / "grades").glob("batch-*.parquet"))
    grade.unlink()
    with pytest.raises(RepositoryLedgerError, match="manifest"):
        merge_repository_candidate(candidate, repository)


def test_repository_ledger_cli_runs_snapshot_verify_restore_and_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "runtime"
    seed_runtime(runtime)
    repository = tmp_path / "repository"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dfri-repository-ledger",
            "snapshot",
            "--runtime-root",
            str(runtime),
            "--output-root",
            str(repository),
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["added_files"] == 3

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dfri-repository-ledger",
            "verify",
            "--repository-root",
            str(repository),
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["row_count"] == 3

    restored = tmp_path / "restored"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dfri-repository-ledger",
            "restore",
            "--repository-root",
            str(repository),
            "--runtime-root",
            str(restored),
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["added_files"] == 3

    candidate = tmp_path / "candidate"
    snapshot_repository_ledgers(restored, candidate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dfri-repository-ledger",
            "merge",
            "--candidate-root",
            str(candidate),
            "--repository-root",
            str(repository),
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["added_files"] == 0


def test_repository_ledger_rejects_nonempty_snapshot_and_missing_manifest(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    seed_runtime(runtime)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(RepositoryLedgerError, match="not empty"):
        snapshot_repository_ledgers(runtime, occupied)
    with pytest.raises(RepositoryLedgerError, match="manifest is missing"):
        verify_repository_ledgers(tmp_path / "missing")
