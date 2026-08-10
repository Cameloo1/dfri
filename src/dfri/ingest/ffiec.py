"""Verified FFIEC Call Report consumer-loan aggregates from the FDIC public API."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from dfri.ingest.http import HttpTransport

FDIC_FINANCIALS_URL: Final = "https://api.fdic.gov/banks/financials"
FFIEC_BULK_URL: Final = "https://cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx"
FFIEC_TERMS_URL: Final = "https://catalog.data.gov/dataset/ffiec-call-reports"
FFIEC_INSTRUCTIONS_URL: Final = "https://www.ffiec.gov/resources/reporting-forms/ffiec051"
FFIEC_FIELDS: Final = (
    "REPDTE",
    "CERT",
    "LNCON",
    "LNCRCD",
    "LNAUTO",
    "LNCONOTH",
    "LNCONORP",
)
FFIEC_ITEM_IDENTIFIERS: Final = {
    "credit_cards": "RCONB538",
    "other_revolving": "RCONB539",
    "automobile": "RCONK137",
    "other_consumer": "RCONK207",
}


class FfiecContractError(RuntimeError):
    """The FFIEC/FDIC response no longer satisfies the pinned contract."""


@dataclass(frozen=True)
class FfiecAutoObservation:
    report_date: date
    record_count: int
    consumer_total_thousands: int
    credit_cards_thousands: int
    other_revolving_thousands: int
    automobile_thousands: int
    other_consumer_thousands: int
    source_url: str
    checksum: str

    @property
    def direct_nonrevolving_thousands(self) -> int:
        return self.automobile_thousands + self.other_consumer_thousands

    @property
    def automobile_share_of_direct_nonrevolving(self) -> float:
        return self.automobile_thousands / self.direct_nonrevolving_thousands


class FfiecClient:
    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def fetch(self, report_date: date) -> FfiecAutoObservation:
        report_key = report_date.strftime("%Y%m%d")
        receipt = self._transport.get(
            FDIC_FINANCIALS_URL,
            params={
                "filters": f"REPDTE:{report_key}",
                "fields": ",".join(FFIEC_FIELDS),
                "limit": 10000,
                "format": "json",
            },
            headers={"Accept": "application/json"},
        )
        return parse_ffiec_financials(
            receipt.content,
            report_date=report_date,
            source_url=receipt.source_url,
            checksum=receipt.checksum,
        )


def parse_ffiec_financials(
    content: bytes,
    *,
    report_date: date,
    source_url: str,
    checksum: str,
) -> FfiecAutoObservation:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FfiecContractError("FFIEC financials response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise FfiecContractError("FFIEC financials response must be an object")
    meta = payload.get("meta")
    rows = payload.get("data")
    if not isinstance(meta, dict) or not isinstance(rows, list) or not rows:
        raise FfiecContractError("FFIEC financials response is missing metadata or rows")
    total = _required_nonnegative_int(meta, "total")
    if total != len(rows) or total > 10000:
        raise FfiecContractError(
            f"FFIEC response is incomplete: metadata={total}, rows={len(rows)}"
        )

    expected_date = report_date.strftime("%Y%m%d")
    sums: dict[str, int] = dict.fromkeys(FFIEC_FIELDS[2:], 0)
    certs: set[int] = set()
    for wrapper in rows:
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("data"), dict):
            raise FfiecContractError("FFIEC financials row wrapper changed")
        row = wrapper["data"]
        if row.get("REPDTE") != expected_date:
            raise FfiecContractError("FFIEC response contains a different report date")
        cert = _required_nonnegative_int(row, "CERT")
        if cert in certs:
            raise FfiecContractError(f"FFIEC response duplicates certificate {cert}")
        certs.add(cert)
        for field in sums:
            sums[field] += _required_nonnegative_int(row, field)

    other_revolving = sums["LNCON"] - (sums["LNCRCD"] + sums["LNAUTO"] + sums["LNCONOTH"])
    if other_revolving < 0:
        raise FfiecContractError("FFIEC Schedule RC-C item 6 components exceed the total")
    if sums["LNCONORP"] != sums["LNCON"] - sums["LNAUTO"]:
        raise FfiecContractError("FDIC normalized consumer-loan fields no longer reconcile")
    direct_nonrevolving = sums["LNAUTO"] + sums["LNCONOTH"]
    if direct_nonrevolving <= 0:
        raise FfiecContractError("FFIEC direct nonrevolving denominator is not positive")

    return FfiecAutoObservation(
        report_date=report_date,
        record_count=len(rows),
        consumer_total_thousands=sums["LNCON"],
        credit_cards_thousands=sums["LNCRCD"],
        other_revolving_thousands=other_revolving,
        automobile_thousands=sums["LNAUTO"],
        other_consumer_thousands=sums["LNCONOTH"],
        source_url=source_url,
        checksum=checksum,
    )


def _required_nonnegative_int(item: dict[str, Any], key: str) -> int:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FfiecContractError(f"FFIEC field is missing or nonnumeric: {key}")
    if not math.isfinite(float(value)) or float(value) < 0 or not float(value).is_integer():
        raise FfiecContractError(f"FFIEC field must be a nonnegative integer: {key}")
    return int(value)
