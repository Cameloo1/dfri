from __future__ import annotations

import html as html_lib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import dfri.publish.site as site_module
from dfri.lake.store import AppendOnlyParquetStore
from dfri.nowcast.bridge import BridgeForecast
from dfri.nowcast.targets import FirstPrintTarget
from dfri.publish.ledger import GradeLedger, PredictionLedger
from dfri.publish.site import SitePublishError, publish_scoreboard

PUBLISHED_AT = datetime(2026, 8, 5, 5, 0, tzinfo=UTC)
DATA_VINTAGE = datetime(2026, 7, 31, 20, 15, tzinfo=UTC)


def seed(store: AppendOnlyParquetStore) -> tuple[str, str]:
    first = BridgeForecast(
        model_version="bridge-ridge-v2-alpha10",
        target_series="DELTA_DTCTLR.M",
        target_period=date(2026, 6, 30),
        made_at=datetime(2026, 8, 4, 20, 0, tzinfo=UTC),
        point=10_000.0,
        low80=8_000.0,
        high80=12_000.0,
        low95=6_000.0,
        high95=14_000.0,
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
    assert "Every prediction. No silent edits." in scoreboard
    assert first_id in scoreboard and second_id in scoreboard
    assert "Not released" in scoreboard
    assert "10,500" in scoreboard
    assert "Research and educational content. Not investment advice." in home
    assert 'href="https://creativecommons.org/licenses/by-nc/4.0/"' in home
    assert 'href="mailto:ops@camelon.app"' in home
    assert "revenue-weighted company index" in home
    assert first.total_bytes < 500_000

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
    assert {row["published_at"] for row in company_rows} == {"2026-08-05T04:17:33.789348+00:00"}
    assert {row["published_at"] for row in assumption_rows} == {"2026-08-05T04:17:33.789348+00:00"}
    assert store.read_table("publication_records").height == 2


def test_attribution_feeds_and_ten_company_pages_publish_with_full_evidence(
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
        publication_mode="live",
        project_root=Path(__file__).parents[2],
    )

    companies = json.loads((output / "v1" / "feeds" / "dfri_companies.json").read_text())
    assumptions = json.loads((output / "v1" / "feeds" / "assumptions.json").read_text())
    schema = json.loads((output / "v1" / "feeds" / "schema.json").read_text())
    assert len(companies["data"]) == 10
    assert assumptions["data"]
    assert companies["meta"]["weighting"] == "revenue-weighted"
    assert set(schema["feeds"]) == {
        "nowcast_predictions",
        "scoreboard",
        "dfri_companies",
        "assumptions",
    }
    parquet = pq.read_table(output / "v1" / "feeds" / "dfri_companies.parquet")
    assert parquet.num_rows == 10
    assert parquet.schema.field("estimated_dfr_pct_mid").type == pa.float64()
    methodology = (output / "methodology" / "index.html").read_text(encoding="utf-8")
    assert "Assumption Registry" in methodology
    assert "Matrix A has" in methodology
    assert "Tier 1 — Observed" in methodology
    home = (output / "index.html").read_text(encoding="utf-8")
    assert "Estimated DFR%" in home
    assert "range-chart" in home
    assert (output / "changelog" / "index.html").exists()

    for row in companies["data"]:
        page = output / "companies" / row["ticker"].lower() / "index.html"
        html = page.read_text(encoding="utf-8")
        assert "Estimated DFR% band" in html
        assert "Assumption sensitivity top 5" in html
        assert row["tier1_excerpt"] in html_lib.unescape(html)
        assert row["tier1_source_url"] in html
        assert "Tier 1" in html and "Tier 2" in html and "Tier 3" in html
        assert page.stat().st_size < 500_000


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
