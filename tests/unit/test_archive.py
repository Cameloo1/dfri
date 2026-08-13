from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dfri.ops.archive import (
    ArchiveError,
    _canonical_source_bytes,
    create_archive,
    main,
    prove_round_trip,
    verify_archive,
)


def test_archive_is_deterministic_and_round_trips_repository_ledgers(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    first_receipt = create_archive(root, first)
    second_receipt = create_archive(root, second)
    proof = prove_round_trip(root, tmp_path / "proof.tar.gz")

    assert first.read_bytes() == second.read_bytes()
    assert first_receipt == verify_archive(first)
    assert first_receipt.ledger_manifest_hash == second_receipt.ledger_manifest_hash
    assert proof["status"] == "PASS"
    assert proof["deterministic_sha256"]


def test_archive_rejects_truncation(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    archive = tmp_path / "ledger.tar.gz"
    create_archive(root, archive)
    archive.write_bytes(archive.read_bytes()[:80])

    with pytest.raises(ArchiveError, match="Cannot read"):
        verify_archive(archive)


def test_archive_canonicalizes_tracked_text_line_endings(tmp_path: Path) -> None:
    source = tmp_path / "CITATION.cff"
    source.write_bytes(b"cff-version: 1.2.0\r\ntitle: DFRI\r\n")

    assert _canonical_source_bytes(source, "CITATION.cff") == (b"cff-version: 1.2.0\ntitle: DFRI\n")


@pytest.mark.parametrize("command", ["create", "verify", "round-trip"])
def test_archive_cli_commands(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = Path(__file__).parents[2]
    archive = tmp_path / "ledger.tar.gz"
    if command == "verify":
        create_archive(root, archive)
    argv = ["archive", command, "--archive", str(archive)]
    if command != "verify":
        argv.extend(["--repository-root", str(root)])
    monkeypatch.setattr(sys, "argv", argv)

    assert main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["archive"] == str(archive)
    if command == "round-trip":
        assert payload["status"] == "PASS"
