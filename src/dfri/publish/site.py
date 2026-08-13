"""Deterministic scoreboard feeds and no-JavaScript-first static site publisher."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

import pyarrow as pa
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from dfri.attribution.criticality import criticality_payload
from dfri.attribution.engine import AttributionResult, CompanyEstimate, run_attribution
from dfri.attribution.registry import (
    DEFAULT_METHODOLOGY_VERSION,
    Assumption,
    AttributionBundle,
    MatrixBEntry,
    load_attribution_bundle,
)
from dfri.attribution.resilience import SourceDegradation
from dfri.lake.guard import VintageGuard
from dfri.lake.readers import CachingSeriesReader, LakeSeriesReader
from dfri.lake.store import AppendOnlyParquetStore, write_deterministic_parquet
from dfri.nowcast.mts import read_mts_first_print_targets
from dfri.nowcast.targets import FirstPrintTarget, read_first_print_targets
from dfri.ops.job_status import build_status_report, render_status_banner
from dfri.ops.quarterly_refresh import (
    QuarterlyRefreshLedger,
    QuarterlyRefreshRecord,
    load_refresh_report,
    refresh_identity,
)
from dfri.publish.archive_registry import load_archive_citation
from dfri.publish.changelog import load_changelog
from dfri.publish.events import build_events, canonical_json, json_feed, rss_feed
from dfri.publish.ledger import (
    GradeLedger,
    GradeRecord,
    PredictionLedger,
    PredictionRecord,
    PublicationLedger,
    PublicationRecord,
    publication_record,
)
from dfri.publish.live_calibration import (
    LiveCalibration,
    calculate_live_calibration,
    calculate_live_calibration_by_series,
)
from dfri.publish.social import SocialCard, render_social_image

METHODOLOGY_VERSION: Final = DEFAULT_METHODOLOGY_VERSION
LICENSE: Final = (
    "CC BY-NC 4.0 — free for non-commercial use with attribution; commercial licensing reserved"
)
LICENSE_URL: Final = "https://creativecommons.org/licenses/by-nc/4.0/"
TARGET_LABELS: Final = {
    "DELTA_DTCTLR.M": "Revolving credit flow",
    "DELTA_DTCTLN.M": "Nonrevolving credit flow",
    "MTS:DEFICIT.M": "Federal deficit",
    "MTS:OUTLAYS.M": "Federal outlays",
}
NOWCAST_SOURCE_URLS: Final = {
    "h8_archive": "https://www.federalreserve.gov/releases/h8/",
    "marts_archive": "https://www.census.gov/retail/marts/historic_releases.html",
    "g19_archive": "https://www.federalreserve.gov/releases/g19/",
    "mts_dataset": "https://fiscaldata.treasury.gov/datasets/monthly-treasury-statement/",
    "mts_archive": "https://fiscal.treasury.gov/accounting/monthly-treasury-statement/previous",
}
FLOW_PRODUCT_LABELS: Final = {
    "revolving_credit": "Revolving",
    "nonrevolving_credit": "Nonrevolving",
}
FLOW_CATEGORY_LABELS: Final = {
    "general_retail": "General retail",
    "auto_market": "Auto market",
    "gm_captive_auto": "GM captive",
}


class SitePublishError(RuntimeError):
    """Published feed or static-site input violates its stable public contract."""


@dataclass(frozen=True)
class PublishReceipt:
    output_root: Path
    prediction_count: int
    graded_count: int
    excluded_count: int
    total_bytes: int
    manifest_hash: str


@dataclass(frozen=True)
class CreditFlowNode:
    key: str
    label_lines: tuple[str, ...]
    amount_display: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class CreditFlowLink:
    path: str
    tier: int
    stroke_width: str
    amount_millions: float
    amount_display: str
    source_label: str
    target_label: str


@dataclass(frozen=True)
class CreditFlowRow:
    lane: str
    category: str
    tier: int
    amount_display: str
    destinations: str


@dataclass(frozen=True)
class CreditFlowView:
    period: str
    node_count: int
    product_nodes: tuple[CreditFlowNode, ...]
    category_nodes: tuple[CreditFlowNode, ...]
    company_nodes: tuple[CreditFlowNode, ...]
    product_links: tuple[CreditFlowLink, ...]
    company_links: tuple[CreditFlowLink, ...]
    rows: tuple[CreditFlowRow, ...]
    top_company_labels: str


@dataclass
class _CreditFlowGroup:
    key: str
    label: str
    tier: int
    product_amounts: dict[str, float]
    company_amounts: dict[str, float]

    @property
    def total(self) -> float:
        return sum(self.company_amounts.values())


def publish_scoreboard(
    ledger_store: AppendOnlyParquetStore,
    output_root: Path,
    *,
    raw_store: AppendOnlyParquetStore | None = None,
    published_at: datetime,
    data_vintage: datetime,
    publication_mode: str,
    minimum_made_at: datetime | None = None,
    project_root: Path | None = None,
) -> PublishReceipt:
    """Build in a disposable directory, then promote a complete publication."""

    if output_root.exists() and (
        not output_root.is_dir() or not (output_root / "manifest.json").is_file()
    ):
        raise SitePublishError(
            f"Refusing to replace unmanaged publication destination: {output_root}"
        )
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = output_root.with_name(f".{output_root.name}.staging-{uuid.uuid4().hex}")
    try:
        receipt = _build_scoreboard(
            ledger_store,
            staging_root,
            raw_store=raw_store or ledger_store,
            published_at=published_at,
            data_vintage=data_vintage,
            publication_mode=publication_mode,
            minimum_made_at=minimum_made_at,
            project_root=project_root,
        )
        if publication_mode == "live":
            PublicationLedger(ledger_store).append_many(
                PredictionLedger(ledger_store).read_all(),
                published_at=published_at,
                data_vintage=data_vintage,
                methodology_version=METHODOLOGY_VERSION,
            )
        _promote_directory(staging_root, output_root)
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    return replace(receipt, output_root=output_root)


def _build_scoreboard(
    ledger_store: AppendOnlyParquetStore,
    output_root: Path,
    *,
    raw_store: AppendOnlyParquetStore,
    published_at: datetime,
    data_vintage: datetime,
    publication_mode: str,
    minimum_made_at: datetime | None,
    project_root: Path | None,
) -> PublishReceipt:
    """Build versioned feeds, pages, permalinks, assets, and a checksum manifest."""

    _aware(published_at, "published-at")
    _aware(data_vintage, "data-vintage")
    if minimum_made_at is not None:
        _aware(minimum_made_at, "minimum-made-at")
    if publication_mode not in {"preview", "live"}:
        raise SitePublishError("Publication mode must be preview or live")
    if publication_mode == "live" and minimum_made_at is not None:
        raise SitePublishError("Live publication cannot exclude prediction-ledger rows")
    if data_vintage > published_at:
        raise SitePublishError("Data vintage cannot follow publication")
    root = project_root or Path(__file__).resolve().parents[3]
    brand = _load_object(root / "site" / "branding.yaml", "branding")
    commercial_contact = brand.get("contact")
    if not isinstance(commercial_contact, str) or not commercial_contact.strip():
        raise SitePublishError("Branding contact must be a non-empty string")
    site_url = brand.get("canonical_base_url")
    if not isinstance(site_url, str) or not site_url.startswith("https://"):
        raise SitePublishError("Branding canonical_base_url must be an HTTPS URL")
    site_url = site_url.rstrip("/") + "/"
    backtest = _load_object(root / "reports" / "m2_backtest.json", "backtest")
    mts_backtest_path = root / "reports" / "mts_backtest.json"
    mts_backtest = (
        _load_object(mts_backtest_path, "MTS backtest")
        if mts_backtest_path.exists()
        else {"targets": []}
    )
    calibration_backtest = {
        "targets": [
            *cast(list[object], backtest["targets"]),
            *cast(list[object], mts_backtest["targets"]),
        ]
    }
    attribution_bundle = load_attribution_bundle()
    attribution = run_attribution(attribution_bundle)
    original_attribution = run_attribution(load_attribution_bundle("1.1.0"))
    prior_attribution = run_attribution(load_attribution_bundle("1.2.0"))
    coverage = _load_object(
        root / "src" / "dfri" / "attribution" / "coverage_registry_v1_1.json",
        "coverage registry",
    )
    refresh_records = {
        refresh_identity(record): record
        for record in QuarterlyRefreshLedger(ledger_store).read_all()
    }
    demo_refresh = load_refresh_report(root / "reports" / "M5_QUARTERLY_REFRESH_DEMO.json")
    # The committed report keeps cold-clone publication complete. A runtime row
    # for the same source snapshot is authoritative and must not appear twice.
    refresh_records.setdefault(refresh_identity(demo_refresh), demo_refresh)
    ordered_refreshes = tuple(
        sorted(refresh_records.values(), key=lambda item: (item.effective_at, item.refresh_id))
    )
    changelog = load_changelog()
    all_predictions = PredictionLedger(ledger_store).read_all()
    predictions = tuple(
        item
        for item in all_predictions
        if minimum_made_at is None or item.made_at >= minimum_made_at
    )
    excluded_count = len(all_predictions) - len(predictions)
    if predictions and published_at < max(item.made_at for item in predictions):
        raise SitePublishError("Published-at cannot precede a selected prediction")
    grade_by_id = {item.prediction_id: item for item in GradeLedger(ledger_store).read_all()}
    prediction_ids = {item.prediction_id for item in all_predictions}
    orphan_grades = sorted(set(grade_by_id) - prediction_ids)
    if orphan_grades:
        raise SitePublishError(f"Stored grade has no prediction: {orphan_grades[0]}")
    selected_grades = tuple(
        grade_by_id[item.prediction_id] for item in predictions if item.prediction_id in grade_by_id
    )
    graded_series = {
        item.target_series for item in predictions if item.prediction_id in grade_by_id
    }
    target_histories: dict[str, tuple[FirstPrintTarget, ...]] = {}
    if graded_series:
        guard = VintageGuard(CachingSeriesReader(LakeSeriesReader(raw_store)))
        target_histories = {}
        for target_series in sorted(graded_series):
            if target_series.startswith("MTS:"):
                target_histories[target_series] = read_mts_first_print_targets(
                    guard, target_series, published_at
                )
            else:
                target_histories[target_series] = read_first_print_targets(
                    guard, target_series, published_at
                )
    live_calibration_by_series = calculate_live_calibration_by_series(
        predictions,
        selected_grades,
        target_histories,
        calibration_backtest,
    )
    g19_predictions = tuple(
        item for item in predictions if not item.target_series.startswith("MTS:")
    )
    g19_prediction_ids = {item.prediction_id for item in g19_predictions}
    g19_grades = tuple(item for item in selected_grades if item.prediction_id in g19_prediction_ids)
    g19_histories = {
        series_id: history
        for series_id, history in target_histories.items()
        if not series_id.startswith("MTS:")
    }
    legacy_live_calibration = calculate_live_calibration(
        g19_predictions,
        g19_grades,
        g19_histories,
        backtest,
    )
    publications = PublicationLedger(ledger_store)
    publication_by_id = {item.prediction_id: item for item in publications.read_all()}
    orphan_publications = sorted(set(publication_by_id) - prediction_ids)
    if orphan_publications:
        raise SitePublishError(f"Stored publication has no prediction: {orphan_publications[0]}")
    prediction_by_id = {item.prediction_id: item for item in all_predictions}
    for publication in publication_by_id.values():
        publication_record(
            prediction_by_id[publication.prediction_id],
            published_at=publication.published_at,
            data_vintage=publication.data_vintage,
            methodology_version=publication.methodology_version,
        )
    row_publications = {
        prediction.prediction_id: (
            publication_by_id.get(prediction.prediction_id)
            or publication_record(
                prediction,
                published_at=published_at,
                data_vintage=data_vintage,
                methodology_version=METHODOLOGY_VERSION,
            )
        )
        for prediction in predictions
    }
    build_meta: dict[str, object] = {
        "methodology_version": METHODOLOGY_VERSION,
        "data_vintage": data_vintage.astimezone(UTC).isoformat(),
        "published_at": published_at.astimezone(UTC).isoformat(),
        "publication_mode": publication_mode,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "commercial_license_contact": commercial_contact,
    }
    prediction_rows = [
        _prediction_feed_row(
            item,
            _publication_fields(
                row_publications[item.prediction_id],
                (
                    "live"
                    if publication_mode == "live" or item.prediction_id in publication_by_id
                    else "preview"
                ),
                commercial_contact,
            ),
        )
        for item in predictions
    ]
    scoreboard_rows = [
        _scoreboard_feed_row(
            item,
            grade_by_id.get(item.prediction_id),
            _publication_fields(
                row_publications[item.prediction_id],
                (
                    "live"
                    if publication_mode == "live" or item.prediction_id in publication_by_id
                    else "preview"
                ),
                commercial_contact,
            ),
        )
        for item in predictions
    ]
    prediction_rows.sort(key=lambda item: (str(item["made_at"]), str(item["prediction_id"])))
    scoreboard_rows.sort(key=lambda item: (str(item["made_at"]), str(item["prediction_id"])))

    feeds = output_root / "v1" / "feeds"
    meta = {
        **build_meta,
        "excluded_prepublication_count": excluded_count,
        "row_count": len(prediction_rows),
        "live_calibration_by_series": {
            series_id: calibration.feed()
            for series_id, calibration in live_calibration_by_series.items()
        },
        # Compatibility field: preserve the pre-MTS G.19-only aggregate byte contract.
        # All current UI and new consumers use live_calibration_by_series.
        "live_calibration": legacy_live_calibration.feed(),
    }
    _write_json(feeds / "nowcast_predictions.json", {"meta": meta, "data": prediction_rows})
    _write_csv(feeds / "nowcast_predictions.csv", prediction_rows, _prediction_columns())
    _write_parquet(feeds / "nowcast_predictions.parquet", prediction_rows, _prediction_schema())
    _write_json(feeds / "scoreboard.json", {"meta": meta, "data": scoreboard_rows})
    _write_csv(feeds / "scoreboard.csv", scoreboard_rows, _scoreboard_columns())
    company_rows_v2 = _company_feed_rows(
        attribution,
        publication_mode=publication_mode,
        commercial_contact=commercial_contact,
    )
    company_rows = [
        {column: row[column] for column in _company_columns()} for row in company_rows_v2
    ]
    assumption_rows_v2 = _assumption_feed_rows(
        attribution_bundle,
        publication_mode=publication_mode,
        commercial_contact=commercial_contact,
    )
    assumption_rows = [
        {column: row.get(column, "") for column in _assumption_columns()}
        for row in assumption_rows_v2
    ]
    attribution_meta = {
        **build_meta,
        "data_vintage": attribution.data_vintage,
        "source_hash": attribution.source_hash,
        "row_count": len(company_rows),
        "weighting": attribution.aggregate.weighting,
    }
    _write_json(feeds / "dfri_companies.json", {"meta": attribution_meta, "data": company_rows})
    _write_csv(feeds / "dfri_companies.csv", company_rows, _company_columns())
    _write_parquet(feeds / "dfri_companies.parquet", company_rows, _company_schema())
    feeds_v2 = output_root / "v2" / "feeds"
    attribution_meta_v2 = {
        **attribution_meta,
        "schema_version": "v2",
        "evidence_lift_headline": attribution.evidence_lift_headline,
        "source_status": "DEGRADED" if attribution.source_degradations else "PRIMARY",
        "source_degradation_count": len(attribution.source_degradations),
        "source_degradations": [asdict(item) for item in attribution.source_degradations],
    }
    _write_json(
        feeds_v2 / "dfri_companies.json",
        {"meta": attribution_meta_v2, "data": company_rows_v2},
    )
    _write_csv(feeds_v2 / "dfri_companies.csv", company_rows_v2, _company_columns_v2())
    _write_parquet(feeds_v2 / "dfri_companies.parquet", company_rows_v2, _company_schema_v2())
    _write_json(feeds_v2 / "schema.json", _feed_schema_v2(build_meta))
    assumption_meta = {
        **attribution_meta,
        "row_count": len(assumption_rows),
    }
    _write_json(feeds / "assumptions.json", {"meta": assumption_meta, "data": assumption_rows})
    _write_csv(feeds / "assumptions.csv", assumption_rows, _assumption_columns())
    assumption_meta_v2 = {**assumption_meta, "schema_version": "v2"}
    _write_json(
        feeds_v2 / "assumptions.json",
        {"meta": assumption_meta_v2, "data": assumption_rows_v2},
    )
    _write_csv(feeds_v2 / "assumptions.csv", assumption_rows_v2, _assumption_columns_v2())
    exclusion_rows = _exclusion_feed_rows(
        coverage,
        common=attribution_meta,
    )
    exclusion_meta: dict[str, object] = {
        **attribution_meta,
        "row_count": len(exclusion_rows),
    }
    _write_json(
        feeds / "coverage_exclusions.json",
        {
            "meta": exclusion_meta,
            "data": exclusion_rows,
        },
    )
    _write_csv(feeds / "coverage_exclusions.csv", exclusion_rows, _exclusion_columns())
    refresh_rows, company_history_rows = _refresh_feed_rows(
        ordered_refreshes,
        commercial_contact=commercial_contact,
    )
    refresh_meta: dict[str, object] = {
        **attribution_meta,
        "row_count": len(refresh_rows),
    }
    company_history_meta: dict[str, object] = {
        **attribution_meta,
        "row_count": len(company_history_rows),
    }
    _write_json(
        feeds / "quarterly_refreshes.json",
        {
            "meta": refresh_meta,
            "data": refresh_rows,
        },
    )
    _write_json(
        feeds / "dfri_company_history.json",
        {
            "meta": company_history_meta,
            "data": company_history_rows,
        },
    )
    _write_json(feeds / "schema.json", _feed_schema(build_meta))

    automation_status = build_status_report(
        as_of=published_at,
        receipt_directory=root / ".local" / "evidence" / "job_status",
        publication_mode=publication_mode,
    )
    _write_json(output_root / "v1" / "status.json", automation_status)
    _atomic_write(output_root / "status" / "banner.html", render_status_banner(automation_status))
    publication_events = build_events(
        predictions,
        selected_grades,
        changelog,
        site_url=site_url,
    )
    _atomic_write(
        output_root / "v1" / "events.json",
        canonical_json(json_feed(publication_events, generated_at=published_at)),
    )
    _atomic_write(output_root / "events.xml", rss_feed(publication_events, site_url=site_url))

    assets = output_root / "assets"
    _copy_stylesheet(root / "site" / "static" / "site.css", assets / "site.css")
    _copy(root / "site" / "static" / "site.js", assets / "site.js")
    shutil.copytree(root / "site" / "static" / "fonts", assets / "fonts")
    display_rows = [
        _display_row(item, grade_by_id.get(item.prediction_id))
        for item in sorted(
            predictions,
            key=lambda row: (row.made_at, row.prediction_id),
            reverse=True,
        )
    ]
    summary = _summary(display_rows, backtest)
    calibration_display = [
        {
            "target_series": series_id,
            "target_label": TARGET_LABELS[series_id],
            **_calibration_display(calibration),
        }
        for series_id, calibration in live_calibration_by_series.items()
    ]
    first_grade_callout = _first_grade_callout(predictions, grade_by_id, backtest)
    environment = Environment(
        loader=FileSystemLoader(root / "site" / "templates"),
        autoescape=select_autoescape(("html",)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    base_context = {
        "brand": brand,
        "site_url": site_url,
        "publication_mode": publication_mode,
        "excluded_count": excluded_count,
        "methodology_version": METHODOLOGY_VERSION,
        "data_vintage": build_meta["data_vintage"],
        "published_at": build_meta["published_at"],
        "feed_license_url": LICENSE_URL,
        "commercial_license_contact": commercial_contact,
        "nowcast_sources": NOWCAST_SOURCE_URLS,
        "source_degradations": [
            _source_degradation_display(item) for item in attribution.source_degradations
        ],
        "automation_status": automation_status,
        "archive_citation": load_archive_citation(),
    }
    company_displays = [_company_display(item) for item in attribution.companies]
    company_histories = _company_histories(attribution, company_history_rows)
    aggregate_display = _aggregate_display(attribution)
    company_midpoint_anchor = _company_midpoint_anchor(attribution)
    companies_by_lift = [
        {**item, "rank": rank}
        for rank, item in enumerate(
            sorted(
                company_displays,
                key=lambda item: (
                    -cast(float, item["evidence_lift_value"]),
                    str(item["ticker"]),
                ),
            ),
            start=1,
        )
    ]
    evidence_supported_companies = [
        item for item in companies_by_lift if not cast(bool, item["baseline_only"])
    ]
    baseline_companies = [item for item in companies_by_lift if cast(bool, item["baseline_only"])]
    credit_flow = _credit_flow_view(attribution_bundle, attribution)
    comparison_rows = _methodology_comparison(
        original_attribution,
        prior_attribution,
        attribution,
    )
    _render_social_images(
        assets / "social",
        aggregate=aggregate_display,
        rows=display_rows,
        companies=company_displays,
        changelog_count=len(changelog),
        exclusion_count=len(cast(list[dict[str, object]], coverage["excluded"])),
    )
    _render(
        environment,
        "home.html",
        output_root / "index.html",
        {
            **base_context,
            **_page_metadata(
                site_url, "", "assets/social/home.png", "DFRI headline estimate and range"
            ),
            "root": "",
            "active_nav": None,
            "title": "Immutable consumer-credit nowcasts",
            "description": "DFRI predictions and first-print Federal Reserve G.19 grades.",
            "latest": display_rows[0] if display_rows else None,
            "summary": summary,
            "live_calibration": calibration_display,
            "aggregate": aggregate_display,
            "company_midpoint_anchor": company_midpoint_anchor,
            "evidence_supported_companies": evidence_supported_companies,
            "baseline_companies": baseline_companies,
            "evidence_lift_headline": attribution.evidence_lift_headline,
            "company_count": len(company_displays),
            "credit_flow": credit_flow,
            "credit_flow_methodology_href": "methodology/index.html#credit-flow",
        },
    )
    _render(
        environment,
        "companies.html",
        output_root / "companies" / "index.html",
        {
            **base_context,
            **_page_metadata(
                site_url,
                "companies/",
                "assets/social/companies.png",
                "DFRI covered company count",
            ),
            "root": "../",
            "active_nav": "companies",
            "title": "Companies",
            "description": "Alphabetical directory of all covered DFRI company estimates.",
            "companies": sorted(company_displays, key=lambda item: str(item["ticker"])),
            "company_count": len(company_displays),
        },
    )
    _render(
        environment,
        "scoreboard.html",
        output_root / "scoreboard" / "index.html",
        {
            **base_context,
            **_page_metadata(
                site_url,
                "scoreboard/",
                "assets/social/scoreboard.png",
                "DFRI immutable prediction ledger count",
            ),
            "root": "../",
            "active_nav": "scoreboard",
            "title": "Scoreboard",
            "description": "Every immutable DFRI prediction and first-print grade.",
            "rows": display_rows,
            "live_calibration": calibration_display,
            "first_grade_callout": first_grade_callout,
        },
    )
    _render(
        environment,
        "methodology.html",
        output_root / "methodology" / "index.html",
        {
            **base_context,
            **_page_metadata(
                site_url,
                "methodology/",
                "assets/social/methodology.png",
                "DFRI methodology version",
            ),
            "root": "../",
            "active_nav": "methodology",
            "title": "Methodology",
            "description": "Point-in-time DFRI nowcast and attribution methodology.",
            "summary": summary,
            "assumptions": [
                _assumption_display(item, attribution.source_degradations)
                for item in attribution_bundle.assumptions
            ],
            "critical_assumptions": [
                _assumption_display(item, attribution.source_degradations)
                for item in attribution_bundle.assumptions
                if item.criticality_rating == "CRITICAL"
            ],
            "criticality": criticality_payload(attribution_bundle),
            "matrix_a": attribution_bundle.matrix_a,
            "matrix_b": attribution_bundle.matrix_b,
            "company_count": len(company_displays),
            "credit_flow": credit_flow,
            "credit_flow_methodology_href": "#credit-flow-method",
        },
    )
    _render(
        environment,
        "methodology_comparison.html",
        output_root / "methodology" / "sensitivity" / "index.html",
        {
            **base_context,
            **_page_metadata(
                site_url,
                "methodology/sensitivity/",
                "assets/social/sensitivity.png",
                "DFRI methodology sensitivity estimate and range",
            ),
            "root": "../../",
            "active_nav": "methodology",
            "title": "Methodology sensitivity",
            "description": (
                "Immutable comparison of DFRI methodology versions 1.1.0, 1.2.0, and 1.2.1."
            ),
            "original_methodology_version": original_attribution.methodology_version,
            "prior_methodology_version": prior_attribution.methodology_version,
            "current_methodology_version": attribution.methodology_version,
            "original": _aggregate_display(original_attribution),
            "prior": _aggregate_display(prior_attribution),
            "current": aggregate_display,
            "rows": comparison_rows,
        },
    )
    exclusions = cast(list[dict[str, object]], coverage["excluded"])
    _render(
        environment,
        "coverage_exclusions.html",
        output_root / "methodology" / "coverage" / "index.html",
        {
            **base_context,
            **_page_metadata(
                site_url,
                "methodology/coverage/",
                "assets/social/coverage.png",
                "DFRI coverage and exclusion counts",
            ),
            "root": "../../",
            "active_nav": "methodology",
            "title": "Coverage and exclusions",
            "description": "Dated DFRI P1 coverage boundary and exclusion reasons.",
            "verified_at": coverage["verified_at"],
            "membership_snapshot_ref": coverage["membership_snapshot_ref"],
            "policy": cast(dict[str, object], coverage["selection_policy"]),
            "included": company_displays,
            "exclusions": exclusions,
        },
    )
    _render(
        environment,
        "changelog.html",
        output_root / "changelog" / "index.html",
        {
            **base_context,
            **_page_metadata(
                site_url,
                "changelog/",
                "assets/social/changelog.png",
                "DFRI append-only changelog entry count",
            ),
            "root": "../",
            "active_nav": "changelog",
            "title": "Changelog",
            "description": "Append-only DFRI publication and methodology changes.",
            "entries": [item.display() for item in reversed(changelog)],
        },
    )
    _render(
        environment,
        "roadmap.html",
        output_root / "roadmap" / "index.html",
        {
            **base_context,
            **_page_metadata(
                site_url,
                "roadmap/",
                "assets/social/methodology.png",
                "DFRI current scope, planned work, and deliberate exclusions",
            ),
            "root": "../",
            "active_nav": "roadmap",
            "title": "Roadmap and boundaries",
            "description": "What DFRI measures, plans, and deliberately excludes.",
        },
    )
    _render(
        environment,
        "corrections.html",
        output_root / "corrections" / "index.html",
        {
            **base_context,
            **_page_metadata(
                site_url,
                "corrections/",
                "assets/social/changelog.png",
                "DFRI append-only corrections policy",
            ),
            "root": "../",
            "active_nav": None,
            "title": "Corrections policy",
            "description": "How to report, verify, publish, and retrieve DFRI corrections.",
        },
    )
    for company, display in zip(attribution.companies, company_displays, strict=True):
        _render(
            environment,
            "company.html",
            output_root / "companies" / company.ticker.lower() / "index.html",
            {
                **base_context,
                **_page_metadata(
                    site_url,
                    f"companies/{company.ticker.lower()}/",
                    f"assets/social/company-{company.ticker.lower()}.png",
                    f"{company.company_name} estimated DFR percent and range",
                ),
                "root": "../../",
                "active_nav": "companies",
                "title": f"{company.company_name} ({company.ticker})",
                "description": (f"Estimated debt-funded revenue share for {company.company_name}."),
                "company": display,
                "history": company_histories[company.ticker],
            },
        )
    for row in display_rows:
        _render(
            environment,
            "prediction.html",
            output_root / "scoreboard" / "predictions" / str(row["prediction_id"]) / "index.html",
            {
                **base_context,
                **_page_metadata(
                    site_url,
                    f"scoreboard/predictions/{row['prediction_id']}/",
                    f"assets/social/prediction-{row['prediction_id']}.png",
                    f"{row['target_label']} prediction and uncertainty range",
                ),
                "root": "../../../",
                "active_nav": None,
                "title": f"Prediction {row['prediction_id']}",
                "description": (
                    "Immutable DFRI prediction, uncertainty bands, and first-print grade."
                ),
                "row": row,
            },
        )
    files = sorted(
        (
            path
            for path in output_root.rglob("*")
            if path.is_file() and path != output_root / "manifest.json"
        ),
        key=lambda path: path.relative_to(output_root).as_posix(),
    )
    manifest = {
        "methodology_version": METHODOLOGY_VERSION,
        "data_vintage": build_meta["data_vintage"],
        "published_at": build_meta["published_at"],
        "files": [
            {
                "path": path.relative_to(output_root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _file_hash(path),
            }
            for path in files
        ],
    }
    _write_json(output_root / "manifest.json", manifest)
    manifest_hash = _file_hash(output_root / "manifest.json")
    total_bytes = sum(path.stat().st_size for path in output_root.rglob("*") if path.is_file())
    return PublishReceipt(
        output_root=output_root,
        prediction_count=len(predictions),
        graded_count=sum(item.prediction_id in grade_by_id for item in predictions),
        excluded_count=excluded_count,
        total_bytes=total_bytes,
        manifest_hash=manifest_hash,
    )


def _publication_fields(
    record: PublicationRecord, mode: str, commercial_contact: str
) -> dict[str, object]:
    return {
        "methodology_version": record.methodology_version,
        "data_vintage": record.data_vintage.astimezone(UTC).isoformat(),
        "published_at": record.published_at.astimezone(UTC).isoformat(),
        "publication_mode": mode,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "commercial_license_contact": commercial_contact,
    }


def _prediction_feed_row(
    prediction: PredictionRecord, common: Mapping[str, object]
) -> dict[str, object]:
    return {
        "prediction_id": prediction.prediction_id,
        "made_at": prediction.made_at.astimezone(UTC).isoformat(),
        "model_version": prediction.model_version,
        "inputs_hash": prediction.inputs_hash,
        "target_series": prediction.target_series,
        "target_period": prediction.target_period.isoformat(),
        "point": prediction.point,
        "low80": prediction.low80,
        "high80": prediction.high80,
        "low95": prediction.low95,
        "high95": prediction.high95,
        "status": prediction.status,
        "permalink": f"/scoreboard/predictions/{prediction.prediction_id}/",
        **common,
    }


def _scoreboard_feed_row(
    prediction: PredictionRecord,
    grade: GradeRecord | None,
    common: Mapping[str, object],
) -> dict[str, object]:
    grade_status = "GRADED" if grade else "PENDING_FIRST_PRINT"
    return {
        **_prediction_feed_row(prediction, common),
        # ``status`` predates grade_status and remains a compatibility field on
        # this joined feed. It must describe the joined row, not the immutable
        # prediction record in isolation.
        "status": grade_status,
        "grade_status": grade_status,
        "actual_first_print": grade.actual_first_print if grade else None,
        "vintage_url": grade.vintage_url if grade else None,
        "abs_error": grade.abs_error if grade else None,
        "graded_at": grade.graded_at.astimezone(UTC).isoformat() if grade else None,
    }


def _display_row(prediction: PredictionRecord, grade: GradeRecord | None) -> dict[str, object]:
    label = TARGET_LABELS.get(prediction.target_series)
    if label is None:
        raise SitePublishError(f"No public target label for {prediction.target_series}")
    is_mts = prediction.target_series.startswith("MTS:")
    return {
        **prediction.row(),
        "target_label": label,
        "made_at": prediction.made_at.astimezone(UTC).isoformat(),
        "made_at_display": prediction.made_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "target_period": prediction.target_period.isoformat(),
        "point_display": _number(prediction.point),
        "low80_display": _number(prediction.low80),
        "high80_display": _number(prediction.high80),
        "low95_display": _number(prediction.low95),
        "high95_display": _number(prediction.high95),
        "grade_status": "Graded" if grade else "Awaiting first print",
        "grade_status_class": "graded" if grade else "pending",
        "actual_display": _number(grade.actual_first_print) if grade else "Not released",
        "abs_error_display": _number(grade.abs_error) if grade else "—",
        "abs_error_sort": grade.abs_error if grade else "",
        "vintage_url": grade.vintage_url if grade else None,
        "source_family": "Treasury MTS" if is_mts else "Federal Reserve G.19",
        "unit_context": (
            "Unadjusted monthly total · millions of U.S. dollars"
            if is_mts
            else "Seasonally adjusted · millions of U.S. dollars"
        ),
        "permalink_unit_context": (
            "unadjusted monthly total · $M" if is_mts else "seasonally adjusted monthly flow · $M"
        ),
        "input_source_label": (
            "U.S. Treasury MTS dated issues"
            if is_mts
            else "Federal Reserve H.8 and Census MARTS dated releases"
        ),
        "grading_source_label": (
            "U.S. Treasury dated MTS issue"
            if is_mts
            else "Federal Reserve Board dated G.19 release"
        ),
        "is_mts": is_mts,
    }


def _summary(rows: list[dict[str, object]], backtest: dict[str, object]) -> dict[str, object]:
    targets = cast(list[object], backtest["targets"])
    primary = cast(dict[str, object], targets[0])
    metrics = cast(list[dict[str, object]], primary["metrics"])
    headline = cast(dict[str, object], backtest["primary_headline"])
    metric = next(item for item in metrics if item["model_version"] == headline["model_version"])
    graded = sum(item["grade_status"] == "Graded" for item in rows)
    return {
        "prediction_count": len(rows),
        "graded_count": graded,
        "pending_count": len(rows) - graded,
        "mae_display": _number(cast(float, metric["mae"])),
        "improvement_display": (
            f"{cast(float, headline['mae_improvement_vs_best_naive']) * 100:.1f}%"
        ),
        "best_naive": headline["best_naive_model_version"],
        "coverage80_display": f"{cast(float, metric['coverage80']) * 100:.1f}%",
        "coverage95_display": f"{cast(float, metric['coverage95']) * 100:.1f}%",
    }


def _calibration_display(calibration: LiveCalibration) -> dict[str, object]:
    versions = sorted(set(calibration.naive_model_versions.values()))
    return {
        "graded_count": calibration.graded_count,
        "coverage80_display": _percentage(calibration.coverage80),
        "coverage95_display": _percentage(calibration.coverage95),
        "mae_display": _optional_number(calibration.mae),
        "naive_mae_display": _optional_number(calibration.naive_mae),
        "naive_model_display": ", ".join(versions) if versions else "awaiting first grade",
    }


def _first_grade_callout(
    predictions: Sequence[PredictionRecord],
    grade_by_id: Mapping[str, GradeRecord],
    backtest: Mapping[str, object],
) -> dict[str, str] | None:
    graded_revolving = [
        item
        for item in sorted(predictions, key=lambda row: (row.made_at, row.prediction_id))
        if item.target_series == "DELTA_DTCTLR.M" and item.prediction_id in grade_by_id
    ]
    if not graded_revolving:
        return None
    prediction = graded_revolving[0]
    grade = grade_by_id[prediction.prediction_id]
    sign_miss = (prediction.point < 0 < grade.actual_first_print) or (
        prediction.point > 0 > grade.actual_first_print
    )
    outside80 = not (prediction.low80 <= grade.actual_first_print <= prediction.high80)
    if not sign_miss or not outside80:
        return None
    return {
        "prediction_id": prediction.prediction_id,
        "predicted": _number(prediction.point),
        "actual": _number(grade.actual_first_print),
        "abs_error": _rounded_tens(grade.abs_error),
        "backtest_mae": _number(_backtest_mae(backtest, prediction)),
    }


def _backtest_mae(backtest: Mapping[str, object], prediction: PredictionRecord) -> float:
    targets = backtest.get("targets")
    if not isinstance(targets, list):
        raise SitePublishError("Backtest targets must be a list")
    for raw_target in targets:
        if not isinstance(raw_target, dict):
            continue
        if raw_target.get("target_series") != prediction.target_series:
            continue
        metrics = raw_target.get("metrics")
        if not isinstance(metrics, list):
            break
        for metric in metrics:
            if (
                isinstance(metric, dict)
                and metric.get("model_version") == prediction.model_version
                and isinstance(metric.get("mae"), (int, float))
            ):
                return float(metric["mae"])
    raise SitePublishError(
        f"Backtest has no MAE for {prediction.target_series}/{prediction.model_version}"
    )


def _company_feed_rows(
    result: AttributionResult,
    *,
    publication_mode: str,
    commercial_contact: str,
) -> list[dict[str, object]]:
    common: dict[str, object] = {
        "methodology_version": result.methodology_version,
        "data_vintage": result.data_vintage,
        "published_at": result.first_published_at,
        "publication_mode": publication_mode,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "commercial_license_contact": commercial_contact,
    }
    return [
        {
            "ticker": item.ticker,
            "company_name": item.company_name,
            "quarter": item.quarter,
            "estimated_dfr_pct_low": item.estimated_dfr_pct_low,
            "estimated_dfr_pct_mid": item.estimated_dfr_pct_mid,
            "estimated_dfr_pct_high": item.estimated_dfr_pct_high,
            "estimated_debt_funded_revenue_mid_millions": (
                item.estimated_debt_funded_revenue_mid_millions
            ),
            "estimated_us_consumer_revenue_mid_millions": (
                item.estimated_us_consumer_revenue_mid_millions
            ),
            "fungibility_baseline_dfr_pct_mid": item.fungibility_baseline_dfr_pct_mid,
            "evidence_lift": item.evidence_lift,
            "evidence_lift_status": item.evidence_lift_status,
            "evidence_lift_headline": result.evidence_lift_headline,
            "tier1_share": item.tier1_share,
            "tier2_share": item.tier2_share,
            "tier3_share": item.tier3_share,
            "revenue_source_url": item.revenue_source_url,
            "tier1_source_url": item.tier1_source_url,
            "tier1_excerpt": item.tier1_excerpt,
            "assumption_ids": "|".join(item.assumption_ids),
            "sensitivity_top5": json.dumps(
                [
                    {
                        "assumption_id": sensitivity.assumption_id,
                        "absolute_correlation": sensitivity.absolute_correlation,
                        "direction": sensitivity.direction,
                    }
                    for sensitivity in item.sensitivity_top5
                ],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "permalink": f"/companies/{item.ticker.lower()}/",
            **common,
        }
        for item in result.companies
    ]


def _exclusion_feed_rows(
    coverage: dict[str, object], *, common: Mapping[str, object]
) -> list[dict[str, object]]:
    raw = coverage.get("excluded")
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise SitePublishError("Coverage exclusions must be object rows")
    return [
        {
            "ticker": cast(dict[str, object], item)["ticker"],
            "company_name": cast(dict[str, object], item)["company_name"],
            "cik": cast(dict[str, object], item)["cik"],
            "gics_sub_industry": cast(dict[str, object], item)["gics_sub_industry"],
            "reason": cast(dict[str, object], item)["reason"],
            "membership_snapshot_ref": coverage["membership_snapshot_ref"],
            "verified_at": coverage["verified_at"],
            "methodology_version": common["methodology_version"],
            "data_vintage": common["data_vintage"],
            "published_at": common["published_at"],
            "publication_mode": common["publication_mode"],
            "license": common["license"],
            "license_url": common["license_url"],
            "commercial_license_contact": common["commercial_license_contact"],
        }
        for item in raw
    ]


def _refresh_feed_rows(
    records: Sequence[QuarterlyRefreshRecord], *, commercial_contact: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    summaries: list[dict[str, object]] = []
    companies: list[dict[str, object]] = []
    for record in records:
        payload = record.payload()
        result = cast(dict[str, object], payload["result"])
        aggregate = cast(dict[str, object], result["aggregate"])
        inputs = {
            cast(str, item["ticker"]): item
            for item in cast(list[dict[str, object]], payload["company_inputs"])
        }
        common: dict[str, object] = {
            "refresh_id": record.refresh_id,
            "target_quarter": record.target_quarter,
            "effective_at": record.effective_at.astimezone(UTC).isoformat(),
            "data_vintage": record.data_vintage.astimezone(UTC).isoformat(),
            "methodology_version": record.methodology_version,
            "source_hash": record.source_hash,
            "license": LICENSE,
            "license_url": LICENSE_URL,
            "commercial_license_contact": commercial_contact,
        }
        summaries.append(
            {
                **common,
                "company_count": record.company_count,
                "updated_company_count": record.updated_company_count,
                "estimated_dfr_pct_low": aggregate["estimated_dfr_pct_low"],
                "estimated_dfr_pct_mid": aggregate["estimated_dfr_pct_mid"],
                "estimated_dfr_pct_high": aggregate["estimated_dfr_pct_high"],
                "weighting": aggregate["weighting"],
            }
        )
        for item in cast(list[dict[str, object]], result["companies"]):
            ticker = cast(str, item["ticker"])
            companies.append(
                {
                    **common,
                    "ticker": ticker,
                    "company_name": item["company_name"],
                    "input_status": inputs[ticker]["status"],
                    "estimated_dfr_pct_low": item["estimated_dfr_pct_low"],
                    "estimated_dfr_pct_mid": item["estimated_dfr_pct_mid"],
                    "estimated_dfr_pct_high": item["estimated_dfr_pct_high"],
                    "tier1_share": item["tier1_share"],
                    "tier2_share": item["tier2_share"],
                    "tier3_share": item["tier3_share"],
                }
            )
    return summaries, companies


def _assumption_feed_rows(
    bundle: AttributionBundle,
    *,
    publication_mode: str,
    commercial_contact: str,
) -> list[dict[str, object]]:
    common: dict[str, object] = {
        "methodology_version": bundle.methodology_version,
        "data_vintage": bundle.data_vintage,
        "published_at": bundle.first_published_at,
        "publication_mode": publication_mode,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "commercial_license_contact": commercial_contact,
    }
    return [
        {
            "assumption_id": item.assumption_id,
            "statement": item.statement,
            "value_low": item.prior.low,
            "value_mid": item.prior.mid,
            "value_high": item.prior.high,
            "tier": item.tier,
            "source_url": item.source_url,
            "evidence_snippet": item.evidence_snippet,
            "sensitivity_note": item.sensitivity_note,
            "version": item.version,
            "active": item.active,
            "review_status": item.review_status,
            **common,
        }
        for item in bundle.assumptions
    ]


def _company_display(item: CompanyEstimate) -> dict[str, object]:
    return {
        "ticker": item.ticker,
        "company_name": item.company_name,
        "quarter": item.quarter,
        "low": f"{item.estimated_dfr_pct_low:.2f}%",
        "mid": f"{item.estimated_dfr_pct_mid:.2f}%",
        "high": f"{item.estimated_dfr_pct_high:.2f}%",
        "debt_revenue": f"{item.estimated_debt_funded_revenue_mid_millions:,.0f}",
        "consumer_revenue": f"{item.estimated_us_consumer_revenue_mid_millions:,.0f}",
        "fungibility_baseline": f"{item.fungibility_baseline_dfr_pct_mid:.2f}%",
        "evidence_lift": f"{item.evidence_lift:.2f}x",
        "evidence_lift_value": item.evidence_lift,
        "evidence_lift_status": item.evidence_lift_status,
        "baseline_only": item.evidence_lift_status == "baseline-only",
        "tier1": f"{item.tier1_share * 100:.1f}%",
        "tier2": f"{item.tier2_share * 100:.1f}%",
        "tier3": f"{item.tier3_share * 100:.1f}%",
        "tier1_width": f"{item.tier1_share * 100:.6f}",
        "tier2_width": f"{item.tier2_share * 100:.6f}",
        "tier3_width": f"{item.tier3_share * 100:.6f}",
        "revenue_source_url": item.revenue_source_url,
        "tier1_source_url": item.tier1_source_url,
        "tier1_excerpt": item.tier1_excerpt,
        "assumption_ids": item.assumption_ids,
        "sensitivity": [
            {
                "assumption_id": sensitivity.assumption_id,
                "correlation": f"{sensitivity.absolute_correlation:.3f}",
                "direction": sensitivity.direction,
            }
            for sensitivity in item.sensitivity_top5
        ],
    }


def _company_histories(
    current: AttributionResult, refresh_rows: list[dict[str, object]]
) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {item.ticker: [] for item in current.companies}
    for row in refresh_rows:
        ticker = cast(str, row["ticker"])
        grouped[ticker].append(
            {
                "label": f"{row['target_quarter']} refresh",
                "effective_at": str(row["effective_at"])[:10],
                "low_value": float(cast(float, row["estimated_dfr_pct_low"])),
                "mid_value": float(cast(float, row["estimated_dfr_pct_mid"])),
                "high_value": float(cast(float, row["estimated_dfr_pct_high"])),
                "status": row["input_status"],
                "provenance_path": "v1/feeds/dfri_company_history.json",
            }
        )
    for item in current.companies:
        grouped[item.ticker].append(
            {
                "label": f"{item.quarter} methodology snapshot",
                "effective_at": current.first_published_at[:10],
                "low_value": item.estimated_dfr_pct_low,
                "mid_value": item.estimated_dfr_pct_mid,
                "high_value": item.estimated_dfr_pct_high,
                "status": "VERSIONED_BASELINE",
                "provenance_path": "v2/feeds/dfri_companies.json",
            }
        )
    for rows in grouped.values():
        maximum = max(float(cast(float, item["high_value"])) for item in rows) * 1.1
        for index, history_item in enumerate(rows):
            history_item["y"] = 38 + index * 54
            history_item["low_x"] = (
                90 + float(cast(float, history_item["low_value"])) / maximum * 430
            )
            history_item["mid_x"] = (
                90 + float(cast(float, history_item["mid_value"])) / maximum * 430
            )
            history_item["high_x"] = (
                90 + float(cast(float, history_item["high_value"])) / maximum * 430
            )
            history_item["low"] = f"{float(cast(float, history_item['low_value'])):.2f}%"
            history_item["mid"] = f"{float(cast(float, history_item['mid_value'])):.2f}%"
            history_item["high"] = f"{float(cast(float, history_item['high_value'])):.2f}%"
        for history_item in rows:
            history_item["height"] = 34 + len(rows) * 54
    return grouped


def _aggregate_display(result: AttributionResult) -> dict[str, object]:
    aggregate = result.aggregate
    return {
        "quarter": aggregate.quarter,
        "low": f"{aggregate.estimated_dfr_pct_low:.2f}%",
        "mid": f"{aggregate.estimated_dfr_pct_mid:.2f}%",
        "high": f"{aggregate.estimated_dfr_pct_high:.2f}%",
        "debt_revenue": f"{aggregate.estimated_debt_funded_revenue_mid_millions:,.0f}",
        "consumer_revenue": f"{aggregate.estimated_us_consumer_revenue_mid_millions:,.0f}",
        "tier1": f"{aggregate.tier1_share * 100:.1f}%",
        "tier2": f"{aggregate.tier2_share * 100:.1f}%",
        "tier3": f"{aggregate.tier3_share * 100:.1f}%",
        "weighting": aggregate.weighting,
        "company_count": len(result.companies),
    }


def _company_midpoint_anchor(result: AttributionResult) -> dict[str, object]:
    ordered = sorted(
        result.companies,
        key=lambda company: (company.estimated_dfr_pct_mid, company.ticker),
    )
    if not ordered:
        raise SitePublishError("Company midpoint comparison requires covered companies")
    lowest = ordered[0]
    highest = ordered[-1]
    return {
        "lowest_mid": f"{lowest.estimated_dfr_pct_mid:.2f}%",
        "lowest_name": lowest.company_name,
        "lowest_href": f"companies/{lowest.ticker.lower()}/index.html",
        "highest_mid": f"{highest.estimated_dfr_pct_mid:.2f}%",
        "highest_name": highest.company_name,
        "highest_href": f"companies/{highest.ticker.lower()}/index.html",
    }


def _credit_flow_view(
    bundle: AttributionBundle,
    result: AttributionResult,
) -> CreditFlowView:
    """Collapse published midpoint mappings into a small, tier-preserving SVG view."""

    flow_midpoints = {item.debt_product: item.prior.mid for item in bundle.flows}
    b_by_category: dict[str, list[MatrixBEntry]] = defaultdict(list)
    for b_entry in bundle.matrix_b:
        b_by_category[b_entry.spend_category].append(b_entry)

    category_groups: dict[str, _CreditFlowGroup] = {}
    for a_row in bundle.matrix_a:
        if a_row.debt_product not in flow_midpoints:
            raise SitePublishError(f"Flow view lacks product input: {a_row.debt_product}")
        group = category_groups.setdefault(
            a_row.spend_category,
            _CreditFlowGroup(
                key=a_row.spend_category,
                label=FLOW_CATEGORY_LABELS.get(
                    a_row.spend_category,
                    a_row.spend_category.replace("_", " ").capitalize(),
                ),
                tier=a_row.tier,
                product_amounts={},
                company_amounts={},
            ),
        )
        if group.tier != a_row.tier:
            raise SitePublishError(
                f"Flow view cannot combine tiers for category: {a_row.spend_category}"
            )
        for b_row in b_by_category.get(a_row.spend_category, []):
            ticker = b_row.ticker
            b_mid = b_row.prior.mid
            amount = flow_midpoints[a_row.debt_product] * a_row.prior.mid * b_mid
            if amount <= 0:
                continue
            group.product_amounts[a_row.debt_product] = (
                group.product_amounts.get(a_row.debt_product, 0.0) + amount
            )
            group.company_amounts[ticker] = group.company_amounts.get(ticker, 0.0) + amount

    ordered_categories = sorted(
        (item for item in category_groups.values() if item.total > 0),
        key=lambda item: (-item.total, item.key),
    )
    # One named category plus tier-specific remainder groups keeps the static view
    # readable at 320-360 CSS pixels without losing the evidence-style encoding.
    named = ordered_categories[:1]
    named_keys = {item.key for item in named}
    groups = list(named)
    remainder_labels = {
        1: "Other Tier 1",
        2: "Other Tier 2",
        3: "Tier 3 proportional",
    }
    for tier in (1, 2, 3):
        remainder = [
            item for item in ordered_categories if item.key not in named_keys and item.tier == tier
        ]
        if not remainder:
            continue
        product_amounts: dict[str, float] = defaultdict(float)
        company_amounts: dict[str, float] = defaultdict(float)
        for remainder_group in remainder:
            for product, amount in remainder_group.product_amounts.items():
                product_amounts[product] += amount
            for ticker, amount in remainder_group.company_amounts.items():
                company_amounts[ticker] += amount
        groups.append(
            _CreditFlowGroup(
                key=f"other-tier-{tier}",
                label=remainder_labels[tier],
                tier=tier,
                product_amounts=dict(product_amounts),
                company_amounts=dict(company_amounts),
            )
        )
    groups.sort(key=lambda item: (-item.total, item.key))

    top_companies = tuple(
        sorted(result.companies, key=lambda item: (-item.evidence_lift, item.ticker))[:2]
    )
    top_tickers = tuple(item.ticker for item in top_companies)
    product_keys = tuple(item.debt_product for item in bundle.flows)
    product_totals = {
        product: sum(item.product_amounts.get(product, 0.0) for item in groups)
        for product in product_keys
    }
    company_totals = {
        ticker: sum(item.company_amounts.get(ticker, 0.0) for item in groups)
        for ticker in top_tickers
    }
    company_totals["all-other"] = sum(
        amount
        for item in groups
        for ticker, amount in item.company_amounts.items()
        if ticker not in top_tickers
    )

    product_centers = _flow_centers(len(product_keys), 90.0)
    category_centers = _flow_centers(len(groups), 30.0)
    company_keys = (*top_tickers, "all-other")
    company_centers = _flow_centers(len(company_keys), 42.0)
    product_nodes = tuple(
        CreditFlowNode(
            key=key,
            label_lines=(FLOW_PRODUCT_LABELS.get(key, key.replace("_", " ").capitalize()),),
            amount_display=_flow_amount(product_totals[key]),
            x=center - 58.0,
            y=30.0,
            width=116.0,
            height=52.0,
        )
        for key, center in zip(product_keys, product_centers, strict=True)
    )
    category_nodes = tuple(
        CreditFlowNode(
            key=item.key,
            label_lines=_flow_label_lines(item.label),
            amount_display=_flow_amount(item.total),
            x=center - 27.0,
            y=245.0,
            width=54.0,
            height=62.0,
        )
        for item, center in zip(groups, category_centers, strict=True)
    )
    lift_by_ticker = {item.ticker: item.evidence_lift for item in top_companies}
    company_nodes = tuple(
        CreditFlowNode(
            key=key,
            label_lines=(
                (key, f"{lift_by_ticker[key]:.2f}x lift")
                if key != "all-other"
                else ("All other", "covered")
            ),
            amount_display=_flow_amount(company_totals[key]),
            x=center - 38.0,
            y=505.0,
            width=76.0,
            height=62.0,
        )
        for key, center in zip(company_keys, company_centers, strict=True)
    )

    product_raw = [
        (
            product,
            group.key,
            amount,
            group.tier,
            FLOW_PRODUCT_LABELS.get(product, product),
            group.label,
        )
        for group in groups
        for product, amount in group.product_amounts.items()
        if amount > 0
    ]
    company_raw = [
        (
            group.key,
            ticker,
            (
                group.company_amounts.get(ticker, 0.0)
                if ticker != "all-other"
                else sum(
                    amount
                    for company_ticker, amount in group.company_amounts.items()
                    if company_ticker not in top_tickers
                )
            ),
            group.tier,
            group.label,
            (ticker if ticker != "all-other" else "All other covered companies"),
        )
        for group in groups
        for ticker in company_keys
    ]
    company_raw = [item for item in company_raw if item[2] > 0]
    all_amounts = [item[2] for item in (*product_raw, *company_raw)]
    if not all_amounts:
        raise SitePublishError("Flow view has no positive published attribution lanes")
    scale = 24.0 / max(all_amounts)
    product_links = _flow_links(
        product_raw,
        product_nodes,
        category_nodes,
        source_y=82.0,
        target_y=245.0,
        scale=scale,
    )
    company_links = _flow_links(
        company_raw,
        category_nodes,
        company_nodes,
        source_y=307.0,
        target_y=505.0,
        scale=scale,
    )

    rows = tuple(
        CreditFlowRow(
            lane=" + ".join(
                FLOW_PRODUCT_LABELS.get(product, product)
                for product in product_keys
                if item.product_amounts.get(product, 0.0) > 0
            ),
            category=item.label,
            tier=item.tier,
            amount_display=_flow_amount(item.total),
            destinations="; ".join(
                f"{(ticker if ticker != 'all-other' else 'All other')} {_flow_amount(amount)}"
                for ticker, amount in (
                    *((ticker, item.company_amounts.get(ticker, 0.0)) for ticker in top_tickers),
                    (
                        "all-other",
                        sum(
                            amount
                            for ticker, amount in item.company_amounts.items()
                            if ticker not in top_tickers
                        ),
                    ),
                )
                if amount > 0
            ),
        )
        for item in groups
    )
    node_count = len(product_nodes) + len(category_nodes) + len(company_nodes)
    if node_count > 9:
        raise SitePublishError(f"Flow view exceeds the 9-node readability cap: {node_count}")
    return CreditFlowView(
        period=result.quarter,
        node_count=node_count,
        product_nodes=product_nodes,
        category_nodes=category_nodes,
        company_nodes=company_nodes,
        product_links=product_links,
        company_links=company_links,
        rows=rows,
        top_company_labels=", ".join(top_tickers),
    )


def _flow_links(
    raw: list[tuple[str, str, float, int, str, str]],
    source_nodes: tuple[CreditFlowNode, ...],
    target_nodes: tuple[CreditFlowNode, ...],
    *,
    source_y: float,
    target_y: float,
    scale: float,
) -> tuple[CreditFlowLink, ...]:
    source_centers = {item.key: item.x + item.width / 2.0 for item in source_nodes}
    target_centers = {item.key: item.x + item.width / 2.0 for item in target_nodes}
    source_slots = _flow_slots(
        raw,
        group_index=0,
        other_index=1,
        group_centers=source_centers,
        other_centers=target_centers,
        scale=scale,
    )
    target_slots = _flow_slots(
        raw,
        group_index=1,
        other_index=0,
        group_centers=target_centers,
        other_centers=source_centers,
        scale=scale,
    )
    midpoint = (source_y + target_y) / 2.0
    links = []
    for index, (_, _, amount, tier, source_label, target_label) in enumerate(raw):
        start = source_slots[index]
        end = target_slots[index]
        links.append(
            CreditFlowLink(
                path=(
                    f"M {start:.2f} {source_y:.2f} C {start:.2f} {midpoint:.2f} "
                    f"{end:.2f} {midpoint:.2f} {end:.2f} {target_y:.2f}"
                ),
                tier=tier,
                stroke_width=f"{amount * scale:.3f}",
                amount_millions=amount,
                amount_display=_flow_amount(amount),
                source_label=source_label,
                target_label=target_label,
            )
        )
    return tuple(links)


def _flow_slots(
    raw: list[tuple[str, str, float, int, str, str]],
    *,
    group_index: int,
    other_index: int,
    group_centers: dict[str, float],
    other_centers: dict[str, float],
    scale: float,
) -> dict[int, float]:
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(raw):
        grouped[cast(str, item[group_index])].append(index)
    slots: dict[int, float] = {}
    for key, indexes in grouped.items():
        ordered = sorted(
            indexes,
            key=lambda index: (other_centers[cast(str, raw[index][other_index])], index),
        )
        widths = [raw[index][2] * scale for index in ordered]
        cursor = group_centers[key] - sum(widths) / 2.0
        for index, width in zip(ordered, widths, strict=True):
            slots[index] = cursor + width / 2.0
            cursor += width
    return slots


def _flow_centers(count: int, margin: float) -> tuple[float, ...]:
    if count <= 0:
        raise SitePublishError("Flow view requires at least one node per layer")
    if count == 1:
        return (180.0,)
    step = (360.0 - margin * 2.0) / (count - 1)
    return tuple(margin + index * step for index in range(count))


def _flow_label_lines(label: str) -> tuple[str, ...]:
    words = label.split()
    if len(label) <= 12 or len(words) == 1:
        return (label,)
    split = max(1, len(words) // 2)
    return (" ".join(words[:split]), " ".join(words[split:]))


def _flow_amount(value: float) -> str:
    return f"${value:,.0f}M"


def _methodology_comparison(
    original: AttributionResult,
    prior: AttributionResult,
    current: AttributionResult,
) -> list[dict[str, object]]:
    original_by_ticker = {item.ticker: item for item in original.companies}
    current_by_ticker = {item.ticker: item for item in current.companies}
    rows: list[dict[str, object]] = []
    for item in prior.companies:
        current_item = current_by_ticker[item.ticker]
        original_item = original_by_ticker[item.ticker]
        rows.append(
            {
                "ticker": item.ticker,
                "company_name": item.company_name,
                "original_low": f"{original_item.estimated_dfr_pct_low:.2f}%",
                "original_mid": f"{original_item.estimated_dfr_pct_mid:.2f}%",
                "original_high": f"{original_item.estimated_dfr_pct_high:.2f}%",
                "prior_low": f"{item.estimated_dfr_pct_low:.2f}%",
                "prior_mid": f"{item.estimated_dfr_pct_mid:.2f}%",
                "prior_high": f"{item.estimated_dfr_pct_high:.2f}%",
                "current_low": f"{current_item.estimated_dfr_pct_low:.2f}%",
                "current_mid": f"{current_item.estimated_dfr_pct_mid:.2f}%",
                "current_high": f"{current_item.estimated_dfr_pct_high:.2f}%",
                "delta_mid": (
                    f"{current_item.estimated_dfr_pct_mid - item.estimated_dfr_pct_mid:+.2f} pp"
                ),
            }
        )
    return rows


def _source_degradation_display(item: SourceDegradation) -> dict[str, object]:
    return {
        "assumption_id": item.assumption_id,
        "active_source_id": item.active_source_id,
        "reason": item.reason,
        "band_multiplier": f"{item.band_multiplier:.2f}x",
    }


def _assumption_display(
    item: Assumption,
    degradations: tuple[SourceDegradation, ...],
) -> dict[str, object]:
    degradation = next(
        (entry for entry in degradations if entry.assumption_id == item.assumption_id),
        None,
    )
    return {
        "assumption_id": item.assumption_id,
        "statement": item.statement,
        "prior": f"{item.prior.low:g} / {item.prior.mid:g} / {item.prior.high:g}",
        "tier": item.tier,
        "source_url": item.source_url,
        "evidence_snippet": item.evidence_snippet,
        "sensitivity_note": item.sensitivity_note,
        "version": item.version,
        "primary_source_id": item.primary_source_id,
        "fallback_source_ids": item.fallback_source_ids,
        "active_source_id": (
            degradation.active_source_id if degradation is not None else item.primary_source_id
        ),
        "source_status": "DEGRADED" if degradation is not None else "PRIMARY",
        "source_note": degradation.reason if degradation is not None else "",
        "criticality_rating": item.criticality_rating,
        "criticality_dependency_share": f"{item.criticality_dependency_share * 100:.1f}%",
        "review_status": item.review_status,
    }


def _feed_schema(common: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": "v1",
        "methodology_version": common["methodology_version"],
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "commercial_license_contact": common["commercial_license_contact"],
        "feeds": {
            "nowcast_predictions": {
                "formats": ["csv", "json", "parquet"],
                "columns": _column_docs(_prediction_columns()),
            },
            "scoreboard": {
                "formats": ["csv", "json"],
                "columns": _column_docs(_scoreboard_columns()),
                "metadata": {
                    "live_calibration": (
                        "Compatibility view of live grades in the pre-MTS G.19-only aggregate. "
                        "It excludes every Treasury series; new consumers should use the "
                        "per-series field."
                    ),
                    "live_calibration_by_series": (
                        "Per-series running coverage and error statistics computed only from "
                        "live grades; no calibration statistic blends target series. "
                        "the naive comparator is refit using first prints available when each "
                        "prediction was recorded."
                    ),
                },
                "invariants": [
                    {
                        "fields": ["status", "grade_status"],
                        "rule": "status equals grade_status for every scoreboard row",
                    }
                ],
            },
            "dfri_companies": {
                "formats": ["csv", "json", "parquet"],
                "columns": _column_docs(_company_columns()),
            },
            "assumptions": {
                "formats": ["csv", "json"],
                "columns": _column_docs(_assumption_columns()),
            },
            "coverage_exclusions": {
                "formats": ["csv", "json"],
                "columns": _column_docs(_exclusion_columns()),
            },
            "quarterly_refreshes": {
                "formats": ["json"],
                "columns": _column_docs(_refresh_columns()),
            },
            "dfri_company_history": {
                "formats": ["json"],
                "columns": _column_docs(_company_history_columns()),
            },
        },
    }


def _feed_schema_v2(common: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": "v2",
        "predecessor_schema_url": "/v1/feeds/schema.json",
        "methodology_version": common["methodology_version"],
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "commercial_license_contact": common["commercial_license_contact"],
        "derived_metrics": {
            "evidence_lift": (
                "Company DFR% midpoint divided by its same-period pure-fungibility "
                "counterfactual midpoint."
            ),
            "fungibility_baseline_dfr_pct_mid": (
                "DFR% midpoint from broad proportional allocation lanes after excluding "
                "company-specific financing and auto-category evidence."
            ),
        },
        "feeds": {
            "dfri_companies": {
                "formats": ["csv", "json", "parquet"],
                "columns": _column_docs(_company_columns_v2()),
                "metadata": {
                    "source_status": "PRIMARY or DEGRADED for the active build",
                    "source_degradations": (
                        "Active fallback IDs, reasons, multipliers, and effective assumption bands"
                    ),
                },
            },
            "assumptions": {
                "formats": ["csv", "json"],
                "columns": _column_docs(_assumption_columns_v2()),
                "metadata": {
                    "review_status": (
                        "APPROVED for new numerical mappings; APPROVED_LEGACY for assumptions "
                        "accepted before the explicit review-status contract."
                    )
                },
            },
        },
    }


def _prediction_columns() -> list[str]:
    return [
        "prediction_id",
        "made_at",
        "model_version",
        "inputs_hash",
        "target_series",
        "target_period",
        "point",
        "low80",
        "high80",
        "low95",
        "high95",
        "status",
        "permalink",
        "methodology_version",
        "data_vintage",
        "published_at",
        "publication_mode",
        "license",
        "license_url",
        "commercial_license_contact",
    ]


def _scoreboard_columns() -> list[str]:
    return [
        *_prediction_columns(),
        "grade_status",
        "actual_first_print",
        "vintage_url",
        "abs_error",
        "graded_at",
    ]


def _company_columns() -> list[str]:
    return [
        "ticker",
        "company_name",
        "quarter",
        "estimated_dfr_pct_low",
        "estimated_dfr_pct_mid",
        "estimated_dfr_pct_high",
        "estimated_debt_funded_revenue_mid_millions",
        "estimated_us_consumer_revenue_mid_millions",
        "tier1_share",
        "tier2_share",
        "tier3_share",
        "revenue_source_url",
        "tier1_source_url",
        "tier1_excerpt",
        "assumption_ids",
        "sensitivity_top5",
        "permalink",
        "methodology_version",
        "data_vintage",
        "published_at",
        "publication_mode",
        "license",
        "license_url",
        "commercial_license_contact",
    ]


def _company_columns_v2() -> list[str]:
    columns = _company_columns()
    insertion = columns.index("tier1_share")
    return [
        *columns[:insertion],
        "fungibility_baseline_dfr_pct_mid",
        "evidence_lift",
        "evidence_lift_status",
        "evidence_lift_headline",
        *columns[insertion:],
    ]


def _assumption_columns() -> list[str]:
    return [
        "assumption_id",
        "statement",
        "value_low",
        "value_mid",
        "value_high",
        "tier",
        "company_count",
        "updated_company_count",
        "source_url",
        "evidence_snippet",
        "sensitivity_note",
        "version",
        "active",
        "methodology_version",
        "data_vintage",
        "published_at",
        "publication_mode",
        "license",
        "license_url",
        "commercial_license_contact",
    ]


def _assumption_columns_v2() -> list[str]:
    columns = _assumption_columns()
    insertion = columns.index("active") + 1
    return [*columns[:insertion], "review_status", *columns[insertion:]]


def _exclusion_columns() -> list[str]:
    return [
        "ticker",
        "company_name",
        "cik",
        "gics_sub_industry",
        "reason",
        "membership_snapshot_ref",
        "verified_at",
        "methodology_version",
        "data_vintage",
        "published_at",
        "publication_mode",
        "license",
        "license_url",
        "commercial_license_contact",
    ]


def _refresh_columns() -> list[str]:
    return [
        "refresh_id",
        "target_quarter",
        "effective_at",
        "data_vintage",
        "methodology_version",
        "source_hash",
        "company_count",
        "updated_company_count",
        "estimated_dfr_pct_low",
        "estimated_dfr_pct_mid",
        "estimated_dfr_pct_high",
        "weighting",
        "license",
        "license_url",
        "commercial_license_contact",
    ]


def _company_history_columns() -> list[str]:
    return [
        "refresh_id",
        "target_quarter",
        "effective_at",
        "data_vintage",
        "methodology_version",
        "source_hash",
        "ticker",
        "company_name",
        "input_status",
        "estimated_dfr_pct_low",
        "estimated_dfr_pct_mid",
        "estimated_dfr_pct_high",
        "tier1_share",
        "tier2_share",
        "tier3_share",
        "license",
        "license_url",
        "commercial_license_contact",
    ]


def _column_docs(columns: list[str]) -> list[dict[str, str]]:
    numeric = {
        "point",
        "low80",
        "high80",
        "low95",
        "high95",
        "actual_first_print",
        "abs_error",
        "estimated_dfr_pct_low",
        "estimated_dfr_pct_mid",
        "estimated_dfr_pct_high",
        "estimated_debt_funded_revenue_mid_millions",
        "estimated_us_consumer_revenue_mid_millions",
        "fungibility_baseline_dfr_pct_mid",
        "evidence_lift",
        "tier1_share",
        "tier2_share",
        "tier3_share",
        "value_low",
        "value_mid",
        "value_high",
        "tier",
    }
    return [
        {"name": item, "type": "number|null" if item in numeric else "string|null"}
        for item in columns
    ]


def _prediction_schema() -> pa.Schema:
    numeric = {"point", "low80", "high80", "low95", "high95"}
    return pa.schema(
        [
            pa.field(item, pa.float64() if item in numeric else pa.string(), nullable=False)
            for item in _prediction_columns()
        ]
    )


def _company_schema() -> pa.Schema:
    numeric = {
        "estimated_dfr_pct_low",
        "estimated_dfr_pct_mid",
        "estimated_dfr_pct_high",
        "estimated_debt_funded_revenue_mid_millions",
        "estimated_us_consumer_revenue_mid_millions",
        "tier1_share",
        "tier2_share",
        "tier3_share",
    }
    return pa.schema(
        [
            pa.field(item, pa.float64() if item in numeric else pa.string(), nullable=False)
            for item in _company_columns()
        ]
    )


def _company_schema_v2() -> pa.Schema:
    numeric = {
        "estimated_dfr_pct_low",
        "estimated_dfr_pct_mid",
        "estimated_dfr_pct_high",
        "estimated_debt_funded_revenue_mid_millions",
        "estimated_us_consumer_revenue_mid_millions",
        "fungibility_baseline_dfr_pct_mid",
        "evidence_lift",
        "tier1_share",
        "tier2_share",
        "tier3_share",
    }
    return pa.schema(
        [
            pa.field(item, pa.float64() if item in numeric else pa.string(), nullable=False)
            for item in _company_columns_v2()
        ]
    )


def _write_parquet(path: Path, rows: list[dict[str, object]], schema: pa.Schema) -> None:
    table = pa.Table.from_pylist(rows, schema=schema)
    write_deterministic_parquet(path, table)


def _write_csv(path: Path, rows: list[dict[str, object]], columns: list[str]) -> None:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write(path, stream.getvalue().encode())


def _write_json(path: Path, payload: object) -> None:
    _atomic_write(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())


def _page_metadata(
    site_url: str, page_path: str, image_path: str, image_alt: str
) -> dict[str, str]:
    base = site_url.rstrip("/") + "/"
    return {
        "canonical_url": f"{base}{page_path}",
        "social_image_url": f"{base}{image_path}",
        "social_image_alt": image_alt,
    }


def _render_social_images(
    directory: Path,
    *,
    aggregate: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
    companies: Sequence[Mapping[str, object]],
    changelog_count: int,
    exclusion_count: int,
) -> None:
    cards = {
        "home.png": SocialCard(
            label=f"{aggregate['quarter']} · estimated share of U.S. consumer revenue",
            title="Debt-Funded Revenue Index",
            figure=str(aggregate["mid"]),
            units=f"80% band {aggregate['low']} to {aggregate['high']}",
            detail=(
                f"Revenue-weighted estimate across {aggregate['company_count']} covered companies."
            ),
        ),
        "companies.png": SocialCard(
            label="Current coverage",
            title="Covered companies",
            figure=str(len(companies)),
            units="company estimates with visible ranges",
            detail="Every estimate carries evidence tiers, assumption IDs, and provenance links.",
        ),
        "scoreboard.png": SocialCard(
            label="Immutable prediction ledger",
            title="Predictions and first-print grades",
            figure=str(len(rows)),
            units="versioned prediction records",
            detail=(
                "Original timestamps, uncertainty bands, model versions, and grades "
                "remain retrievable."
            ),
        ),
        "methodology.png": SocialCard(
            label="Current evidence method",
            title="DFRI methodology",
            figure=METHODOLOGY_VERSION,
            units="versioned methodology",
            detail=(
                "Point-in-time nowcasting and evidence-linked attribution with explicit "
                "uncertainty."
            ),
        ),
        "sensitivity.png": SocialCard(
            label="Methodology sensitivity",
            title="Current revenue-weighted estimate",
            figure=str(aggregate["mid"]),
            units=f"80% band {aggregate['low']} to {aggregate['high']}",
            detail="Prior methodology values remain preserved for direct, immutable comparison.",
        ),
        "coverage.png": SocialCard(
            label="Dated coverage boundary",
            title="Included and excluded companies",
            figure=f"{len(companies)} / {exclusion_count}",
            units="included / explicitly excluded",
            detail=(
                "Coverage decisions publish dated reasons instead of silently dropping companies."
            ),
        ),
        "changelog.png": SocialCard(
            label="Append-only publication history",
            title="DFRI changelog",
            figure=str(changelog_count),
            units="versioned public entries",
            detail=(
                "Restatements, source fallbacks, grades, and methodology changes are never silent."
            ),
        ),
    }
    for filename, card in cards.items():
        render_social_image(directory / filename, card)
    for company in companies:
        ticker = str(company["ticker"]).lower()
        render_social_image(
            directory / f"company-{ticker}.png",
            SocialCard(
                label=f"{company['quarter']} · estimated DFR%",
                title=f"{company['company_name']} ({company['ticker']})",
                figure=str(company["mid"]),
                units=f"80% band {company['low']} to {company['high']}",
                detail=(
                    f"Evidence Lift {company['evidence_lift']}; tier shares "
                    f"T1 {company['tier1']}, T2 {company['tier2']}, T3 {company['tier3']}."
                ),
            ),
        )
    for row in rows:
        graded = row["grade_status"] == "Graded"
        render_social_image(
            directory / f"prediction-{row['prediction_id']}.png",
            SocialCard(
                label=f"{row['target_period']} · {row['source_family']}",
                title=str(row["target_label"]),
                figure=str(row["point_display"]),
                units=(f"$M point; 80% band {row['low80_display']} to {row['high80_display']}"),
                detail=(
                    f"95% band {row['low95_display']} to {row['high95_display']}. "
                    f"Status: {row['grade_status']}."
                ),
                verified=graded,
            ),
        )


def _render(
    environment: Environment, template: str, path: Path, context: dict[str, object]
) -> None:
    rendered = environment.get_template(template).render(**context)
    compact = "\n".join(line.strip() for line in rendered.splitlines() if line.strip()) + "\n"
    _atomic_write(path, compact.encode())


def _copy(source: Path, destination: Path) -> None:
    content = source.read_text(encoding="utf-8").replace("\r\n", "\n")
    _atomic_write(destination, content.encode())


def _copy_stylesheet(source: Path, destination: Path) -> None:
    content = source.read_text(encoding="utf-8").replace("\r\n", "\n")
    compact = " ".join(content.split()) + "\n"
    _atomic_write(destination, compact.encode())


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _promote_directory(staging: Path, destination: Path) -> None:
    backup = destination.with_name(f".{destination.name}.previous-{uuid.uuid4().hex}")
    had_destination = destination.exists()
    if had_destination:
        destination.replace(backup)
    try:
        staging.replace(destination)
    except Exception:
        if had_destination and backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    if had_destination:
        shutil.rmtree(backup)


def _load_object(path: Path, label: str) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SitePublishError(f"{label} must be an object")
    return cast(dict[str, object], payload)


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value: float) -> str:
    return f"{value:,.0f}"


def _rounded_tens(value: float) -> str:
    return f"{round(value, -1):,.0f}"


def _optional_number(value: float | None) -> str:
    return _number(value) if value is not None else "—"


def _percentage(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"


def _aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SitePublishError(f"{label} must be timezone-aware")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("Timestamp must include a timezone")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--ledger-root", type=Path, default=Path(".local/lake/curated"))
    parser.add_argument("--output", type=Path, default=Path("published/public"))
    parser.add_argument("--published-at", type=_parse_timestamp)
    parser.add_argument("--data-vintage", type=_parse_timestamp, required=True)
    parser.add_argument("--publication-mode", choices=("preview", "live"), default="preview")
    parser.add_argument("--minimum-made-at", type=_parse_timestamp)
    args = parser.parse_args()
    receipt = publish_scoreboard(
        AppendOnlyParquetStore(args.ledger_root),
        args.output,
        raw_store=AppendOnlyParquetStore(args.raw_root),
        published_at=args.published_at or datetime.now(UTC),
        data_vintage=args.data_vintage,
        publication_mode=args.publication_mode,
        minimum_made_at=args.minimum_made_at,
    )
    print(json.dumps({**receipt.__dict__, "output_root": str(receipt.output_root)}, sort_keys=True))


if __name__ == "__main__":
    main()
