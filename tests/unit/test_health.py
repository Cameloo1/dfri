from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

import dfri.api.health as health
from dfri.api.health import (
    FreshnessPolicy,
    HealthContractError,
    LakeFreshnessPolicy,
    _lake_source_freshness,
    calculate_health,
    calculate_store_health,
    default_freshness_policies,
    default_lake_freshness_policies,
)
from dfri.lake.store import AppendOnlyParquetStore

NOW = datetime(2026, 8, 4, 16, 0, tzinfo=UTC)


def policy(*, optional: bool = False) -> FreshnessPolicy:
    return FreshnessPolicy(
        source_id="TEST",
        source="TEST_SOURCE",
        series_ids=frozenset({"A", "B"}),
        calendar_prefixes=("Test release ",),
        max_age=timedelta(days=10),
        release_grace=timedelta(hours=4),
        optional=optional,
    )


def observations(*rows: tuple[str, str, datetime]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "source": [row[0] for row in rows],
            "series_id": [row[1] for row in rows],
            "ingested_at": [row[2] for row in rows],
        },
        schema={
            "source": pl.String,
            "series_id": pl.String,
            "ingested_at": pl.Datetime("us", "UTC"),
        },
    )


def calendar_row(
    *,
    expected_at: datetime | None,
    status: str = "EXPECTED_OFFICIAL",
    release_name: str = "Test release 2026-08",
) -> dict[str, object]:
    return {
        "release_name": release_name,
        "expected_at": expected_at,
        "actual_at": None,
        "status": status,
    }


def test_default_policies_cover_every_registered_macro_series() -> None:
    policies = default_freshness_policies()
    counts = {item.source_id: len(item.series_ids) for item in policies}

    assert counts == {
        "BOARD_G19": 6,
        "BOARD_H8": 3,
        "BEA_CONTEXT": 14,
        "CENSUS_MARTS": 6,
        "NYFED_HHDC": 21,
    }
    assert {item.source for item in policies if item.source_id.startswith("BOARD_")} == {
        "FEDERAL_RESERVE_BOARD"
    }
    assert all(not item.optional for item in policies)


def test_default_lake_policies_cover_every_verified_sec_lane() -> None:
    policies = default_lake_freshness_policies()
    counts = {item.source_id: len(item.expected_entities) for item in policies}

    assert counts == {
        "SEC_XBRL": 17,
        "SEC_HTML_EVIDENCE": 1,
        "SEC_AUTO_ABS_ACTIVE": 5,
        "SEC_AUTO_ABS_TERMINAL": 1,
        "SEC_CARD_10D": 3,
    }
    assert {item.watermark_column for item in policies} == {
        "ingested_at",
        "reporting_period_end",
    }
    assert all(not item.optional for item in policies)


class _FrameStore:
    def __init__(self, frames: dict[str, pl.DataFrame]) -> None:
        self.frames = frames

    def read_table(self, table_name: str) -> pl.DataFrame:
        return self.frames[table_name]


def _lake_policy(*, optional: bool = False) -> LakeFreshnessPolicy:
    return LakeFreshnessPolicy(
        source_id="LAKE_TEST",
        table_name="test_table",
        entity_column="entity",
        expected_entities=frozenset({"A", "B"}),
        watermark_column="period",
        max_age=timedelta(days=62),
        optional=optional,
    )


def _lake_frame(*rows: tuple[str, date, datetime]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "entity": [row[0] for row in rows],
            "period": [row[1] for row in rows],
            "ingested_at": [row[2] for row in rows],
        },
        schema={
            "entity": pl.String,
            "period": pl.Date,
            "ingested_at": pl.Datetime("us", "UTC"),
        },
    )


