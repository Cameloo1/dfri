"""Census Economic Indicators API client and pinned MARTS contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from dfri.ingest.http import HttpTransport
from dfri.ingest.registry import ContextSeriesDefinition

CENSUS_BASE: Final = "https://api.census.gov/data/timeseries/eits"
REQUIRED_VARIABLES: Final = frozenset(
    {"cell_value", "data_type_code", "category_code", "seasonally_adj", "time"}
)


class CensusContractError(ValueError):
    """A Census response failed its dataset, metadata, or value contract."""


@dataclass(frozen=True)
class CensusDatasetData:
    dataset: str
    source_url: str
    checksum: str
    retrieved_at: str
    rows: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class CensusVerification:
    series_id: str
    title: str
    units: str
    frequency: str
    observations: int
    source_url: str
    checksum: str


class CensusClient:
    def __init__(self, transport: HttpTransport, api_key: str) -> None:
        if not api_key:
            raise ValueError("CENSUS_API_KEY is required")
        self._transport = transport
        self._api_key = api_key

    def fetch_variables(self, dataset: str) -> frozenset[str]:
        receipt = self._transport.get(
            f"{CENSUS_BASE}/{dataset}/variables.json",
            headers={"Accept": "application/json"},
        )
        try:
            payload = json.loads(receipt.content)
            variables = payload["variables"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise CensusContractError("Census variables response is malformed") from exc
        if not isinstance(variables, dict):
            raise CensusContractError("Census variables must be an object")
        names = frozenset(str(name) for name in variables)
        if not REQUIRED_VARIABLES.issubset(names):
            raise CensusContractError("Census dataset is missing required variables")
        return names

    def fetch_month(self, dataset: str, month: str) -> CensusDatasetData:
        return self.fetch_periods(dataset, month)

    def fetch_periods(self, dataset: str, time_range: str) -> CensusDatasetData:
        receipt = self._transport.get(
            f"{CENSUS_BASE}/{dataset}",
            params={
                "get": (
                    "cell_value,data_type_code,time_slot_id,time_slot_name,time_slot_date,"
                    "category_code,seasonally_adj,program_code"
                ),
                "time": time_range,
                "key": self._api_key,
            },
            headers={"Accept": "application/json"},
        )
        return CensusDatasetData(
            dataset=dataset,
            source_url=receipt.source_url,
            checksum=receipt.checksum,
            retrieved_at=receipt.retrieved_at.isoformat(),
            rows=parse_census_rows(receipt.content),
        )

    def verify_series(
        self, definitions: tuple[ContextSeriesDefinition, ...], *, month: str
    ) -> tuple[CensusVerification, ...]:
        applicable = tuple(item for item in definitions if item.source == "census")
        if not applicable:
            raise CensusContractError("No registered Census definitions")
        datasets: dict[str, CensusDatasetData] = {}
        verified_variables: set[str] = set()
        verified: list[CensusVerification] = []
        for definition in applicable:
            attrs = definition.expected_source_attributes
            dataset = attrs["dataset"]
            if dataset not in verified_variables:
                self.fetch_variables(dataset)
                verified_variables.add(dataset)
            if dataset not in datasets:
                datasets[dataset] = self.fetch_month(dataset, month)
            data = datasets[dataset]
            matches = [
                row
                for row in data.rows
                if row.get("category_code") == attrs["category_code"]
                and row.get("data_type_code") == attrs["data_type_code"]
                and row.get("seasonally_adj") == attrs["seasonally_adj"]
            ]
            if not matches:
                raise CensusContractError(
                    f"Registered Census series missing: {definition.series_id}"
                )
            for row in matches:
                if row.get("program_code") != attrs["program_code"]:
                    raise CensusContractError(f"{definition.series_id} program_code mismatch")
                parse_census_value(row.get("cell_value", ""))
            verified.append(
                CensusVerification(
                    series_id=definition.series_id,
                    title=definition.expected_title,
                    units=definition.units,
                    frequency=definition.frequency,
                    observations=len(matches),
                    source_url=data.source_url,
                    checksum=data.checksum,
                )
            )
        return tuple(verified)


def parse_census_rows(content: bytes) -> tuple[dict[str, str], ...]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CensusContractError("Census response is not JSON") from exc
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[0], list):
        raise CensusContractError("Census response has no tabular rows")
    headers = payload[0]
    if not all(isinstance(header, str) for header in headers):
        raise CensusContractError("Census headers must be strings")
    rows: list[dict[str, str]] = []
    for raw in payload[1:]:
        if (
            not isinstance(raw, list)
            or len(raw) != len(headers)
            or not all(isinstance(value, str) for value in raw)
        ):
            raise CensusContractError("Census row shape does not match its header")
        rows.append(dict(zip(headers, raw, strict=True)))
    return tuple(rows)


def parse_census_value(raw: str) -> Decimal | None:
    if raw in {"", "NA", "N", "S", "Z"}:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise CensusContractError(f"Census value is invalid: {raw!r}") from exc
