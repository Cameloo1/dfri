"""Strict curated-layer schemas from DFRI build specification section 5."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

import pyarrow as pa

UTC_TIMESTAMP: Final = pa.timestamp("us", tz="UTC")
STRING_LIST: Final = pa.list_(pa.string())


def _field(name: str, data_type: pa.DataType, *, nullable: bool = False) -> pa.Field:
    return pa.field(name, data_type, nullable=nullable)


TABLE_SCHEMAS: Final[Mapping[str, pa.Schema]] = {
    "series_registry": pa.schema(
        [
            _field("series_id", pa.string()),
            _field("source", pa.string()),
            _field("expected_title", pa.string()),
            _field("units", pa.string()),
            _field("frequency", pa.string()),
            _field("license_note", pa.string()),
            _field("verified_at", UTC_TIMESTAMP, nullable=True),
        ]
    ),
    "raw_observations": pa.schema(
        [
            _field("source", pa.string()),
            _field("series_id", pa.string()),
            _field("obs_period", pa.date32()),
            _field("value", pa.float64()),
            _field("unit", pa.string()),
            _field("release_date", UTC_TIMESTAMP),
            _field("vintage_date", pa.date32()),
            _field("ingested_at", UTC_TIMESTAMP),
            _field("source_url", pa.string()),
            _field("checksum", pa.string()),
        ]
    ),
    "releases_calendar": pa.schema(
        [
            _field("release_name", pa.string()),
            _field("expected_at", UTC_TIMESTAMP, nullable=True),
            _field("actual_at", UTC_TIMESTAMP, nullable=True),
            _field("status", pa.string()),
        ]
    ),
    "predictions": pa.schema(
        [
            _field("prediction_id", pa.string()),
            _field("made_at", UTC_TIMESTAMP),
            _field("model_version", pa.string()),
            _field("inputs_hash", pa.string()),
            _field("target_series", pa.string()),
            _field("target_period", pa.date32()),
            _field("point", pa.float64()),
            _field("low80", pa.float64()),
            _field("high80", pa.float64()),
            _field("low95", pa.float64()),
            _field("high95", pa.float64()),
            _field("status", pa.string()),
        ]
    ),
    "grades": pa.schema(
        [
            _field("prediction_id", pa.string()),
            _field("actual_first_print", pa.float64()),
            _field("vintage_url", pa.string()),
            _field("abs_error", pa.float64()),
            _field("graded_at", UTC_TIMESTAMP),
        ]
    ),
    "publication_records": pa.schema(
        [
            _field("prediction_id", pa.string()),
            _field("published_at", UTC_TIMESTAMP),
            _field("data_vintage", UTC_TIMESTAMP),
            _field("methodology_version", pa.string()),
        ]
    ),
    "assumptions": pa.schema(
        [
            _field("assumption_id", pa.string()),
            _field("statement", pa.string()),
            _field("value_or_prior", pa.string()),
            _field("tier", pa.int8()),
            _field("source_url", pa.string()),
            _field("evidence_snippet", pa.string()),
            _field("sensitivity_note", pa.string()),
            _field("version", pa.string()),
            _field("active", pa.bool_()),
        ]
    ),
    "matrix_a": pa.schema(
        [
            _field("version", pa.string()),
            _field("debt_product", pa.string()),
            _field("spend_category", pa.string()),
            _field("weight_low", pa.float64()),
            _field("weight_mid", pa.float64()),
            _field("weight_high", pa.float64()),
            _field("tier", pa.int8()),
            _field("assumption_ids", STRING_LIST),
        ]
    ),
    "matrix_b": pa.schema(
        [
            _field("version", pa.string()),
            _field("spend_category", pa.string()),
            _field("ticker", pa.string()),
            _field("weight_low", pa.float64()),
            _field("weight_mid", pa.float64()),
            _field("weight_high", pa.float64()),
            _field("method", pa.string()),
            _field("evidence_refs", STRING_LIST),
        ]
    ),
    "company_facts": pa.schema(
        [
            _field("ticker", pa.string()),
            _field("cik", pa.string()),
            _field("period", pa.date32()),
            _field("revenue_total", pa.float64()),
            _field("revenue_us_consumer_est", pa.float64()),
            _field("segment_evidence_refs", STRING_LIST),
            _field("source_accessions", STRING_LIST),
        ]
    ),
    "sec_xbrl_facts": pa.schema(
        [
            _field("issuer_role", pa.string()),
            _field("ticker", pa.string()),
            _field("cik", pa.string()),
            _field("entity_name", pa.string()),
            _field("namespace", pa.string()),
            _field("tag", pa.string()),
            _field("label", pa.string(), nullable=True),
            _field("description", pa.string(), nullable=True),
            _field("unit", pa.string()),
            _field("fact_index", pa.int32()),
            _field("period_start", pa.date32(), nullable=True),
            _field("period_end", pa.date32()),
            _field("fiscal_year", pa.int32(), nullable=True),
            _field("fiscal_period", pa.string(), nullable=True),
            _field("form", pa.string()),
            _field("filed_at", pa.date32()),
            _field("accession", pa.string()),
            _field("frame", pa.string(), nullable=True),
            _field("value_json", pa.string()),
            _field("source_url", pa.string()),
            _field("source_checksum", pa.string()),
            _field("ingested_at", UTC_TIMESTAMP),
        ]
    ),
    "sec_filing_evidence": pa.schema(
        [
            _field("issuer_role", pa.string()),
            _field("ticker", pa.string()),
            _field("cik", pa.string()),
            _field("evidence_type", pa.string()),
            _field("period", pa.date32()),
            _field("filed_at", pa.date32()),
            _field("accession", pa.string()),
            _field("primary_document", pa.string()),
            _field("source_url", pa.string()),
            _field("source_checksum", pa.string()),
            _field("context", pa.string()),
            _field("evidence_snippet", pa.string()),
            _field("snippet_hash", pa.string()),
            _field("extracted_table_json", pa.string()),
            _field("ingested_at", UTC_TIMESTAMP),
        ]
    ),
    "auto_abs_aggregates": pa.schema(
        [
            _field("trust_id", pa.string()),
            _field("trust_name", pa.string()),
            _field("credit_segment", pa.string()),
            _field("cik", pa.string()),
            _field("reporting_period_start", pa.date32()),
            _field("reporting_period_end", pa.date32()),
            _field("filed_at", pa.date32()),
            _field("accession", pa.string()),
            _field("exhibit_document", pa.string()),
            _field("asset_count", pa.int64()),
            _field("core_metric_asset_count", pa.int64()),
            _field("recovery_only_asset_count", pa.int64()),
            _field("asset_added_count", pa.int64()),
            _field("asset_added_indicator_observed_count", pa.int64()),
            _field("recovered_amount_sum", pa.decimal128(38, 8)),
            _field("recovered_amount_observed_asset_count", pa.int64()),
            _field("original_loan_amount_sum", pa.decimal128(38, 8)),
            _field("asset_added_original_loan_amount_sum", pa.decimal128(38, 8)),
            _field("beginning_balance_sum", pa.decimal128(38, 8)),
            _field("ending_balance_sum", pa.decimal128(38, 8)),
            _field("weighted_avg_original_interest_rate", pa.decimal128(18, 10)),
            _field("weighted_avg_reporting_interest_rate", pa.decimal128(18, 10)),
            _field("reporting_interest_rate_asset_count", pa.int64()),
            _field("reporting_interest_rate_balance_sum", pa.decimal128(38, 8)),
            _field("weighted_avg_original_loan_term", pa.decimal128(18, 8)),
            _field("weighted_avg_remaining_term", pa.decimal128(18, 8)),
            _field("remaining_term_asset_count", pa.int64()),
            _field("remaining_term_balance_sum", pa.decimal128(38, 8)),
            _field("source_url", pa.string()),
            _field("source_checksum", pa.string()),
            _field("source_bytes", pa.int64()),
            _field("filing_index_url", pa.string()),
            _field("filing_index_checksum", pa.string()),
            _field("ingested_at", UTC_TIMESTAMP),
        ]
    ),
    "card_trust_aggregates": pa.schema(
        [
            _field("trust_id", pa.string()),
            _field("trust_name", pa.string()),
            _field("trust_cik", pa.string()),
            _field("archive_cik", pa.string()),
            _field("reporting_period_end", pa.date32()),
            _field("filed_at", pa.date32()),
            _field("accession", pa.string()),
            _field("primary_document", pa.string()),
            _field("exhibit_document", pa.string()),
            _field("ending_principal_receivables", pa.decimal128(38, 2)),
            _field("principal_payment_rate_pct", pa.decimal128(18, 6)),
            _field("payment_rate_basis", pa.string()),
            _field("portfolio_yield_pct", pa.decimal128(18, 6)),
            _field("yield_basis", pa.string()),
            _field("chargeoff_amount", pa.decimal128(38, 2), nullable=True),
            _field("chargeoff_amount_status", pa.string()),
            _field("chargeoff_rate_pct", pa.decimal128(18, 6)),
            _field("chargeoff_basis", pa.string()),
            _field("metric_evidence_json", pa.string()),
            _field("evidence_snippet_hash", pa.string()),
            _field("source_url", pa.string()),
            _field("source_checksum", pa.string()),
            _field("source_bytes", pa.int64()),
            _field("archive_index_url", pa.string()),
            _field("archive_index_checksum", pa.string()),
            _field("ingested_at", UTC_TIMESTAMP),
        ]
    ),
    "dfri_output": pa.schema(
        [
            _field("ticker", pa.string()),
            _field("period", pa.date32()),
            _field("dfr_low", pa.float64()),
            _field("dfr_mid", pa.float64()),
            _field("dfr_high", pa.float64()),
            _field("tier1_share", pa.float64()),
            _field("tier2_share", pa.float64()),
            _field("tier3_share", pa.float64()),
            _field("methodology_version", pa.string()),
            _field("data_vintage", pa.date32()),
            _field("published_at", UTC_TIMESTAMP),
        ]
    ),
    "attribution_refreshes": pa.schema(
        [
            _field("refresh_id", pa.string()),
            _field("target_quarter", pa.string()),
            _field("effective_at", UTC_TIMESTAMP),
            _field("data_vintage", UTC_TIMESTAMP),
            _field("methodology_version", pa.string()),
            _field("source_hash", pa.string()),
            _field("company_count", pa.int16()),
            _field("updated_company_count", pa.int16()),
            _field("payload_json", pa.string()),
        ]
    ),
}


class SchemaViolationError(ValueError):
    """Raised when input rows do not satisfy the registered table contract."""


def schema_for(table_name: str) -> pa.Schema:
    """Return a registered schema or fail closed for an unknown table."""

    try:
        return TABLE_SCHEMAS[table_name]
    except KeyError as exc:
        raise SchemaViolationError(f"Unknown curated table: {table_name}") from exc


def table_from_rows(table_name: str, rows: Sequence[Mapping[str, object]]) -> pa.Table:
    """Build a strict Arrow table after checking exact columns and nullability."""

    schema = schema_for(table_name)
    expected = set(schema.names)
    for index, row in enumerate(rows):
        actual = set(row)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise SchemaViolationError(
                f"{table_name} row {index} columns mismatch; missing={missing}, extra={extra}"
            )
        for field in schema:
            if not field.nullable and row[field.name] is None:
                raise SchemaViolationError(
                    f"{table_name} row {index} field {field.name!r} may not be null"
                )

    try:
        table = pa.Table.from_pylist(list(rows), schema=schema)
    except (ArrowError, TypeError, ValueError) as exc:
        raise SchemaViolationError(f"{table_name} values do not match schema: {exc}") from exc
    return table


ArrowError = pa.ArrowException