def test_lake_freshness_uses_entity_coverage_and_subject_period_watermark() -> None:
    current = _FrameStore(
        {
            "test_table": _lake_frame(
                ("A", date(2026, 6, 30), NOW),
                ("B", date(2026, 6, 25), NOW),
            )
        }
    )
    green = _lake_source_freshness(current, as_of=NOW, policy=_lake_policy())  # type: ignore[arg-type]
    stale = _lake_source_freshness(
        current,
        as_of=NOW + timedelta(days=30),
        policy=_lake_policy(),
    )  # type: ignore[arg-type]
    missing = _lake_source_freshness(
        _FrameStore({"test_table": _lake_frame(("A", date(2026, 6, 30), NOW))}),
        as_of=NOW,
        policy=_lake_policy(),
    )  # type: ignore[arg-type]
    optional = _lake_source_freshness(
        _FrameStore({"test_table": _lake_frame()}),
        as_of=NOW,
        policy=_lake_policy(optional=True),
    )  # type: ignore[arg-type]

    assert green.status == "GREEN"
    assert green.watermark == datetime(2026, 6, 25, tzinfo=UTC)
    assert stale.status == "STALE"
    assert stale.reason == "MAX_AGE_EXCEEDED"
    assert missing.status == "BLOCKED"
    assert missing.reason == "ENTITY_COVERAGE_INCOMPLETE"
    assert optional.status == "OPTIONAL-DEGRADED"
    assert optional.reason == "NO_OBSERVATION"


def test_store_health_combines_calendar_and_lake_sources() -> None:
    store = _FrameStore(
        {
            "raw_observations": observations(
                ("TEST_SOURCE", "A", NOW),
                ("TEST_SOURCE", "B", NOW),
            ),
            "test_table": _lake_frame(
                ("A", date(2026, 6, 30), NOW),
                ("B", date(2026, 6, 25), NOW),
            ),
        }
    )
    report = calculate_store_health(
        store,  # type: ignore[arg-type]
        as_of=NOW,
        calendar=[],
        policies=(policy(),),
        lake_policies=(_lake_policy(),),
    )

    assert report.status == "GREEN"
    assert [item.source_id for item in report.sources] == ["TEST", "LAKE_TEST"]


def test_current_complete_source_is_green_and_reports_next_release() -> None:
    next_release = NOW + timedelta(days=2)
    report = calculate_health(
        observations(
            ("TEST_SOURCE", "A", NOW - timedelta(hours=2)),
            ("TEST_SOURCE", "B", NOW - timedelta(hours=1)),
        ),
        [calendar_row(expected_at=next_release)],
        as_of=NOW,
        policies=(policy(),),
    )

    assert report.status == "GREEN"
    assert report.sources[0].status == "GREEN"
    assert report.sources[0].reason == "CURRENT"
    assert report.sources[0].watermark == NOW - timedelta(hours=2)
    assert report.sources[0].next_expected_at == next_release


def test_release_due_within_grace_is_green_but_missed_sla_is_stale() -> None:
    source_rows = observations(
        ("TEST_SOURCE", "A", NOW - timedelta(days=1)),
        ("TEST_SOURCE", "B", NOW - timedelta(days=1)),
    )
    within = calculate_health(
        source_rows,
        [calendar_row(expected_at=NOW - timedelta(hours=1))],
        as_of=NOW,
        policies=(policy(),),
    )
    missed = calculate_health(
        source_rows,
        [calendar_row(expected_at=NOW - timedelta(hours=5))],
        as_of=NOW,
        policies=(policy(),),
    )

    assert within.sources[0].status == "GREEN"
    assert within.sources[0].reason == "AWAITING_RELEASE_WITHIN_SLA"
    assert missed.status == "STALE"
    assert missed.sources[0].reason == "MISSED_RELEASE_SLA"


def test_missing_required_and_optional_sources_have_explicit_states() -> None:
    empty = observations()
    required = calculate_health(empty, [], as_of=NOW, policies=(policy(),))
    optional = calculate_health(empty, [], as_of=NOW, policies=(policy(optional=True),))

    assert required.status == "BLOCKED"
    assert required.sources[0].reason == "NO_OBSERVATION"
    assert optional.status == "OPTIONAL-DEGRADED"
    assert optional.sources[0].reason == "NO_OBSERVATION"


def test_partial_coverage_blocks_even_when_present_series_is_current() -> None:
    report = calculate_health(
        observations(("TEST_SOURCE", "A", NOW)),
        [],
        as_of=NOW,
        policies=(policy(),),
    )

    source = report.sources[0]
    assert source.status == "BLOCKED"
    assert source.reason == "SERIES_COVERAGE_INCOMPLETE"
    assert source.expected_series_count == 2
    assert source.observed_series_count == 1


