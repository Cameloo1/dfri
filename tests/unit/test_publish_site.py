from __future__ import annotations

import html as html_lib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import dfri.attribution.resilience as resilience_module
import dfri.publish.site as site_module
from dfri.attribution.engine import run_attribution
from dfri.attribution.registry import load_attribution_bundle
from dfri.lake.store import AppendOnlyParquetStore
from dfri.nowcast.bridge import BridgeForecast
from dfri.nowcast.targets import FirstPrintTarget
from dfri.ops.quarterly_refresh import QuarterlyRefreshLedger, load_refresh_report
from dfri.publish.ledger import GradeLedger, PredictionLedger
from dfri.publish.site import SitePublishError, publish_scoreboard

PUBLISHED_AT = datetime(2026, 8, 5, 5, 0, tzinfo=UTC)
DATA_VINTAGE = datetime(2026, 7, 31, 20, 15, tzinfo=UTC)
TIER_LEGEND_COPY = (
    "T1 — Observed",
    "A company disclosure directly links financing to sales.",
    "T2 — Category-mapped",
    "Credit is mapped to spending categories, then to companies using registered weights.",
    "T3 — Fungible",
    (
        "Credit that cannot be assigned directly is allocated broadly by estimated consumer "
        "revenue, with the widest uncertainty."
    ),
    "Tier percentages show how the estimate was constructed—not confidence scores.",
)


def seed(store: AppendOnlyParquetStore) -> tuple[str, str]:
    periods = [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
        date(2026, 5, 31),
        date(2026, 6, 30),
    ]
    releases = [
        datetime(2026, 2, 7, 19, tzinfo=UTC),
        datetime(2026, 3, 7, 19, tzinfo=UTC),
        datetime(2026, 4, 7, 19, tzinfo=UTC),
        datetime(2026, 5, 7, 19, tzinfo=UTC),
        datetime(2026, 6, 7, 19, tzinfo=UTC),
        datetime(2026, 8, 4, 20, 30, tzinfo=UTC),
    ]
    store.append(
        "raw_observations",
        [
            {
                "source": "DFRI_DERIVED_BOARD_FIRST_PRINT_V1",
                "series_id": "DELTA_DTCTLR.M",
                "obs_period": period,
                "value": value,
                "unit": "Millions of U.S. Dollars",
                "release_date": release,
                "vintage_date": release.date(),
                "ingested_at": release,
                "source_url": (f"https://www.federalreserve.gov/releases/g19/{release:%Y%m%d}/"),
                "checksum": f"{index:064x}",
            }
            for index, (period, release, value) in enumerate(
                zip(
                    periods,
                    releases,
                    [1_000.0, 3_000.0, -2_000.0, 4_500.0, 500.0, 10_500.0],
                    strict=True,
                )
            )
        ],
    )
    first = BridgeForecast(
        model_version="bridge-ridge-v2-alpha10",
        target_series="DELTA_DTCTLR.M",
        target_period=date(2026, 6, 30),
        made_at=datetime(2026, 8, 4, 20, 0, tzinfo=UTC),
        point=-5_529.0,
        low80=-13_243.0,
        high80=2_184.0,
        low95=-17_327.0,
        high95=6_268.0,
        training_observations=137,
        inputs_hash="a" * 64,
    )
    second = BridgeForecast(
        model_version="bridge-ridge-v2-alpha10",
        target_series="DELTA_DTCTLN.M",
        target_period=date(2026, 7, 31),
        made_at=datetime(2026, 8, 4, 20, 5, tzinfo=UTC),
        point=20_000.0,
        low80=17_000.0,
        high80=23_000.0,
        low95=15_000.0,
        high95=25_000.0,
        training_observations=137,
        inputs_hash="b" * 64,
    )
    predictions = PredictionLedger(store)
    first_id = predictions.append(first).record_id
    second_id = predictions.append(second).record_id
    record = next(item for item in predictions.read_all() if item.prediction_id == first_id)
    target = FirstPrintTarget(
        target_series="DELTA_DTCTLR.M",
        level_series="DTCTLR.M",
        target_period=date(2026, 6, 30),
        value=10_500.0,
        unit="Millions of U.S. Dollars",
        release_at=datetime(2026, 8, 4, 20, 30, tzinfo=UTC),
        vintage_date=date(2026, 8, 4),
        source_url="https://www.federalreserve.gov/releases/g19/20260804/",
        checksum="c" * 64,
    )
    GradeLedger(store).append(record, target)
    return first_id, second_id


