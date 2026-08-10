from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import dfri.mts_backtest as mts_backtest_cli
from dfri.ingest.http import HttpReceipt
from dfri.ingest.registry import load_treasury_mts
from dfri.ingest.treasury_mts import (
    TreasuryMtsClient,
    TreasuryMtsError,
    ingest_mts_history,
)
from dfri.lake.store import AppendOnlyParquetStore
from dfri.nowcast.mts import (
    MTS_AR2_VERSION,
    MTS_RANDOM_WALK_VERSION,
    MTS_SEASONAL_VERSION,
    fit_mts_forecast,
    point_forecast,
    run_mts_backtest,
)
from dfri.nowcast.targets import FirstPrintTarget


def http_receipt(content: bytes, url: str, checksum: str) -> HttpReceipt:
    return HttpReceipt(
        content=content,
        source_url=url,
        checksum=checksum,
        retrieved_at=datetime(2026, 8, 10, 17, tzinfo=UTC),
        status_code=200,
    )


def api_payload(period: date, *, outlays: str = "616066757155.07") -> bytes:
    rows = [
        {
            "record_date": period.isoformat(),
            "parent_id": "null",
            "classification_id": "parent-current",
            "classification_desc": "FY 2026",
            "current_month_gross_outly_amt": "null",
            "current_month_dfct_sur_amt": "null",
            "table_nbr": "1",
            "data_type_cd": "S",
            "record_type_cd": "SL",
        },
        {
            "record_date": period.isoformat(),
            "parent_id": "parent-current",
            "classification_id": "detail-current",
            "classification_desc": period.strftime("%B"),
            "current_month_gross_outly_amt": outlays,
            "current_month_dfct_sur_amt": "120305275586.37",
            "table_nbr": "1",
            "data_type_cd": "D",
            "record_type_cd": "MTH",
        },
        {
            "record_date": period.isoformat(),
            "parent_id": "parent-prior",
            "classification_id": "detail-prior",
            "classification_desc": period.strftime("%B"),
            "current_month_gross_outly_amt": "1.00",
            "current_month_dfct_sur_amt": "1.00",
            "table_nbr": "1",
            "data_type_cd": "D",
            "record_type_cd": "MTH",
        },
    ]
    return json.dumps(
        {
            "data": rows,
            "meta": {
                "dataTypes": {
                    "current_month_gross_outly_amt": "CURRENCY",
                    "current_month_dfct_sur_amt": "CURRENCY",
                }
            },
        }
    ).encode()


def metadata_payload() -> bytes:
    return json.dumps(
        {
            "result": {
                "pageContext": {
                    "config": {
                        "datasetId": "015-BFS-2014Q1-13",
                        "name": "Monthly Treasury Statement (MTS)",
                        "apis": [
                            {
                                "endpoint": "v1/accounting/mts/mts_table_1",
                                "tableName": (
                                    "Summary of Receipts, Outlays, and the Deficit/Surplus "
                                    "of the U.S. Government"
                                ),
                            }
                        ],
                    }
                }
            }
        }
    ).encode()


class FakeMtsClient(TreasuryMtsClient):
    def __init__(self, *, outlays: str = "616066757155.07") -> None:
        definition = load_treasury_mts()
        self.definition = replace(
            definition,
            release_schedule={
                date(2026, 6, 30): date(2026, 7, 13),
                date(2026, 7, 31): date(2026, 8, 12),
            },
            unverified_historical_periods=(),
        )
        self.outlays = outlays
        self.issue_calls = 0

    def fetch_metadata(self) -> HttpReceipt:
        return http_receipt(metadata_payload(), self.definition.metadata_url, "a" * 64)

    def fetch_table_1(self, start: date) -> HttpReceipt:
        assert start == date(2026, 6, 30)
        return http_receipt(
            api_payload(start, outlays=self.outlays), self.definition.api_url, "b" * 64
        )

    def fetch_issue(self, period: date) -> HttpReceipt:
        self.issue_calls += 1
        return http_receipt(
            b"%PDF-1.7 test",
            self.definition.archive_url_pattern.format(period=period.strftime("%Y%m")),
            "c" * 64,
        )


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self.issue_content = b"%PDF-1.7 test"

    def get(self, url: str, params: dict[str, object] | None = None) -> HttpReceipt:
        self.calls.append((url, params))
        if url.endswith("page-data.json"):
            content = metadata_payload()
        elif "mts_table_1" in url:
            content = api_payload(date(2026, 6, 30))
        else:
            content = self.issue_content
        return http_receipt(content, url, "d" * 64)


def test_mts_client_uses_pinned_urls_and_rejects_non_pdf_issue() -> None:
    transport = RecordingTransport()
    client = TreasuryMtsClient(transport)  # type: ignore[arg-type]

    client.fetch_metadata()
    client.fetch_table_1(date(2026, 6, 30))
    issue = client.fetch_issue(date(2026, 6, 30))

    assert issue.content.startswith(b"%PDF-")
    assert transport.calls[1][1] == {
        "filter": "record_date:gte:2026-06-30",
        "page[size]": 10000,
    }
    assert transport.calls[2][0].endswith("MonthlyTreasuryStatement_202606.pdf")
    transport.issue_content = b"not a pdf"
    with pytest.raises(TreasuryMtsError, match="is not a PDF"):
        client.fetch_issue(date(2026, 6, 30))


