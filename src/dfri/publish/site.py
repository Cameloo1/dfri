"""Deterministic scoreboard feeds and no-JavaScript-first static site publisher."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

import pyarrow as pa
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from dfri.attribution.engine import AttributionResult, CompanyEstimate, run_attribution
from dfri.attribution.registry import Assumption, AttributionBundle, load_attribution_bundle
from dfri.lake.store import AppendOnlyParquetStore, write_deterministic_parquet
from dfri.publish.changelog import load_changelog
from dfri.publish.ledger import (
    GradeLedger,
    GradeRecord,
    PredictionLedger,
    PredictionRecord,
    PublicationLedger,
    PublicationRecord,
    publication_record,
)

METHODOLOGY_VERSION: Final = "1.0.0"
LICENSE: Final = (
    "CC BY-NC 4.0 — free for non-commercial use with attribution; commercial licensing reserved"
)
LICENSE_URL: Final = "https://creativecommons.org/licenses/by-nc/4.0/"
TARGET_LABELS: Final = {
    "DELTA_DTCTLR.M": "Revolving credit flow",
    "DELTA_DTCTLN.M": "Nonrevolving credit flow",
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


def publish_scoreboard(
    ledger_store: AppendOnlyParquetStore,
    output_root: Path,
    *,
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
    backtest = _load_object(root / "reports" / "m2_backtest.json", "backtest")
    attribution_bundle = load_attribution_bundle()
    attribution = run_attribution(attribution_bundle)
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
    }
    _write_json(feeds / "nowcast_predictions.json", {"meta": meta, "data": prediction_rows})
    _write_csv(feeds / "nowcast_predictions.csv", prediction_rows, _prediction_columns())
    _write_parquet(feeds / "nowcast_predictions.parquet", prediction_rows, _prediction_schema())
    _write_json(feeds / "scoreboard.json", {"meta": meta, "data": scoreboard_rows})
    _write_csv(feeds / "scoreboard.csv", scoreboard_rows, _scoreboard_columns())
    company_rows = _company_feed_rows(
        attribution,
        publication_mode=publication_mode,
        commercial_contact=commercial_contact,
    )
    assumption_rows = _assumption_feed_rows(
        attribution_bundle,
        publication_mode=publication_mode,
        commercial_contact=commercial_contact,
    )
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
    assumption_meta = {
        **attribution_meta,
        "row_count": len(assumption_rows),
    }
    _write_json(feeds / "assumptions.json", {"meta": assumption_meta, "data": assumption_rows})
    _write_csv(feeds / "assumptions.csv", assumption_rows, _assumption_columns())
    _write_json(feeds / "schema.json", _feed_schema(build_meta))

    assets = output_root / "assets"
    _copy(root / "site" / "static" / "site.css", assets / "site.css")
    _copy(root / "site" / "static" / "site.js", assets / "site.js")
    display_rows = [
        _display_row(item, grade_by_id.get(item.prediction_id))
        for item in sorted(
            predictions,
            key=lambda row: (row.made_at, row.prediction_id),
            reverse=True,
        )
    ]
    summary = _summary(display_rows, backtest)
    environment = Environment(
        loader=FileSystemLoader(root / "site" / "templates"),
        autoescape=select_autoescape(("html",)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    base_context = {
        "brand": brand,
        "publication_mode": publication_mode,
        "excluded_count": excluded_count,
        "methodology_version": METHODOLOGY_VERSION,
        "data_vintage": build_meta["data_vintage"],
        "published_at": build_meta["published_at"],
        "feed_license_url": LICENSE_URL,
        "commercial_license_contact": commercial_contact,
    }
    company_displays = [_company_display(item) for item in attribution.companies]
    aggregate_display = _aggregate_display(attribution)
    _render(
        environment,
        "home.html",
        output_root / "index.html",
        {
            **base_context,
            "root": "",
            "title": "Immutable consumer-credit nowcasts",
            "description": "DFRI predictions and first-print Federal Reserve G.19 grades.",
            "latest": display_rows[0] if display_rows else None,
            "summary": summary,
            "aggregate": aggregate_display,
            "companies": company_displays,
        },
    )
    _render(
        environment,
        "scoreboard.html",
        output_root / "scoreboard" / "index.html",
        {
            **base_context,
            "root": "../",
            "title": "Scoreboard",
            "description": "Every immutable DFRI prediction and first-print grade.",
            "rows": display_rows,
        },
    )
    _render(
        environment,
        "methodology.html",
        output_root / "methodology" / "index.html",
        {
            **base_context,
            "root": "../",
            "title": "Methodology",
            "description": "Point-in-time DFRI nowcast and attribution methodology.",
            "assumptions": [_assumption_display(item) for item in attribution_bundle.assumptions],
            "matrix_a": attribution_bundle.matrix_a,
            "matrix_b": attribution_bundle.matrix_b,
        },
    )
    _render(
        environment,
        "changelog.html",
        output_root / "changelog" / "index.html",
        {
            **base_context,
            "root": "../",
            "title": "Changelog",
            "description": "Append-only DFRI publication and methodology changes.",
            "entries": [item.display() for item in reversed(changelog)],
        },
    )
    for company, display in zip(attribution.companies, company_displays, strict=True):
        _render(
            environment,
            "company.html",
            output_root / "companies" / company.ticker.lower() / "index.html",
            {
                **base_context,
                "root": "../../",
                "title": f"{company.company_name} ({company.ticker})",
                "description": (f"Estimated debt-funded revenue share for {company.company_name}."),
                "company": display,
            },
        )
    for row in display_rows:
        _render(
            environment,
            "prediction.html",
            output_root / "scoreboard" / "predictions" / str(row["prediction_id"]) / "index.html",
            {
                **base_context,
                "root": "../../../",
                "title": f"Prediction {row['prediction_id']}",
                "description": (
                    "Immutable DFRI prediction, uncertainty bands, and first-print grade."
                ),
                "row": row,
            },
        )
    files = sorted(
        path for path in output_root.rglob("*") if path.is_file() and path.name != "manifest.json"
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
    return {
        **_prediction_feed_row(prediction, common),
        "grade_status": "GRADED" if grade else "PENDING_FIRST_PRINT",
        "actual_first_print": grade.actual_first_print if grade else None,
        "vintage_url": grade.vintage_url if grade else None,
        "abs_error": grade.abs_error if grade else None,
        "graded_at": grade.graded_at.astimezone(UTC).isoformat() if grade else None,
    }


def _display_row(prediction: PredictionRecord, grade: GradeRecord | None) -> dict[str, object]:
    label = TARGET_LABELS.get(prediction.target_series)
    if label is None:
        raise SitePublishError(f"No public target label for {prediction.target_series}")
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
    }


def _assumption_display(item: Assumption) -> dict[str, object]:
    return {
        "assumption_id": item.assumption_id,
        "statement": item.statement,
        "prior": f"{item.prior.low:g} / {item.prior.mid:g} / {item.prior.high:g}",
        "tier": item.tier,
        "source_url": item.source_url,
        "evidence_snippet": item.evidence_snippet,
        "sensitivity_note": item.sensitivity_note,
        "version": item.version,
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
            },
            "dfri_companies": {
                "formats": ["csv", "json", "parquet"],
                "columns": _column_docs(_company_columns()),
            },
            "assumptions": {
                "formats": ["csv", "json"],
                "columns": _column_docs(_assumption_columns()),
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


def _assumption_columns() -> list[str]:
    return [
        "assumption_id",
        "statement",
        "value_low",
        "value_mid",
        "value_high",
        "tier",
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


def _render(
    environment: Environment, template: str, path: Path, context: dict[str, object]
) -> None:
    _atomic_write(path, environment.get_template(template).render(**context).encode())


def _copy(source: Path, destination: Path) -> None:
    _atomic_write(destination, source.read_bytes())


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
        published_at=args.published_at or datetime.now(UTC),
        data_vintage=args.data_vintage,
        publication_mode=args.publication_mode,
        minimum_made_at=args.minimum_made_at,
    )
    print(json.dumps({**receipt.__dict__, "output_root": str(receipt.output_root)}, sort_keys=True))


if __name__ == "__main__":
    main()