def test_old_source_is_stale_unless_calendar_sla_is_blocked() -> None:
    old = observations(
        ("TEST_SOURCE", "A", NOW - timedelta(days=11)),
        ("TEST_SOURCE", "B", NOW - timedelta(days=11)),
    )
    stale = calculate_health(old, [], as_of=NOW, policies=(policy(),))
    blocked = calculate_health(
        old,
        [calendar_row(expected_at=None, status="BLOCKED_DATE_UNANNOUNCED")],
        as_of=NOW,
        policies=(policy(),),
    )

    assert stale.status == "STALE"
    assert stale.sources[0].reason == "MAX_AGE_EXCEEDED"
    assert blocked.status == "BLOCKED"
    assert blocked.sources[0].reason == "CALENDAR_SLA_BLOCKED"


def test_health_at_historical_as_of_excludes_future_ingest() -> None:
    report = calculate_health(
        observations(
            ("TEST_SOURCE", "A", NOW + timedelta(seconds=1)),
            ("TEST_SOURCE", "B", NOW + timedelta(seconds=1)),
        ),
        [],
        as_of=NOW,
        policies=(policy(),),
    )

    assert report.status == "BLOCKED"
    assert report.sources[0].reason == "NO_OBSERVATION"


def test_health_contracts_fail_closed() -> None:
    with pytest.raises(HealthContractError, match="missing columns"):
        calculate_health(pl.DataFrame(), [], as_of=NOW, policies=(policy(),))
    with pytest.raises(HealthContractError, match="timezone-aware"):
        calculate_health(observations(), [], as_of=NOW.replace(tzinfo=None), policies=(policy(),))
    with pytest.raises(HealthContractError, match="unique"):
        calculate_health(observations(), [], as_of=NOW, policies=(policy(), policy()))
    with pytest.raises(HealthContractError, match="durations"):
        calculate_health(
            observations(),
            [],
            as_of=NOW,
            policies=(
                FreshnessPolicy(
                    source_id="BAD",
                    source="TEST",
                    series_ids=frozenset({"A"}),
                    calendar_prefixes=(),
                    max_age=timedelta(0),
                    release_grace=timedelta(0),
                ),
            ),
        )
    with pytest.raises(HealthContractError, match="expected_at"):
        calculate_health(
            observations(("TEST_SOURCE", "A", NOW), ("TEST_SOURCE", "B", NOW)),
            [calendar_row(expected_at="bad")],  # type: ignore[arg-type]
            as_of=NOW,
            policies=(policy(),),
        )
    with pytest.raises(HealthContractError, match="missing columns"):
        _lake_source_freshness(
            _FrameStore({"test_table": pl.DataFrame()}),  # type: ignore[arg-type]
            as_of=NOW,
            policy=_lake_policy(),
        )
    with pytest.raises(HealthContractError, match="must be a date"):
        _lake_source_freshness(
            _FrameStore(
                {
                    "test_table": pl.DataFrame(
                        {
                            "entity": ["A", "B"],
                            "period": ["bad", "bad"],
                            "ingested_at": [NOW, NOW],
                        }
                    )
                }
            ),  # type: ignore[arg-type]
            as_of=NOW,
            policy=_lake_policy(),
        )


def test_cli_reads_the_lake_and_emits_a_secret_free_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cli_policy = FreshnessPolicy(
        source_id="CLI",
        source="CLI_SOURCE",
        series_ids=frozenset({"CLI:ONE"}),
        calendar_prefixes=("CLI release ",),
        max_age=timedelta(days=1),
        release_grace=timedelta(hours=1),
    )
    store = AppendOnlyParquetStore(tmp_path / "lake")
    store.append(
        "raw_observations",
        [
            {
                "source": "CLI_SOURCE",
                "series_id": "CLI:ONE",
                "obs_period": date(2026, 6, 30),
                "value": 1.0,
                "unit": "Units",
                "release_date": NOW,
                "vintage_date": NOW.date(),
                "ingested_at": NOW,
                "source_url": "https://example.com/source",
                "checksum": "a" * 64,
            }
        ],
    )
    monkeypatch.setattr(health, "default_freshness_policies", lambda: (cli_policy,))
    monkeypatch.setattr(health, "default_lake_freshness_policies", lambda: ())
    monkeypatch.setattr(health, "release_calendar_rows", lambda: [])

    result = health.main(["--lake-root", str(store.root), "--as-of", "2026-08-04T16:00:00Z"])
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["status"] == "GREEN"
    assert output["sources"][0]["source_id"] == "CLI"
    assert "lake" not in output
    with pytest.raises(SystemExit):
        health.build_parser().parse_args(["--as-of", "2026-08-04T16:00:00"])
