from __future__ import annotations

import json
from pathlib import Path

import pytest

from dfri.seed.publication import SeedPublicationError, _load_snapshot, publish_seed_snapshot


def test_frozen_public_snapshot_builds_complete_byte_identical_publication(
    tmp_path: Path,
) -> None:
    output = tmp_path / "public"
    evidence = tmp_path / "evidence" / "publication.json"

    first = publish_seed_snapshot(output, evidence=evidence, project_root=Path(__file__).parents[2])
    before = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }
    second = publish_seed_snapshot(
        output, evidence=evidence, project_root=Path(__file__).parents[2]
    )
    after = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }

    assert first == second
    assert before == after
    assert first.prediction_count == 4
    assert first.graded_count == 0
    assert (output / "v1" / "feeds" / "schema.json").is_file()
    assert len(list((output / "companies").glob("*/index.html"))) == 10
    receipt = json.loads(evidence.read_text(encoding="utf-8"))
    assert receipt["status"] == "PASS"
    assert receipt["replay_byte_identical"] is True
    assert receipt["snapshot_id"] == "first-public-scoreboard-2026-08-05"

    no_replay = publish_seed_snapshot(
        tmp_path / "no-replay",
        verify_determinism=False,
        project_root=Path(__file__).parents[2],
    )
    assert no_replay.prediction_count == 4


def test_frozen_publication_snapshot_rejects_schema_and_timestamp_errors(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SeedPublicationError, match="schema"):
        _load_snapshot(path)

    payload = {
        "schema_version": "v1",
        "snapshot_id": "bad",
        "source_url": "https://example.test",
        "captured_at": "2026-08-05T04:00:00+00:00",
        "published_at": "2026-08-05T04:00:00+00:00",
        "data_vintage": "2026-08-01T00:00:00",
        "rows": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SeedPublicationError, match="prediction rows"):
        _load_snapshot(path)
