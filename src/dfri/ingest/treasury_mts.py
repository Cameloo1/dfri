"""Point-in-time Monthly Treasury Statement first-print ingestion."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, cast
from zoneinfo import ZoneInfo

import polars as pl

from dfri.ingest.http import HttpReceipt, HttpTransport
from dfri.ingest.registry import TreasuryMtsDefinition, load_treasury_mts
from dfri.lake.store import AppendOnlyParquetStore, WriteReceipt

DOLLARS_PER_MILLION: Final = Decimal("1000000")
REQUIRED_API_FIELDS: Final = {
    "record_date",
    "parent_id",
    "classification_id",
    "classification_desc",
    "current_month_gross_outly_amt",
    "current_month_dfct_sur_amt",
    "table_nbr",
    "data_type_cd",
    "record_type_cd",
}


class TreasuryMtsError(RuntimeError):
    """MTS source data cannot satisfy its pinned first-print contract."""


@dataclass(frozen=True)
class TreasuryMtsReceipt:
    rows: int
    periods: int
    first_period: date
    last_period: date
    skipped_unverified_periods: tuple[str, ...]
    appended_periods: int
    already_present_periods: int
    writes: tuple[WriteReceipt, ...]


class TreasuryMtsClient:
    """Fetch the official Table 1 API and dated issue PDFs."""

    def __init__(
        self,
        transport: HttpTransport,
        definition: TreasuryMtsDefinition | None = None,
    ) -> None:
        self._transport = transport
        self.definition = definition or load_treasury_mts()

    def fetch_table_1(self, start: date) -> HttpReceipt:
        return self._transport.get(
            self.definition.api_url,
            params={
                "filter": f"record_date:gte:{start.isoformat()}",
                "page[size]": 10000,
            },
        )

    def fetch_metadata(self) -> HttpReceipt:
        return self._transport.get(self.definition.metadata_url)

    def fetch_issue(self, period: date) -> HttpReceipt:
        url = self.definition.archive_url_pattern.format(period=period.strftime("%Y%m"))
        receipt = self._transport.get(url)
        if not receipt.content.startswith(b"%PDF-"):
            raise TreasuryMtsError(f"Treasury MTS issue is not a PDF: {receipt.source_url}")
        return receipt


def ingest_mts_history(
    store: AppendOnlyParquetStore,
    client: TreasuryMtsClient,
    *,
    start: date,
    end: date | None = None,
) -> TreasuryMtsReceipt:
    """Persist only target months with a verified first-print release timestamp."""

    definition = client.definition
    _validate_dataset_metadata(client.fetch_metadata(), definition)
    api_receipt = client.fetch_table_1(start)
    rows, data_types = _parse_api(api_receipt)
    _validate_api_contract(rows, data_types, definition)
    if end is None:
        released_periods = [
            period
            for period, release_date in definition.release_schedule.items()
            if _release_at(definition, release_date) <= api_receipt.retrieved_at.astimezone(UTC)
        ]
        if not released_periods:
            raise TreasuryMtsError("No scheduled MTS release has occurred by retrieval time")
        end = max(released_periods)
    selected_periods = tuple(
        period for period in sorted(definition.release_schedule) if start <= period <= end
    )
    if not selected_periods:
        raise TreasuryMtsError("No verified MTS releases fall inside the requested window")
    by_record_date: dict[date, list[dict[str, object]]] = {}
    for row in rows:
        raw_date = row.get("record_date")
        if not isinstance(raw_date, str):
            raise TreasuryMtsError("Treasury MTS row has no record_date")
        try:
            record_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise TreasuryMtsError("Treasury MTS row has an invalid record_date") from exc
        by_record_date.setdefault(record_date, []).append(row)

    existing = store.read_table("raw_observations").filter(pl.col("source") == definition.source)
    existing_periods = {
        period
        for period in existing["obs_period"].unique().to_list()
        if isinstance(period, date)
        and existing.filter(pl.col("obs_period") == period)["series_id"].n_unique()
        == len(definition.targets)
    }
    writes: list[WriteReceipt] = []
    for period in selected_periods:
        issue_rows = by_record_date.get(period)
        if issue_rows is None:
            raise TreasuryMtsError(f"Treasury MTS API has no dated issue for {period}")
        if period in existing_periods:
            continue
        issue = client.fetch_issue(period)
        value_row = _select_current_month_row(issue_rows, period)
        release_date = definition.release_schedule[period]
        release_at = _release_at(definition, release_date)
        lake_rows: list[dict[str, object]] = []
        for target in definition.targets:
            lake_rows.append(
                {
                    "source": definition.source,
                    "series_id": target.target_series_id,
                    "obs_period": period,
                    "value": _millions(value_row.get(target.api_field), target.api_field),
                    "unit": target.unit,
                    "release_date": release_at,
                    "vintage_date": release_date,
                    "ingested_at": api_receipt.retrieved_at.astimezone(UTC),
                    "source_url": issue.source_url,
                    "checksum": issue.checksum,
                }
            )
        writes.append(store.append("raw_observations", lake_rows))
    return TreasuryMtsReceipt(
        rows=len(selected_periods) * len(definition.targets),
        periods=len(selected_periods),
        first_period=selected_periods[0],
        last_period=selected_periods[-1],
        skipped_unverified_periods=tuple(
            item
            for item in definition.unverified_historical_periods
            if start.strftime("%Y-%m") <= item <= end.strftime("%Y-%m")
        ),
        appended_periods=len(writes),
        already_present_periods=len(selected_periods) - len(writes),
        writes=tuple(writes),
    )


def _parse_api(receipt: HttpReceipt) -> tuple[list[dict[str, object]], dict[str, str]]:
    try:
        payload = json.loads(receipt.content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TreasuryMtsError("Treasury MTS API did not return JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise TreasuryMtsError("Treasury MTS API payload has no data list")
    meta = payload.get("meta")
    if not isinstance(meta, dict) or not isinstance(meta.get("dataTypes"), dict):
        raise TreasuryMtsError("Treasury MTS API payload has no data type metadata")
    rows: list[dict[str, object]] = []
    for item in cast(list[object], payload["data"]):
        if not isinstance(item, dict):
            raise TreasuryMtsError("Treasury MTS API row is not an object")
        rows.append(cast(dict[str, object], item))
    data_types = {
        str(key): str(value) for key, value in cast(dict[object, object], meta["dataTypes"]).items()
    }
    return rows, data_types


def _validate_dataset_metadata(receipt: HttpReceipt, definition: TreasuryMtsDefinition) -> None:
    try:
        payload = json.loads(receipt.content)
        config = payload["result"]["pageContext"]["config"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TreasuryMtsError("Treasury MTS dataset metadata is malformed") from exc
    if not isinstance(config, dict):
        raise TreasuryMtsError("Treasury MTS dataset metadata config is not an object")
    if config.get("datasetId") != definition.dataset_id:
        raise TreasuryMtsError("Treasury MTS dataset ID changed")
    if config.get("name") != definition.expected_dataset_name:
        raise TreasuryMtsError("Treasury MTS dataset name changed")
    specs = config.get("apis")
    if not isinstance(specs, list):
        raise TreasuryMtsError("Treasury MTS dataset has no technical specifications")
    endpoint = definition.api_url.split("/services/api/fiscal_service/", 1)[-1]
    matching = [
        item for item in specs if isinstance(item, dict) and item.get("endpoint") == endpoint
    ]
    if len(matching) != 1 or matching[0].get("tableName") != definition.expected_table_name:
        raise TreasuryMtsError("Treasury MTS Table 1 endpoint changed")


def _validate_api_contract(
    rows: list[dict[str, object]],
    data_types: dict[str, str],
    definition: TreasuryMtsDefinition,
) -> None:
    if not rows:
        raise TreasuryMtsError("Treasury MTS API returned no rows")
    if not REQUIRED_API_FIELDS.issubset(rows[0]):
        raise TreasuryMtsError("Treasury MTS Table 1 fields changed")
    for target in definition.targets:
        if data_types.get(target.api_field) != "CURRENCY":
            raise TreasuryMtsError(f"Treasury MTS unit changed for {target.api_field}")
    if any(row.get("table_nbr") != "1" for row in rows):
        raise TreasuryMtsError("Treasury MTS endpoint returned a non-Table-1 row")


def _select_current_month_row(rows: list[dict[str, object]], period: date) -> dict[str, object]:
    parent_descriptions = {
        str(row.get("classification_id")): str(row.get("classification_desc")) for row in rows
    }
    fiscal_year = period.year + int(period.month >= 10)
    expected_parent = f"FY {fiscal_year}"
    matches = [
        row
        for row in rows
        if row.get("classification_desc") == period.strftime("%B")
        and row.get("data_type_cd") == "D"
        and row.get("record_type_cd") == "MTH"
        and parent_descriptions.get(str(row.get("parent_id"))) == expected_parent
    ]
    if len(matches) != 1:
        raise TreasuryMtsError(
            f"Treasury MTS {period} has {len(matches)} current-month rows; expected one"
        )
    return matches[0]


def _millions(raw: object, field: str) -> float:
    if not isinstance(raw, str) or re.fullmatch(r"-?\d+(?:\.\d+)?", raw) is None:
        raise TreasuryMtsError(f"Treasury MTS {field} is not a decimal amount")
    try:
        return float(Decimal(raw) / DOLLARS_PER_MILLION)
    except InvalidOperation as exc:
        raise TreasuryMtsError(f"Treasury MTS {field} is invalid") from exc


def _release_at(definition: TreasuryMtsDefinition, release_date: date) -> datetime:
    return datetime.combine(
        release_date,
        time.fromisoformat(definition.release_time),
        tzinfo=ZoneInfo(definition.time_zone),
    ).astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2017, 12, 31))
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    args = parser.parse_args()
    with HttpTransport() as transport:
        result = ingest_mts_history(
            AppendOnlyParquetStore(args.lake_root),
            TreasuryMtsClient(transport),
            start=args.start,
            end=args.end,
        )
    print(
        json.dumps(
            {
                "rows": result.rows,
                "periods": result.periods,
                "first_period": result.first_period.isoformat(),
                "last_period": result.last_period.isoformat(),
                "skipped_unverified_periods": list(result.skipped_unverified_periods),
                "appended_periods": result.appended_periods,
                "already_present_periods": result.already_present_periods,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
