"""Bureau of Economic Analysis API client and pinned metadata checks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, cast

from dfri.ingest.http import HttpTransport
from dfri.ingest.registry import ContextSeriesDefinition

BEA_ENDPOINT: Final = "https://apps.bea.gov/api/data/"


class BeaContractError(ValueError):
    """A BEA response failed its endpoint, metadata, or value contract."""


@dataclass(frozen=True)
class BeaObservation:
    time_period: str
    value: Decimal | None


@dataclass(frozen=True)
class BeaVerification:
    series_id: str
    title: str
    units: str
    frequency: str
    observations: int
    source_url: str
    checksum: str


@dataclass(frozen=True)
class BeaTableData:
    dataset: str
    table_name: str
    source_url: str
    checksum: str
    retrieved_at: str
    rows: tuple[dict[str, str], ...]


class BeaClient:
    def __init__(self, transport: HttpTransport, api_key: str) -> None:
        if not api_key:
            raise ValueError("BEA_API_KEY is required")
        self._transport = transport
        self._api_key = api_key

    def fetch_table(
        self, *, dataset: str, table_name: str, year: str, frequency: str = "M"
    ) -> BeaTableData:
        receipt = self._transport.get(
            BEA_ENDPOINT,
            params={
                "UserID": self._api_key,
                "method": "GetData",
                "DataSetName": dataset,
                "TableName": table_name,
                "Frequency": frequency,
                "Year": year,
                "ResultFormat": "JSON",
            },
            headers={"Accept": "application/json"},
        )
        rows = parse_bea_rows(receipt.content)
        return BeaTableData(
            dataset=dataset,
            table_name=table_name,
            source_url=receipt.source_url,
            checksum=bea_rows_checksum(rows),
            retrieved_at=receipt.retrieved_at.isoformat(),
            rows=rows,
        )

    def verify_series(
        self, definitions: tuple[ContextSeriesDefinition, ...], *, year: str
    ) -> tuple[BeaVerification, ...]:
        applicable = tuple(item for item in definitions if item.source == "bea")
        if not applicable:
            raise BeaContractError("No registered BEA definitions")
        tables: dict[tuple[str, str], BeaTableData] = {}
        verified: list[BeaVerification] = []
        for definition in applicable:
            attrs = definition.expected_source_attributes
            key = (attrs["dataset"], attrs["table_name"])
            if key not in tables:
                tables[key] = self.fetch_table(
                    dataset=key[0], table_name=key[1], year=year, frequency="M"
                )
            table = tables[key]
            matches = [
                row
                for row in table.rows
                if row.get("SeriesCode") == attrs["series_code"]
                and row.get("LineNumber") == attrs["line_number"]
            ]
            if not matches:
                raise BeaContractError(f"Registered BEA series missing: {definition.series_id}")
            for row in matches:
                verify_bea_row(definition, row)
                parse_bea_value(row.get("DataValue", ""))
            verified.append(
                BeaVerification(
                    series_id=definition.series_id,
                    title=definition.expected_title,
                    units=definition.units,
                    frequency=definition.frequency,
                    observations=len(matches),
                    source_url=table.source_url,
                    checksum=table.checksum,
                )
            )
        return tuple(verified)


def parse_bea_rows(content: bytes) -> tuple[dict[str, str], ...]:
    try:
        payload = json.loads(content)
        api = payload["BEAAPI"]
        results = api["Results"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BeaContractError("BEA response is not a valid API result") from exc
    if isinstance(results, dict) and "Error" in results:
        raise BeaContractError("BEA API returned a structured error")
    raw_rows = results.get("Data") if isinstance(results, dict) else None
    if not isinstance(raw_rows, list) or not raw_rows:
        raise BeaContractError("BEA response contains no data rows")
    rows: list[dict[str, str]] = []
    for raw in raw_rows:
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise BeaContractError("BEA data rows must contain string fields")
        rows.append(cast(dict[str, str], raw))
    return tuple(rows)


def bea_rows_checksum(rows: tuple[dict[str, str], ...]) -> str:
    """Hash authoritative BEA data rows while excluding volatile API-envelope metadata."""

    canonical_rows = sorted(
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True) for row in rows
    )
    canonical = ("[" + ",".join(canonical_rows) + "]").encode()
    return hashlib.sha256(canonical).hexdigest()


def parse_bea_value(raw: str) -> Decimal | None:
    if raw in {"", "(NA)", "NA", "--"}:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise BeaContractError(f"BEA value is invalid: {raw!r}") from exc


def verify_bea_row(definition: ContextSeriesDefinition, row: dict[str, str]) -> None:
    attrs = definition.expected_source_attributes
    checks = {
        "TableName": attrs["table_name"],
        "SeriesCode": attrs["series_code"],
        "LineNumber": attrs["line_number"],
        "LineDescription": definition.expected_title,
        "CL_UNIT": attrs["cl_unit"],
        "UNIT_MULT": attrs["unit_mult"],
    }
    for key, expected in checks.items():
        if row.get(key) != expected:
            raise BeaContractError(
                f"{definition.series_id} field {key} mismatch: {row.get(key)!r} != {expected!r}"
            )
    if not row.get("TimePeriod", "").startswith(tuple(str(year) for year in range(1900, 2201))):
        raise BeaContractError(f"{definition.series_id} has invalid monthly TimePeriod")
