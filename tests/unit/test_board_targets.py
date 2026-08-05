from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dfri.ingest.board import (
    BoardG19FirstPrintData,
    parse_g19_first_print_flows,
    release_timestamp,
)
from dfri.ingest.board_targets import (
    BoardTargetError,
    BoardTargetIngestor,
    first_print_target_rows,
    validate_board_targets,
)
from dfri.lake.store import AppendOnlyParquetStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "board"
FIXTURE = FIXTURES / "g19_20260708_first_print_excerpt.html"


def target_release() -> BoardG19FirstPrintData:
    return BoardG19FirstPrintData(
        archive_date=date(2026, 7, 8),
        release_date=date(2026, 7, 8),
        release_at=release_timestamp("g19", date(2026, 7, 8)),
        source_url="https://www.federalreserve.gov/releases/g19/20260708/",
        checksum="a" * 64,
        retrieved_at=datetime(2026, 8, 4, 18, 30, tzinfo=UTC),
        flows=parse_g19_first_print_flows(FIXTURE.read_bytes(), date(2026, 7, 8)),
    )


def test_actual_fixture_provenance_hash_is_current() -> None:
    provenance = json.loads(
        (FIXTURES / "g19_20260708_first_print_excerpt.provenance.json").read_text(encoding="utf-8")
    )
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == provenance["fixture_sha256"]
    assert provenance["source_url"].endswith("/releases/g19/20260708/")
    assert provenance["source_sha256"] == (
        "33a69cc4669561a3bb73cd9cbc76864e3c45f1b54a55c2cac931003d82124d11"
    )


def test_release_rows_keep_exact_target_identity_and_values() -> None:
    rows = first_print_target_rows(target_release())
    by_series = {str(row["series_id"]): row for row in rows}

    assert len(rows) == 2
    assert by_series["DELTA_DTCTLR.M"]["value"] == -5300.0
    assert by_series["DELTA_DTCTLN.M"]["value"] == 5100.0
    assert {row["obs_period"] for row in rows} == {date(2026, 5, 31)}
    assert {row["source"] for row in rows} == {"DFRI_DERIVED_BOARD_FIRST_PRINT_V1"}
    assert {row["release_date"] for row in rows} == {datetime(2026, 7, 8, 19, 0, tzinfo=UTC)}


def test_target_ingest_is_idempotent_and_validates_exact_page_coverage(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    ingestor = BoardTargetIngestor(store)

    first = ingestor.ingest(target_release())
    second = ingestor.ingest(target_release())
    validation = validate_board_targets(store, (date(2026, 7, 8),))

    assert not first.already_present
    assert second.already_present
    assert first.content_hash == second.content_hash
    assert validation.archive_pages == 1
    assert validation.row_count == 2
    assert validation.target_series == 2
    assert validation.first_target_period == date(2026, 5, 31)
    assert validation.last_target_period == date(2026, 5, 31)


def test_same_checksum_with_conflicting_stored_rows_fails_closed(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    release = target_release()
    rows = first_print_target_rows(release)
    rows[0]["value"] = -999.0
    store.append("raw_observations", rows)

    with pytest.raises(BoardTargetError, match="does not match"):
        BoardTargetIngestor(store).ingest(release)


def test_invalid_release_metadata_and_flow_fail_closed() -> None:
    release = target_release()
    with pytest.raises(BoardTargetError, match="URL mismatch"):
        first_print_target_rows(replace(release, source_url="https://example.test/"))

    bad_flow = replace(release.flows[0], value=release.flows[0].value + 1)
    with pytest.raises(BoardTargetError, match="does not equal"):
        first_print_target_rows(replace(release, flows=(bad_flow, release.flows[1])))


def test_fetch_rejects_non_g19_and_requires_client(tmp_path: Path) -> None:
    ingestor = BoardTargetIngestor(AppendOnlyParquetStore(tmp_path))
    with pytest.raises(BoardTargetError, match=r"G\.19 only"):
        ingestor.fetch_and_ingest("h8", date(2026, 7, 8))
    with pytest.raises(BoardTargetError, match="requires a Board client"):
        ingestor.fetch_and_ingest("g19", date(2026, 7, 8))