def test_publish_builds_stable_feeds_pages_permalinks_and_manifest(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path / "ledger")
    first_id, second_id = seed(store)
    output = tmp_path / "published"
    project = Path(__file__).parents[2]

    first = publish_scoreboard(
        store,
        output,
        published_at=PUBLISHED_AT,
        data_vintage=DATA_VINTAGE,
        publication_mode="preview",
        project_root=project,
    )
    before = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }
    second = publish_scoreboard(
        store,
        output,
        published_at=PUBLISHED_AT,
        data_vintage=DATA_VINTAGE,
        publication_mode="preview",
        project_root=project,
    )
    after = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }

    assert first == second
    assert before == after
    assert first.prediction_count == 2
    assert first.graded_count == 1
    assert (output / "scoreboard" / "predictions" / first_id / "index.html").exists()
    assert (output / "scoreboard" / "predictions" / second_id / "index.html").exists()
    scoreboard = (output / "scoreboard" / "index.html").read_text(encoding="utf-8")
    home = (output / "index.html").read_text(encoding="utf-8")
    assert "<h1>Predictions</h1>" in scoreboard
    assert "monthly U.S. consumer borrowing" in scoreboard
    assert "seasonally adjusted flows in millions of U.S. dollars" in scoreboard
    assert "Point estimate ($M)" in scoreboard
    assert "80% band ($M)" in scoreboard
    assert "First print ($M)" in scoreboard
    assert 'data-sort-column="0"' in scoreboard
    assert '<button class="sort-button"' not in scoreboard
    assert first_id in scoreboard and second_id in scoreboard
    assert "Not released" in scoreboard
    assert "10,500" in scoreboard
    assert "Live calibration" in scoreboard
    assert "First revolving miss" in scoreboard
    assert "missed the sign and fell outside its 80% interval" in scoreboard
    assert "statistically uninformative" in scoreboard
    assert "has not been retrained or retuned" in scoreboard
    assert "Research and educational content. Not investment advice." in home
    assert 'href="https://creativecommons.org/licenses/by-nc/4.0/"' in home
    assert 'href="mailto:ops@camelon.app"' in home
    assert "Revenue-weighted DFR%" in home
    assert "each month's change in U.S. consumer borrowing" in home
    assert "Seasonally adjusted · millions of U.S. dollars" in home
    assert "tier-explainer-visible" in home
    assert all(item in home for item in TIER_LEGEND_COPY)
    assert "Evidence Lift measures company-specific financing evidence" in home
    assert "No company-specific financing evidence found" in home
    assert "Carvana has the highest Evidence Lift" in home
    prediction = (output / "scoreboard" / "predictions" / first_id / "index.html").read_text(
        encoding="utf-8"
    )
    assert "seasonally adjusted monthly flow · $M" in prediction
    assert "<dt>Model version</dt>" in prediction
    assert "<dt>Input sources</dt>" in prediction
    assert "https://www.federalreserve.gov/releases/h8/" in prediction
    assert "https://www.census.gov/retail/marts/historic_releases.html" in prediction
    assert "https://www.federalreserve.gov/releases/g19/" in prediction
    methodology = (output / "methodology" / "index.html").read_text(encoding="utf-8")
    assert "https://www.federalreserve.gov/releases/h8/" in methodology
    assert "https://www.census.gov/retail/marts/historic_releases.html" in methodology
    assert "https://www.federalreserve.gov/releases/g19/" in methodology
    assert "the nominal 80% band contained 71.3%" in methodology
    site_js = (output / "assets" / "site.js").read_text(encoding="utf-8")
    assert 'document.createElement("button")' in site_js
    # The tree-wide cap guards runaway duplication; the product's hard 500 KB budget is checked
    # per rendered page below. MTS adds two targets and per-series calibration metadata, while
    # deterministic metadata-only social previews add one compact image per stable URL.
    assert first.total_bytes < 1_800_000
    assert all(
        line == line.lstrip()
        for line in (output / "index.html").read_text(encoding="utf-8").splitlines()
    )
    assert b"\r\n" not in (output / "assets" / "site.css").read_bytes()
    assert b"\r\n" not in (output / "assets" / "site.js").read_bytes()

    filtered = publish_scoreboard(
        store,
        output,
        published_at=PUBLISHED_AT,
        data_vintage=DATA_VINTAGE,
        publication_mode="preview",
        minimum_made_at=datetime(2026, 8, 4, 20, 1, tzinfo=UTC),
        project_root=project,
    )
    assert filtered.prediction_count == 1
    assert not (output / "scoreboard" / "predictions" / first_id).exists()
    assert (output / "scoreboard" / "predictions" / second_id / "index.html").exists()


