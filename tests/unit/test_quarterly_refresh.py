from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dfri.attribution.registry import AttributionBundle, load_attribution_bundle
from dfri.ingest.board_targets import DERIVED_SOURCE
from dfri.ingest.edgar import EdgarJsonReceipt
from dfri.lake.store import AppendOnlyParquetStore
from dfri.ops import quarterly_refresh as qr
from dfri.ops.quarterly_refresh import (
    CompanyRefreshInput,
    QuarterlyFlows,
    QuarterlyRefreshError,
    QuarterlyRefreshLedger,
    QuarterlyRefreshRecord,
    build_refresh_record,
    latest_complete_quarter,
    load_refresh_report,
    select_company_refresh,
)


def baseline_inputs(bundle: AttributionBundle) -> tuple[CompanyRefreshInput, ...]:
    return tuple(
        CompanyRefreshInput(
            ticker=company.ticker,
            status="BASELINE_NO_NEW_10Q",
            period=date.fromisoformat(company.period),
            revenue_total_millions=company.revenue_total_millions,
            revenue_source_url=company.revenue_source_url,
            annual_value_millions=company.revenue_total_millions,
            current_ytd_millions=None,
            prior_ytd_millions=None,
            annual_accession=f"test-{company.ticker}",
            annual_filed_at=date.fromisoformat(company.period),
            quarterly_accession=None,
            quarterly_filed_at=None,
            revenue_tag=company.revenue_tag,
            selected_facts_hash="a" * 64,
        )
        for company in bundle.companies
    )


def raw_row(series: str, period: date, value: float, released: datetime) -> dict[str, object]:
    return {
        "source": DERIVED_SOURCE,
        "series_id": series,
        "obs_period": period,
        "value": value,
        "unit": "Millions of U.S. Dollars",
        "release_date": released,
        "vintage_date": released.date(),
        "ingested_at": released,
        "source_url": f"https://www.federalreserve.gov/releases/g19/{released:%Y%m%d}/",
        "checksum": f"{period.month:064x}",
    }