def test_mts_ingest_selects_current_fiscal_year_and_is_idempotent(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    client = FakeMtsClient()

    first = ingest_mts_history(store, client, start=date(2026, 6, 30))
    second = ingest_mts_history(store, client, start=date(2026, 6, 30))

    assert first.appended_periods == 1
    assert second.appended_periods == 0
    assert second.already_present_periods == 1
    assert client.issue_calls == 1
    rows = store.read_table("raw_observations").sort("series_id")
    assert rows.height == 2
    assert rows["value"].to_list() == pytest.approx([120305.27558637, 616066.75715507])
    assert rows["release_date"].to_list() == [
        datetime(2026, 7, 13, 18, tzinfo=UTC),
        datetime(2026, 7, 13, 18, tzinfo=UTC),
    ]
    assert set(rows["source_url"].to_list()) == {
        "https://fiscaldata.treasury.gov/static-data/published-reports/mts/"
        "MonthlyTreasuryStatement_202606.pdf"
    }


def test_mts_ingest_fails_closed_on_unit_or_dataset_drift(tmp_path: Path) -> None:
    client = FakeMtsClient(outlays="not-a-number")
    with pytest.raises(TreasuryMtsError, match="decimal amount"):
        ingest_mts_history(AppendOnlyParquetStore(tmp_path), client, start=date(2026, 6, 30))

    class WrongDataset(FakeMtsClient):
        def fetch_metadata(self) -> HttpReceipt:
            payload = json.loads(metadata_payload())
            payload["result"]["pageContext"]["config"]["datasetId"] = "changed"
            return http_receipt(
                json.dumps(payload).encode(), self.definition.metadata_url, "d" * 64
            )

    with pytest.raises(TreasuryMtsError, match="dataset ID changed"):
        ingest_mts_history(
            AppendOnlyParquetStore(tmp_path / "wrong"),
            WrongDataset(),
            start=date(2026, 6, 30),
        )


def histories() -> dict[str, tuple[FirstPrintTarget, ...]]:
    output: dict[str, tuple[FirstPrintTarget, ...]] = {}
    for series_index, series_id in enumerate(("MTS:DEFICIT.M", "MTS:OUTLAYS.M")):
        rows: list[FirstPrintTarget] = []
        for year in range(2017, 2026):
            for month in range(1, 13):
                if month == 9:
                    continue
                period = month_end(year, month)
                release = datetime.combine(period + timedelta(days=12), datetime.min.time(), UTC)
                rows.append(
                    FirstPrintTarget(
                        target_series=series_id,
                        level_series=series_id,
                        target_period=period,
                        value=100_000.0 * series_index + month * 1_000.0 + (year - 2017) * 10.0,
                        unit="Millions of U.S. Dollars",
                        release_at=release,
                        vintage_date=release.date(),
                        source_url=(
                            "https://fiscaldata.treasury.gov/static-data/published-reports/mts/"
                            f"MonthlyTreasuryStatement_{period.strftime('%Y%m')}.pdf"
                        ),
                        checksum=f"{len(rows) + 1 + series_index:064x}",
                    )
                )
        output[series_id] = tuple(rows)
    return output


def month_end(year: int, month: int) -> date:
    next_year = year + int(month == 12)
    next_month = 1 if month == 12 else month + 1
    return date(next_year, next_month, 1) - date.resolution


def test_mts_backtest_selects_from_three_benchmarks_and_builds_nested_bands() -> None:
    target_histories = histories()
    as_of = datetime(2026, 8, 10, tzinfo=UTC)
    report = run_mts_backtest(target_histories, as_of=as_of)

    assert {item["target_series"] for item in report["targets"]} == {
        "MTS:DEFICIT.M",
        "MTS:OUTLAYS.M",
    }
    for target in report["targets"]:
        assert {item["model_version"] for item in target["metrics"]} == {
            MTS_RANDOM_WALK_VERSION,
            MTS_SEASONAL_VERSION,
            MTS_AR2_VERSION,
        }
        selected = next(
            item
            for item in target["metrics"]
            if item["model_version"] == target["selected_model_version"]
        )
        assert selected["interval_observations"] > 0
        assert 0 <= selected["coverage80"] <= 1
        assert selected["coverage80"] <= selected["coverage95"] <= 1
    history = target_histories["MTS:DEFICIT.M"]
    target_period = month_end(2026, 1)
    forecast = fit_mts_forecast(
        history,
        target_period=target_period,
        made_at=as_of,
        backtest=report,
    )
    assert forecast.low95 <= forecast.low80 <= forecast.point <= forecast.high80 <= forecast.high95
    assert len(forecast.inputs_hash) == 64
    assert point_forecast(MTS_SEASONAL_VERSION, history, target_period) is not None


def test_mts_model_rejects_unknown_benchmark() -> None:
    history = histories()["MTS:DEFICIT.M"]
    with pytest.raises(Exception, match="Unsupported MTS benchmark"):
        point_forecast("not-a-model", history, month_end(2026, 1))


def test_mts_backtest_cli_build_and_atomic_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target_histories = histories()
    as_of = datetime(2026, 8, 10, tzinfo=UTC)
    monkeypatch.setattr(
        mts_backtest_cli,
        "read_mts_first_print_targets",
        lambda _guard, series_id, _as_of, start: target_histories[series_id],
    )
    report = mts_backtest_cli.build_mts_backtest(tmp_path / "lake", as_of=as_of)
    assert report["targets"]

    output = tmp_path / "reports" / "mts.json"
    monkeypatch.setattr(
        sys, "argv", ["dfri-mts-backtest", "--as-of", as_of.isoformat(), "--output", str(output)]
    )
    monkeypatch.setattr(mts_backtest_cli, "build_mts_backtest", lambda _lake, as_of: report)
    mts_backtest_cli.main()
    first = output.read_bytes()
    mts_backtest_cli.main()

    assert output.read_bytes() == first
    assert (
        json.loads(capsys.readouterr().out.splitlines()[0])["report_hash"] == report["report_hash"]
    )
    with pytest.raises(argparse.ArgumentTypeError, match="timezone"):
        mts_backtest_cli._as_of("2026-08-10T12:00:00")
