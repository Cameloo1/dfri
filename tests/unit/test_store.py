from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from dfri.lake.schemas import table_from_rows
from dfri.lake.store import AppendOnlyParquetStore, AppendOnlyViolationError, file_sha256


def row(value: float = 1336.7) -> dict[str, object]:
    return {
        "source": "FEDERAL_RESERVE_BOARD",
        "series_id": "DTCTLR.M",
        "obs_period": date(2024, 1, 1),
        "value": value,
        "unit": "Billions of Dollars",
        "release_date": datetime(2024, 3, 7, 20, 0, tzinfo=UTC),
        "vintage_date": date(2024, 3, 7),
        "ingested_at": datetime(2024, 3, 7, 20, 5, tzinfo=UTC),
        "source_url": "https://www.federalreserve.gov/releases/g19/data/FRB_g19_xml.zip",
        "checksum": "a" * 64,
    }


def test_append_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    first = store.append("raw_observations", [row()])
    second = store.append("raw_observations", [row()])

    assert first.path == second.path
    assert first.already_present is False
    assert second.already_present is True
    assert file_sha256(first.path) == file_sha256(second.path)
    assert store.read_table("raw_observations").height == 1


def test_changed_batch_appends_without_overwriting(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    first = store.append("raw_observations", [row()])
    second = store.append("raw_observations", [row(1337.0)])

    assert first.path != second.path
    assert len(list((tmp_path / "raw_observations").glob("*.parquet"))) == 2
    assert store.read_table("raw_observations").height == 2


def test_retry_rejects_changed_content_with_same_row_count(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    first = store.append("raw_observations", [row()])
    pq.write_table(table_from_rows("raw_observations", [row(999.0)]), first.path)
    corrupted = first.path.read_bytes()
    with pytest.raises(AppendOnlyViolationError, match="Content-address"):
        store.append("raw_observations", [row()])
    assert first.path.read_bytes() == corrupted