def test_feed_contract_has_publication_fields_license_and_typed_parquet(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path / "ledger")
    seed(store)
    output = tmp_path / "published"
    publish_scoreboard(
        store,
        output,
        published_at=PUBLISHED_AT,
        data_vintage=DATA_VINTAGE,
        publication_mode="live",
        project_root=Path(__file__).parents[2],
    )

    payload = json.loads((output / "v1" / "feeds" / "scoreboard.json").read_text())
    assert payload["meta"]["license"].startswith("CC BY-NC 4.0")
    assert payload["meta"]["license_url"] == "https://creativecommons.org/licenses/by-nc/4.0/"
    assert payload["meta"]["commercial_license_contact"] == "ops@camelon.app"
    assert payload["meta"]["publication_mode"] == "live"
    assert payload["meta"]["live_calibration"]["scope"] == "live_grades_only"
    assert payload["meta"]["live_calibration"]["graded_count"] == 1
    assert payload["meta"]["live_calibration"]["coverage80"] == 0.0
    assert payload["meta"]["live_calibration"]["coverage95"] == 0.0
    assert payload["meta"]["live_calibration"]["mae"] == pytest.approx(16_029.0)
    assert all(
        {
            "methodology_version",
            "data_vintage",
            "published_at",
            "license",
            "license_url",
            "commercial_license_contact",
        }
        <= set(row)
        for row in payload["data"]
    )
    assert all(row["status"] == row["grade_status"] for row in payload["data"])
    assert {row["status"] for row in payload["data"]} == {
        "GRADED",
        "PENDING_FIRST_PRINT",
    }
    schema = json.loads((output / "v1" / "feeds" / "schema.json").read_text())
    assert schema["feeds"]["scoreboard"]["invariants"] == [
        {
            "fields": ["status", "grade_status"],
            "rule": "status equals grade_status for every scoreboard row",
        }
    ]
    assert "live grades" in schema["feeds"]["scoreboard"]["metadata"]["live_calibration"]
    parquet = pq.read_table(output / "v1" / "feeds" / "nowcast_predictions.parquet")
    assert parquet.num_rows == 2
    assert parquet.schema.field("point").type == pa.float64()
    later = PUBLISHED_AT + timedelta(days=1)
    later_vintage = DATA_VINTAGE + timedelta(days=1)
    publish_scoreboard(
        store,
        output,
        published_at=later,
        data_vintage=later_vintage,
        publication_mode="live",
        project_root=Path(__file__).parents[2],
    )
    rebuilt = json.loads((output / "v1" / "feeds" / "scoreboard.json").read_text())
    assert rebuilt["meta"]["published_at"] == later.isoformat()
    assert all(row["published_at"] == PUBLISHED_AT.isoformat() for row in rebuilt["data"])
    assert all(row["data_vintage"] == DATA_VINTAGE.isoformat() for row in rebuilt["data"])
    company_rows = json.loads((output / "v1" / "feeds" / "dfri_companies.json").read_text())["data"]
    assumption_rows = json.loads((output / "v1" / "feeds" / "assumptions.json").read_text())["data"]
    assert {row["published_at"] for row in company_rows} == {"2026-08-06T05:40:24.524787+00:00"}
    assert {row["published_at"] for row in assumption_rows} == {"2026-08-06T05:40:24.524787+00:00"}
    assert store.read_table("publication_records").height == 2


