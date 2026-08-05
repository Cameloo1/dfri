from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from dfri.ops.state_bundle import (
    MANIFEST_NAME,
    StateBundleError,
    main,
    pack_state_bundle,
    unpack_state_bundle,
)


def seeded_state(root: Path) -> dict[Path, bytes]:
    payloads = {
        Path("lake/raw/raw_observations/batch-a.parquet"): b"raw-public-data",
        Path("lake/curated/predictions/batch-b.parquet"): b"prediction-ledger",
        Path("lake/curated/grades/batch-c.parquet"): b"grade-ledger",
        Path("lake/curated/publication_records/batch-d.parquet"): b"publication-ledger",
        Path("lake/curated/attribution_refreshes/batch-e.parquet"): b"refresh-ledger",
        Path("state/board-backfill.json"): b'{"schema_version":1}\n',
        Path("evidence/scoreboard_jobs/predict-abc.json"): b'{"kind":"predict"}\n',
        Path("evidence/quarterly_refresh/q1.json"): b'{"kind":"quarterly-refresh"}\n',
    }
    for relative, content in payloads.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    private = root / "lake" / "raw" / "_private" / "secret.xml"
    private.parent.mkdir(parents=True)
    private.write_text("must not leave the runner", encoding="utf-8")
    return payloads


def test_bundle_is_deterministic_allowlisted_and_round_trips(tmp_path: Path) -> None:
    root = tmp_path / "state"
    expected = seeded_state(root)
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    first_receipt = pack_state_bundle(root, first)
    second_receipt = pack_state_bundle(root, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_receipt.manifest_hash == second_receipt.manifest_hash
    with tarfile.open(first, "r:gz") as archive:
        names = archive.getnames()
    assert "MANIFEST.json" in names
    assert all("_private" not in name and "secret" not in name for name in names)

    restored = tmp_path / "restored"
    receipt = unpack_state_bundle(first, restored)
    assert receipt.file_count == len(expected)
    assert {
        path.relative_to(restored): path.read_bytes()
        for path in restored.rglob("*")
        if path.is_file()
    } == expected


def test_restore_refuses_unmanaged_destination_and_corrupt_bundle(tmp_path: Path) -> None:
    root = tmp_path / "state"
    seeded_state(root)
    bundle = tmp_path / "state.tar.gz"
    pack_state_bundle(root, bundle)
    destination = tmp_path / "restored"
    destination.mkdir()
    marker = destination / "user.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(StateBundleError, match="non-empty"):
        unpack_state_bundle(bundle, destination)
    assert marker.read_text(encoding="utf-8") == "preserve"

    corrupt = tmp_path / "corrupt.tar.gz"
    corrupt.write_bytes(bundle.read_bytes()[:32])
    with pytest.raises(StateBundleError, match="Cannot read"):
        unpack_state_bundle(corrupt, tmp_path / "corrupt-restore")


def test_pack_rejects_empty_state(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    with pytest.raises(StateBundleError, match="no allowlisted"):
        pack_state_bundle(root, tmp_path / "empty.tar.gz")


def write_bundle(path: Path, manifest: object, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        payloads = {MANIFEST_NAME: (json.dumps(manifest) + "\n").encode(), **members}
        for name, content in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def test_restore_rejects_invalid_manifest_and_non_allowlisted_paths(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.tar.gz"
    write_bundle(
        invalid,
        {
            "schema_version": 1,
            "files": [{"path": "state/board-backfill.json", "bytes": 2, "sha256": "z" * 64}],
        },
        {"state/board-backfill.json": b"{}"},
    )
    with pytest.raises(StateBundleError, match="entry is invalid"):
        unpack_state_bundle(invalid, tmp_path / "invalid-restore")

    traversal = tmp_path / "traversal.tar.gz"
    content = b"secret"
    write_bundle(
        traversal,
        {
            "schema_version": 1,
            "files": [
                {
                    "path": "../secret",
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        },
        {"../secret": content},
    )
    with pytest.raises(StateBundleError, match="not allowlisted"):
        unpack_state_bundle(traversal, tmp_path / "traversal-restore")
    assert not (tmp_path / "secret").exists()


def test_state_bundle_cli_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "state"
    expected = seeded_state(root)
    bundle = tmp_path / "state.tar.gz"
    monkeypatch.setattr(
        sys, "argv", ["dfri-state-bundle", "pack", "--root", str(root), "--bundle", str(bundle)]
    )
    main()

    restored = tmp_path / "restored"
    monkeypatch.setattr(
        sys,
        "argv",
        ["dfri-state-bundle", "unpack", "--root", str(restored), "--bundle", str(bundle)],
    )
    main()
    assert {
        path.relative_to(restored): path.read_bytes()
        for path in restored.rglob("*")
        if path.is_file()
    } == expected
