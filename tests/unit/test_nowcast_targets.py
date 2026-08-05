from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from dfri.lake.guard import VintageGuard
from dfri.lake.readers import LakeSeriesReader
from dfri.lake.store import AppendOnlyParquetStore
from dfri.nowcast.targets import TargetDatasetError, read_first_print_targets


def row(period: date, release_at: datetime, value: float, checksum: str) -> dict[str, object]:
    return {
        "source": "DFRI_DERIVED_BOARD_FIRST_PRINT_V1",
        "series_id": "DELTA_DTCTLR.M",
        "obs_period": period,
        "value": value,
        "unit": "Millions of U.S. Dollars",
        "release_date": release_at,
        "vintage_date": release_at.date(),
        "ingested_at": release_at,
        "source_url": (
            f"https://www.federalreserve.gov/releases/g19/{release_at.strftime('%Y%m%d')}/"
        ),
        "checksum": checksum,
    }


def guarded(tmp_path: Path, rows: list[dict[str, object]]) -> VintageGuard:
    store = AppendOnlyParquetStore(tmp_path)
    store.append("raw_observations", rows)
    return VintageGuard(LakeSeriesReader(store))


def test_target_reader_filters_future_release_and_preserves_provenance(tmp_path: Path) -> None:
    rows = [
        row(date(2024, 1, 31), datetime(2024, 3, 7, 20, tzinfo=UTC), 100.0, "a" * 64),
        row(date(2024, 2, 29), datetime(2024, 4, 5, 19, tzinfo=UTC), 200.0, "b" * 64),
        row(date(2024, 3, 31), datetime(2024, 5, 7, 19, tzinfo=UTC), 999999.0, "c" * 64),
    ]

    targets = read_first_print_targets(
        guarded(tmp_path, rows),
        "DELTA_DTCTLR.M",
        datetime(2024, 4, 15, tzinfo=UTC),
    )

    assert [target.value for target in targets] == [100.0, 200.0]
    assert targets[-1].level_series == "DTCTLR.M"
    assert targets[-1].source_url.endswith("/20240405/")


def test_target_reader_applies_start_after_continuity_validation(tmp_path: Path) -> None:
    rows = [
        row(date(2024, 1, 31), datetime(2024, 3, 7, 20, tzinfo=UTC), 100.0, "a" * 64),
        row(date(2024, 2, 29), datetime(2024, 4, 5, 19, tzinfo=UTC), 200.0, "b" * 64),
    ]
    targets = read_first_print_targets(
        guarded(tmp_path, rows),
        "DELTA_DTCTLR.M",
        date(2024, 4, 30),
        start=date(2024, 2, 1),
    )
    assert [target.target_period for target in targets] == [date(2024, 2, 29)]


def test_target_reader_rejects_gaps_and_unknown_series(tmp_path: Path) -> None:
    rows = [
        row(date(2024, 1, 31), datetime(2024, 3, 7, 20, tzinfo=UTC), 100.0, "a" * 64),
        row(date(2024, 3, 31), datetime(2024, 5, 7, 19, tzinfo=UTC), 300.0, "b" * 64),
    ]
    guard = guarded(tmp_path, rows)
    with pytest.raises(TargetDatasetError, match="monthly gap"):
        read_first_print_targets(guard, "DELTA_DTCTLR.M", date(2024, 6, 1))
    with pytest.raises(TargetDatasetError, match="Unsupported"):
        read_first_print_targets(guard, "UNKNOWN", date(2024, 6, 1))


def test_target_reader_rejects_conflicting_same_release_values(tmp_path: Path) -> None:
    release_at = datetime(2024, 3, 7, 20, tzinfo=UTC)
    rows = [
        row(date(2024, 1, 31), release_at, 100.0, "a" * 64),
        row(date(2024, 1, 31), release_at, 101.0, "b" * 64),
    ]
    with pytest.raises(TargetDatasetError, match="conflicting"):
        read_first_print_targets(guarded(tmp_path, rows), "DELTA_DTCTLR.M", date(2024, 4, 1))


def test_target_reader_returns_empty_when_no_derived_rows_exist(tmp_path: Path) -> None:
    guard = VintageGuard(LakeSeriesReader(AppendOnlyParquetStore(tmp_path)))
    assert read_first_print_targets(guard, "DELTA_DTCTLR.M", date(2024, 4, 1)) == ()


def test_target_reader_rejects_missing_contract_columns() -> None:
    class IncompleteGuard:
        def read(self, _series_id: str, _as_of: date) -> pl.DataFrame:
            return pl.DataFrame({"series_id": ["DELTA_DTCTLR.M"]})

    with pytest.raises(TargetDatasetError, match="missing columns"):
        read_first_print_targets(  # type: ignore[arg-type]
            IncompleteGuard(), "DELTA_DTCTLR.M", date(2024, 4, 1)
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"unit": "Dollars"}, "unit changed"),
        ({"source_url": "https://example.test/"}, r"not a dated G\.19"),
        ({"checksum": "bad"}, "not lowercase SHA-256"),
        ({"value": float("inf")}, "must be finite"),
        (
            {
                "obs_period": date(2024, 3, 31),
                "release_date": datetime(2024, 3, 7, 20, tzinfo=UTC),
            },
            "not before its release",
        ),
    ],
)
def test_target_reader_rejects_malformed_evidence(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    valid = row(date(2024, 1, 31), datetime(2024, 3, 7, 20, tzinfo=UTC), 100.0, "a" * 64)
    valid.update(change)
    with pytest.raises(TargetDatasetError, match=message):
        read_first_print_targets(guarded(tmp_path, [valid]), "DELTA_DTCTLR.M", date(2024, 4, 1))


def test_target_reader_rejects_multiple_archive_releases_for_one_month(tmp_path: Path) -> None:
    rows = [
        row(date(2024, 1, 31), datetime(2024, 3, 7, 20, tzinfo=UTC), 100.0, "a" * 64),
        row(date(2024, 1, 31), datetime(2024, 3, 8, 20, tzinfo=UTC), 100.0, "b" * 64),
    ]
    with pytest.raises(TargetDatasetError, match="multiple archive releases"):
        read_first_print_targets(guarded(tmp_path, rows), "DELTA_DTCTLR.M", date(2024, 4, 1))