def test_source_fallback_is_visible_and_published_with_the_effective_band(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = resilience_module.load_source_registry()
    sources["ffiec_call_reports"] = replace(sources["ffiec_call_reports"], available=False)
    monkeypatch.setattr(resilience_module, "load_source_registry", lambda: sources)
    store = AppendOnlyParquetStore(tmp_path / "ledger")
    seed(store)
    output = tmp_path / "published"

    publish_scoreboard(
        store,
        output,
        published_at=PUBLISHED_AT,
        data_vintage=DATA_VINTAGE,
        publication_mode="preview",
        project_root=Path(__file__).parents[2],
    )

    home = (output / "index.html").read_text(encoding="utf-8")
    companies = json.loads((output / "v2" / "feeds" / "dfri_companies.json").read_text())
    assert "Source fallback active." in home
    assert "uses ncua_call_reports" in home
    assert companies["meta"]["source_status"] == "DEGRADED"
    assert companies["meta"]["source_degradation_count"] == 1
    assert companies["meta"]["source_degradations"][0]["active_source_id"] == ("ncua_call_reports")
    assert companies["meta"]["source_degradations"][0]["effective_prior_low"] < 0.1
    assert companies["meta"]["source_degradations"][0]["effective_prior_high"] > 0.23


def test_attribution_feeds_and_fifty_company_pages_publish_with_full_evidence(
    tmp_path: Path,
) -> None:
    store = AppendOnlyParquetStore(tmp_path / "ledger")
    seed(store)
    demo = load_refresh_report(
        Path(__file__).parents[2] / "reports" / "M5_QUARTERLY_REFRESH_DEMO.json"
    )
    runtime_payload = demo.payload()
    runtime_payload["refresh_id"] = "qrf_runtime_authoritative"
    runtime = replace(
        demo,
        refresh_id="qrf_runtime_authoritative",
        payload_json=json.dumps(runtime_payload, sort_keys=True, separators=(",", ":")),
    )
    QuarterlyRefreshLedger(store).append(runtime)
    output = tmp_path / "published"
    publish_scoreboard(
        store,
        output,
        published_at=PUBLISHED_AT,
        data_vintage=DATA_VINTAGE,
        publication_mode="live",
        project_root=Path(__file__).parents[2],
    )

    companies = json.loads((output / "v1" / "feeds" / "dfri_companies.json").read_text())
    assumptions = json.loads((output / "v1" / "feeds" / "assumptions.json").read_text())
    schema = json.loads((output / "v1" / "feeds" / "schema.json").read_text())
    companies_v2 = json.loads((output / "v2" / "feeds" / "dfri_companies.json").read_text())
    assumptions_v2 = json.loads((output / "v2" / "feeds" / "assumptions.json").read_text())
    schema_v2 = json.loads((output / "v2" / "feeds" / "schema.json").read_text())
    assert len(companies["data"]) == 50
    assert assumptions["data"]
    assert companies["meta"]["weighting"] == "revenue-weighted"
    assert set(schema["feeds"]) == {
        "nowcast_predictions",
        "scoreboard",
        "dfri_companies",
        "assumptions",
        "coverage_exclusions",
        "quarterly_refreshes",
        "dfri_company_history",
    }
    assert schema_v2["schema_version"] == "v2"
    assert schema_v2["predecessor_schema_url"] == "/v1/feeds/schema.json"
    assert set(schema_v2["feeds"]) == {"dfri_companies", "assumptions"}
    assert companies_v2["meta"]["evidence_lift_headline"].startswith("Carvana ")
    assert len(companies_v2["data"]) == 50
    assert all(
        {
            "fungibility_baseline_dfr_pct_mid",
            "evidence_lift",
            "evidence_lift_status",
            "evidence_lift_headline",
        }
        <= set(row)
        for row in companies_v2["data"]
    )
    assert all("evidence_lift" not in row for row in companies["data"])
    assert all("review_status" not in row for row in assumptions["data"])
    tjx_assumption = next(
        row for row in assumptions_v2["data"] if row["assumption_id"] == "A-T1-TJX-SYF-001"
    )
    assert tjx_assumption["review_status"] == "APPROVED"
    parquet = pq.read_table(output / "v1" / "feeds" / "dfri_companies.parquet")
    assert parquet.num_rows == 50
    assert parquet.schema.field("estimated_dfr_pct_mid").type == pa.float64()
    parquet_v2 = pq.read_table(output / "v2" / "feeds" / "dfri_companies.parquet")
    assert parquet_v2.num_rows == 50
    assert parquet_v2.schema.field("evidence_lift").type == pa.float64()
    methodology = (output / "methodology" / "index.html").read_text(encoding="utf-8")
    assert "Assumption Registry" in methodology
    assert "Matrix A has" in methodology
    assert "<h2>Tier 1</h2><p>Observed:" in methodology
    assert 'id="credit-flow"' in methodology
    assert "does not enter the nowcast" in methodology
    assert "Exact static values behind the diagram." in methodology
    assert "This build has 11 critical assumptions and 0 critical assumptions" in methodology
    assert "FFIEC bank, NCUA credit-union" in methodology
    assert 'id="mts-clock"' in methodology
    home = (output / "index.html").read_text(encoding="utf-8")
    assert "Revenue-weighted DFR%" in home
    assert "range-chart" in home
    assert '<rect x="108" y="23" width="384" height="26" class="range-band"' in home
    assert 'class="range-mid-rule"' in home
    assert 'id="evidence-lift"' in home
    assert 'id="credit-flow"' in home
    assert 'data-node-count="9"' in home
    assert "Width = estimated dollars" in home
    assert "Style = how much is actually known" in home
    assert "Tier 3 flows are proportional allocations, not observed transfers." in home
    assert all(f'class="flow-ribbon tier-{tier}"' in home for tier in (1, 2, 3))
    assert "evidence, not risk, credit quality, or investment merit" in home
    assert (output / "changelog" / "index.html").exists()
    assert (output / "methodology" / "sensitivity" / "index.html").exists()
    assert (output / "methodology" / "coverage" / "index.html").exists()
    exclusions = json.loads((output / "v1" / "feeds" / "coverage_exclusions.json").read_text())
    assert len(exclusions["data"]) == 31
    refreshes = json.loads((output / "v1" / "feeds" / "quarterly_refreshes.json").read_text())
    history = json.loads((output / "v1" / "feeds" / "dfri_company_history.json").read_text())
    assert len(refreshes["data"]) == 1
    assert refreshes["data"][0]["refresh_id"] == "qrf_runtime_authoritative"
    assert len(history["data"]) == 50

    v2_by_ticker = {row["ticker"]: row for row in companies_v2["data"]}
    for row in companies["data"]:
        page = output / "companies" / row["ticker"].lower() / "index.html"
        html = page.read_text(encoding="utf-8")
        assert "estimated DFR% of U.S. consumer revenue" in html
        assert "<h2>Assumptions</h2>" in html
        assert "<h2>Sensitivity</h2>" in html
        assert "<h2>History</h2>" in html
        assert 'class="figure-evidence"' in html
        assert 'class="range-band"' in html
        assert 'class="range-mid-rule"' in html
        if row["tier1_source_url"]:
            assert row["tier1_excerpt"] in html_lib.unescape(html)
            assert row["tier1_source_url"] in html
        else:
            assert "No company-specific observed financing line" in html
        assert "Tier 1" in html and "Tier 2" in html and "Tier 3" in html
        assert "Evidence Lift" in html
        if v2_by_ticker[row["ticker"]]["evidence_lift_status"] == "baseline-only":
            assert "No company-specific financing evidence found" in html
        assert '<details class="tier-explainer">' in html
        assert "<summary>Tier definitions</summary>" in html
        assert all(item in html for item in TIER_LEGEND_COPY)
        assert page.stat().st_size < 500_000

    cvna = v2_by_ticker["CVNA"]
    assert cvna["evidence_lift"] > 10
    assert 15 < cvna["estimated_dfr_pct_mid"] < 22
    assert cvna["tier1_share"] > 0.5

    prediction = next((output / "scoreboard" / "predictions").glob("*/index.html"))
    prediction_html = prediction.read_text(encoding="utf-8")
    assert 'class="card record-hero"' in prediction_html
    assert '<rect x="108" y="23" width="384" height="26" class="range-band"' in prediction_html


def test_credit_flow_widths_are_linear_tiered_and_capped() -> None:
    bundle = load_attribution_bundle()
    view = site_module._credit_flow_view(bundle, run_attribution(bundle))

    assert view.node_count <= 9
    assert len(view.product_nodes) == 2
    assert len(view.category_nodes) <= 4
    assert [item.key for item in view.company_nodes[:2]] == ["CVNA", "GM"]
    assert view.company_nodes[-1].key == "all-other"
    links = (*view.product_links, *view.company_links)
    assert {item.tier for item in links} == {1, 2, 3}
    scale = float(links[0].stroke_width) / links[0].amount_millions
    assert all(
        float(item.stroke_width) / item.amount_millions == pytest.approx(scale, rel=0.01)
        for item in links
    )
    assert all(item.amount_millions > 0 for item in links)


def test_home_information_architecture_preserves_all_company_evidence_and_semantics(
    tmp_path: Path,
) -> None:
    store = AppendOnlyParquetStore(tmp_path / "ledger")
    seed(store)
    output = tmp_path / "published"
    publish_scoreboard(
        store,
        output,
        published_at=PUBLISHED_AT,
        data_vintage=DATA_VINTAGE,
        publication_mode="preview",
        project_root=Path(__file__).parents[2],
    )

    home = (output / "index.html").read_text(encoding="utf-8")
    section_ids = [
        'id="current-estimate"',
        'id="prediction-ledger"',
        'id="model-record"',
        'id="evidence-tiers"',
        'id="credit-flow"',
        'id="evidence-lift"',
        'id="companies"',
    ]
    assert [home.index(section_id) for section_id in section_ids] == sorted(
        home.index(section_id) for section_id in section_ids
    )
    assert '<div class="opening-spread">' in home
    assert 'class="section-block prediction-rail"' in home
    rail_items = [
        'data-figure="prediction"',
        'data-figure="count"',
        'id="model-record"',
        'data-figure="diagnostic-usd"',
        'data-figure="diagnostic-percent"',
        'class="calibration-ledger home-calibration opening-calibration"',
    ]
    rail_positions = [home.index(item) for item in rail_items]
    assert rail_positions == sorted(rail_positions)
    assert home.count("Live ledger · prediction records") == 1
    assert home.count("2018\u20132026 · $M mean absolute error") == 1
    assert home.count("2018\u20132026 · share inside 80% band") == 1
    assert "Historical tests compare the nowcast" in home
    assert "they are not live-grade results" in home
    assert '<details class="baseline-disclosure">' in home
    assert "Show 37 baseline-only companies at 1.00x" in home
    assert home.count('data-lift-status="evidence-supported"') == 13
    assert home.count('data-lift-status="baseline-only"') == 37
    assert home.count("No company-specific financing evidence found") == 1
    assert '<a class="directory-link" href="companies/index.html">' in home
    assert 'class="company-links"' not in home

    result = run_attribution(load_attribution_bundle())
    ordered = sorted(
        result.companies,
        key=lambda company: (company.estimated_dfr_pct_mid, company.ticker),
    )
    assert f"{ordered[0].estimated_dfr_pct_mid:.2f}%" in home
    assert f"{ordered[-1].estimated_dfr_pct_mid:.2f}%" in home
    assert ordered[0].company_name in home
    assert ordered[-1].company_name in home
    assert all(
        f'href="companies/{company.ticker.lower()}/index.html"' in home
        for company in result.companies
    )

    directory = (output / "companies" / "index.html").read_text(encoding="utf-8")
    assert directory.count("data-company-directory-entry") == 50
    assert "50 covered companies, alphabetically" in directory
    directory_positions = [
        directory.index(f">{company.ticker}</strong>")
        for company in sorted(result.companies, key=lambda company: company.ticker)
    ]
    assert directory_positions == sorted(directory_positions)

    semantic_pages = {
        "scoreboard/index.html": "Scoreboard",
        "companies/index.html": "Companies",
        "companies/cvna/index.html": "Companies",
        "methodology/index.html": "Methodology",
        "methodology/coverage/index.html": "Methodology",
        "changelog/index.html": "Changelog",
    }
    for relative, current_label in semantic_pages.items():
        html = (output / relative).read_text(encoding="utf-8")
        assert '<a class="skip-link" href="#main-content">Skip to main content</a>' in html
        assert '<main id="main-content" tabindex="-1">' in html
        assert html.count('aria-current="page"') == 1
        assert f'aria-current="page">{current_label}</a>' in html
        assert 'href="' + ("../" * relative.count("/")) + 'companies/index.html"' in html

    assert 'aria-current="page"' not in home
    prediction = next((output / "scoreboard" / "predictions").glob("*/index.html"))
    assert 'aria-current="page"' not in prediction.read_text(encoding="utf-8")


def test_prepublication_filter_is_explicit_and_validation_blocks_bad_boundaries(
    tmp_path: Path,
) -> None:
    store = AppendOnlyParquetStore(tmp_path / "ledger")
    _first_id, second_id = seed(store)
    output = tmp_path / "published"
    receipt = publish_scoreboard(
        store,
        output,
        published_at=PUBLISHED_AT,
        data_vintage=DATA_VINTAGE,
        publication_mode="preview",
        minimum_made_at=datetime(2026, 8, 4, 20, 1, tzinfo=UTC),
        project_root=Path(__file__).parents[2],
    )
    assert receipt.prediction_count == 1
    assert receipt.excluded_count == 1
    assert second_id in (output / "scoreboard" / "index.html").read_text()
    with pytest.raises(SitePublishError, match="mode"):
        publish_scoreboard(
            store,
            output,
            published_at=PUBLISHED_AT,
            data_vintage=DATA_VINTAGE,
            publication_mode="bad",
            project_root=Path(__file__).parents[2],
        )
    with pytest.raises(SitePublishError, match="cannot exclude"):
        publish_scoreboard(
            store,
            output,
            published_at=PUBLISHED_AT,
            data_vintage=DATA_VINTAGE,
            publication_mode="live",
            minimum_made_at=datetime(2026, 8, 4, 20, 1, tzinfo=UTC),
            project_root=Path(__file__).parents[2],
        )
    with pytest.raises(SitePublishError, match="precede"):
        publish_scoreboard(
            store,
            output,
            published_at=datetime(2026, 8, 4, 19, 0, tzinfo=UTC),
            data_vintage=DATA_VINTAGE,
            publication_mode="preview",
            project_root=Path(__file__).parents[2],
        )


def test_publish_refuses_to_replace_unmanaged_destination(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path / "ledger")
    seed(store)
    output = tmp_path / "published"
    output.mkdir()
    (output / "user-file.txt").write_text("preserve me", encoding="utf-8")

    with pytest.raises(SitePublishError, match="unmanaged"):
        publish_scoreboard(
            store,
            output,
            published_at=PUBLISHED_AT,
            data_vintage=DATA_VINTAGE,
            publication_mode="preview",
            project_root=Path(__file__).parents[2],
        )
    assert (output / "user-file.txt").read_text(encoding="utf-8") == "preserve me"


def test_failed_build_preserves_last_good_publication_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AppendOnlyParquetStore(tmp_path / "ledger")
    seed(store)
    output = tmp_path / "published"
    project = Path(__file__).parents[2]
    publish_scoreboard(
        store,
        output,
        published_at=PUBLISHED_AT,
        data_vintage=DATA_VINTAGE,
        publication_mode="preview",
        project_root=project,
    )
    before = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }
    original = site_module._build_scoreboard

    def fail_build(*args: object, **kwargs: object) -> object:
        raise OSError("injected build failure")

    monkeypatch.setattr(site_module, "_build_scoreboard", fail_build)
    with pytest.raises(OSError, match="injected build failure"):
        publish_scoreboard(
            store,
            output,
            published_at=PUBLISHED_AT,
            data_vintage=DATA_VINTAGE,
            publication_mode="preview",
            project_root=project,
        )
    assert before == {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }
    assert not list(tmp_path.glob(".published.staging-*"))

    monkeypatch.setattr(site_module, "_build_scoreboard", original)
    receipt = publish_scoreboard(
        store,
        output,
        published_at=PUBLISHED_AT,
        data_vintage=DATA_VINTAGE,
        publication_mode="preview",
        project_root=project,
    )
    assert receipt.manifest_hash
    assert before == {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }


def test_failed_promotion_rolls_back_last_good_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "public"
    staging = tmp_path / ".public.staging-test"
    destination.mkdir()
    staging.mkdir()
    (destination / "state.txt").write_text("last-good", encoding="utf-8")
    (staging / "state.txt").write_text("candidate", encoding="utf-8")
    original_replace = Path.replace

    def replace_with_failure(path: Path, target: Path) -> Path:
        if path == staging:
            raise OSError("injected promotion failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace_with_failure)
    with pytest.raises(OSError, match="injected promotion failure"):
        site_module._promote_directory(staging, destination)

    assert (destination / "state.txt").read_text(encoding="utf-8") == "last-good"
    assert (staging / "state.txt").read_text(encoding="utf-8") == "candidate"