def test_latest_complete_quarter_ignores_incomplete_newer_quarter(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    rows: list[dict[str, object]] = []
    for month, release_month in ((1, 3), (2, 4), (3, 5)):
        released = datetime(2026, release_month, 7, 19, tzinfo=UTC)
        for series, value in (("DELTA_DTCTLR.M", 10.0), ("DELTA_DTCTLN.M", 20.0)):
            rows.append(raw_row(series, date(2026, month, 28), value, released))
    for series, value in (("DELTA_DTCTLR.M", 1.0), ("DELTA_DTCTLN.M", 2.0)):
        rows.append(raw_row(series, date(2026, 4, 30), value, datetime(2026, 6, 5, 19, tzinfo=UTC)))
    store.append("raw_observations", rows)

    selected = latest_complete_quarter(store, as_of=datetime(2026, 8, 5, 12, tzinfo=UTC))

    assert selected.quarter == "2026-Q1"
    assert {item.debt_product: item.prior.mid for item in selected.inputs} == {
        "revolving_credit": 30.0,
        "nonrevolving_credit": 60.0,
    }
    assert selected.data_vintage == datetime(2026, 5, 7, 19, tzinfo=UTC)


def test_latest_complete_quarter_fails_closed_without_a_complete_vintage(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    with pytest.raises(QuarterlyRefreshError, match="complete three-month"):
        latest_complete_quarter(store, as_of=datetime(2026, 8, 5, tzinfo=UTC))
    with pytest.raises(QuarterlyRefreshError, match="timezone-aware"):
        latest_complete_quarter(store, as_of=datetime(2026, 8, 5))  # noqa: DTZ001


def test_company_refresh_derives_ttm_from_real_filing_shape() -> None:
    company = next(item for item in load_attribution_bundle().companies if item.ticker == "ABNB")
    submissions = EdgarJsonReceipt(
        kind="submissions",
        source_url="https://data.sec.gov/submissions/CIK0001559720.json",
        checksum="a" * 64,
        retrieved_at="2026-08-05T12:00:00+00:00",
        payload={
            "cik": 1559720,
            "name": "Airbnb, Inc.",
            "filings": {
                "recent": {
                    "form": ["10-Q", "10-K"],
                    "filingDate": ["2026-05-07", "2026-02-12"],
                    "reportDate": ["2026-03-31", "2025-12-31"],
                    "accessionNumber": ["0001559720-26-000014", "0001559720-26-000004"],
                    "primaryDocument": ["abnb-20260331.htm", "abnb-20251231.htm"],
                }
            },
        },
    )
    facts = EdgarJsonReceipt(
        kind="companyfacts",
        source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0001559720.json",
        checksum="b" * 64,
        retrieved_at="2026-08-05T12:00:00+00:00",
        payload={
            "cik": 1559720,
            "entityName": "Airbnb, Inc.",
            "facts": {
                "us-gaap": {
                    company.revenue_tag: {
                        "units": {
                            "USD": [
                                {
                                    "start": "2025-01-01",
                                    "end": "2025-12-31",
                                    "filed": "2026-02-12",
                                    "form": "10-K",
                                    "fp": "FY",
                                    "accn": "0001559720-26-000004",
                                    "val": 100_000_000,
                                },
                                {
                                    "start": "2026-01-01",
                                    "end": "2026-03-31",
                                    "filed": "2026-05-07",
                                    "form": "10-Q",
                                    "fp": "Q1",
                                    "accn": "0001559720-26-000014",
                                    "val": 30_000_000,
                                },
                                {
                                    "start": "2025-01-01",
                                    "end": "2025-03-31",
                                    "filed": "2026-05-07",
                                    "form": "10-Q",
                                    "fp": "Q1",
                                    "accn": "0001559720-26-000014",
                                    "val": 20_000_000,
                                },
                            ]
                        }
                    }
                }
            },
        },
    )

    selected = select_company_refresh(
        company,
        submissions,
        facts,
        as_of=datetime(2026, 8, 5, 12, tzinfo=UTC),
        quarter_end=date(2026, 3, 31),
    )

    assert selected.status == "UPDATED_TTM_FROM_10Q"
    assert selected.revenue_total_millions == 110
    assert selected.current_ytd_millions == 30
    assert selected.prior_ytd_millions == 20
    assert selected.quarterly_accession == "0001559720-26-000014"
    assert selected.revenue_source_url.endswith("/abnb-20260331.htm")


def test_build_refresh_record_reweights_all_fifty_and_is_deterministic() -> None:
    bundle = load_attribution_bundle()
    flow_vintage = datetime.fromisoformat(bundle.data_vintage)
    flows = QuarterlyFlows("2026-Q1", flow_vintage, bundle.flows)
    inputs = baseline_inputs(bundle)

    first = build_refresh_record(bundle, flows, inputs)
    second = build_refresh_record(bundle, flows, inputs)

    assert first == second
    assert first.company_count == 50
    assert first.updated_company_count == 0
    payload = first.payload()
    assert len(payload["result"]["companies"]) == 50
    assert payload["result"]["aggregate"]["weighting"] == "revenue-weighted"
    identity = {
        "methodology_version": first.methodology_version,
        "source_hash": first.source_hash,
        "target_quarter": first.target_quarter,
    }
    encoded_identity = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    expected = "qrf_" + hashlib.sha256(encoded_identity).hexdigest()[:24]
    assert first.refresh_id == expected


def test_build_refresh_record_rejects_incomplete_or_different_coverage() -> None:
    bundle = load_attribution_bundle()
    flows = QuarterlyFlows("2026-Q1", datetime.fromisoformat(bundle.data_vintage), bundle.flows)
    inputs = baseline_inputs(bundle)

    with pytest.raises(QuarterlyRefreshError, match="exactly 50 unique"):
        build_refresh_record(bundle, flows, inputs[:-1])
    with pytest.raises(QuarterlyRefreshError, match="coverage differs"):
        build_refresh_record(bundle, flows, (*inputs[:-1], replace(inputs[-1], ticker="OTHER")))


def test_refresh_ledger_is_append_only_idempotent_and_conflict_checked(tmp_path: Path) -> None:
    ledger = QuarterlyRefreshLedger(AppendOnlyParquetStore(tmp_path))
    payload = {
        "refresh_id": "qrf_test",
        "company_count": 50,
        "result": {"companies": []},
    }
    record = QuarterlyRefreshRecord(
        refresh_id="qrf_test",
        target_quarter="2026-Q1",
        effective_at=datetime(2026, 5, 7, 19, tzinfo=UTC),
        data_vintage=datetime(2026, 5, 7, 19, tzinfo=UTC),
        methodology_version="1.1.0",
        source_hash="a" * 64,
        company_count=50,
        updated_company_count=20,
        payload_json=json.dumps(payload, sort_keys=True),
    )

    assert ledger.append(record).appended is True
    assert ledger.append(record).appended is False
    assert ledger.read_all() == (record,)
    semantically_identical = replace(
        record,
        refresh_id="qrf_cross_platform",
        payload_json=json.dumps(
            {
                **payload,
                "refresh_id": "qrf_cross_platform",
                "result": {"companies": [], "platform_epsilon": 1e-16},
            },
            sort_keys=True,
        ),
    )
    deduplicated = ledger.append(semantically_identical)
    assert deduplicated.appended is False
    assert deduplicated.refresh_id == record.refresh_id
    assert ledger.read_all() == (record,)
    with pytest.raises(QuarterlyRefreshError, match="different content"):
        ledger.append(replace(record, source_hash="b" * 64))

    with pytest.raises(QuarterlyRefreshError, match="conflicting metadata"):
        ledger.append(replace(record, refresh_id="qrf_bad_metadata", company_count=49))


def test_committed_live_refresh_report_is_complete_and_loadable() -> None:
    root = Path(__file__).parents[2]
    record = load_refresh_report(root / "reports" / "M5_QUARTERLY_REFRESH_DEMO.json")

    assert record.target_quarter == "2026-Q1"
    assert record.company_count == 50
    assert record.updated_company_count == 35
    assert record.source_hash == "77e05082429325e1906786a7d02846f7307c5bfcb7f9cd3f354a2670b7a323ef"


def test_refresh_report_validation_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(QuarterlyRefreshError, match="Cannot load"):
        load_refresh_report(tmp_path / "missing.json")

    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")
    with pytest.raises(QuarterlyRefreshError, match="must be an object"):
        load_refresh_report(invalid)

    invalid.write_text('{"schema_version":"v1"}', encoding="utf-8")
    with pytest.raises(QuarterlyRefreshError, match="fields are incomplete"):
        load_refresh_report(invalid)

    source = Path(__file__).parents[2] / "reports" / "M5_QUARTERLY_REFRESH_DEMO.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["result"]["companies"] = []
    invalid.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(QuarterlyRefreshError, match="50 company results"):
        load_refresh_report(invalid)


def test_refresh_rejects_wrong_sec_identities_before_fact_selection() -> None:
    company = load_attribution_bundle().companies[0]

    def receipt(kind: str, cik: object) -> EdgarJsonReceipt:
        return EdgarJsonReceipt(
            kind=kind,
            source_url="https://data.sec.gov/example.json",
            checksum="a" * 64,
            retrieved_at="2026-08-05T12:00:00+00:00",
            payload={"cik": cik},
        )

    with pytest.raises(QuarterlyRefreshError, match="submissions CIK differs"):
        select_company_refresh(
            company,
            receipt("submissions", 0),
            receipt("companyfacts", company.cik),
            as_of=datetime(2026, 8, 5, tzinfo=UTC),
            quarter_end=date(2026, 3, 31),
        )
    with pytest.raises(QuarterlyRefreshError, match="companyfacts CIK differs"):
        select_company_refresh(
            company,
            receipt("submissions", company.cik),
            receipt("companyfacts", 0),
            as_of=datetime(2026, 8, 5, tzinfo=UTC),
            quarter_end=date(2026, 3, 31),
        )


def test_quarterly_refresh_cli_writes_a_report_and_machine_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    committed = Path(__file__).parents[2] / "reports" / "M5_QUARTERLY_REFRESH_DEMO.json"
    record = load_refresh_report(committed)
    monkeypatch.setattr(
        qr,
        "run_live_refresh",
        lambda *_args, **_kwargs: (
            record,
            qr.QuarterlyRefreshAppend(record.refresh_id, False, None),
        ),
    )
    output = tmp_path / "refresh.json"

    assert (
        qr.main(
            (
                "--raw-lake-root",
                str(tmp_path / "raw"),
                "--ledger-root",
                str(tmp_path / "curated"),
                "--as-of",
                "2026-08-05T12:00:00Z",
                "--output",
                str(output),
            )
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "PASS"
    assert summary["appended"] == 0
    assert summary["effective_at"] == record.effective_at.isoformat()
    assert load_refresh_report(output) == record
