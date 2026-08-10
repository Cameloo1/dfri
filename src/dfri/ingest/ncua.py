"""Verified NCUA 5300 Call Report auto and residual consumer-loan aggregates."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO, TextIOWrapper
from typing import Final
from zipfile import BadZipFile, ZipFile

from dfri.ingest.http import HttpTransport

NCUA_QUARTERLY_PAGE: Final = (
    "https://ncua.gov/analysis/credit-union-corporate-call-report-data/quarterly-data"
)
NCUA_TERMS_URL: Final = "https://ncua.gov/support-services/guaranteed-notes-program"
NCUA_DATA_URL_TEMPLATE: Final = (
    "https://ncua.gov/files/publications/analysis/call-report-data-{year}-{month:02d}.zip"
)
NCUA_FIELDS: Final = {
    "FS220A.txt": ("ACCT_370", "ACCT_385", "ACCT_396", "ACCT_002"),
    "FS220L.txt": ("ACCT_025B1", "ACCT_400A1", "ACCT_400B1"),
    "FS220R.txt": ("ACCT_RL0047",),
}


class NcuaContractError(RuntimeError):
    """The NCUA bulk file no longer satisfies the pinned 5300 contract."""


@dataclass(frozen=True)
class NcuaAutoObservation:
    report_date: date
    record_count: int
    used_vehicle: int
    new_vehicle: int
    credit_cards: int
    leases: int
    total_loans_and_leases: int
    consumer_real_estate: int
    commercial_member: int
    commercial_nonmember: int
    source_url: str
    checksum: str

    @property
    def automobile(self) -> int:
        return self.used_vehicle + self.new_vehicle

    @property
    def nonrevolving_consumer_residual(self) -> int:
        return (
            self.total_loans_and_leases
            - self.consumer_real_estate
            - self.commercial_member
            - self.commercial_nonmember
            - self.credit_cards
            - self.leases
        )

    @property
    def automobile_share_of_nonrevolving_consumer(self) -> float:
        return self.automobile / self.nonrevolving_consumer_residual


class NcuaClient:
    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def fetch(self, report_date: date) -> NcuaAutoObservation:
        if report_date.month not in {3, 6, 9, 12}:
            raise NcuaContractError("NCUA report date must be a calendar-quarter end")
        url = NCUA_DATA_URL_TEMPLATE.format(year=report_date.year, month=report_date.month)
        receipt = self._transport.get(url, headers={"Accept": "application/zip"})
        return parse_ncua_call_report(
            receipt.content,
            report_date=report_date,
            source_url=receipt.source_url,
            checksum=receipt.checksum,
        )


def parse_ncua_call_report(
    content: bytes,
    *,
    report_date: date,
    source_url: str,
    checksum: str,
) -> NcuaAutoObservation:
    if hashlib.sha256(content).hexdigest() != checksum:
        raise NcuaContractError("NCUA bulk checksum does not match the response")
    try:
        archive = ZipFile(BytesIO(content))
    except BadZipFile as exc:
        raise NcuaContractError("NCUA bulk response is not a valid ZIP file") from exc
    tables: dict[str, dict[str, dict[str, int]]] = {}
    with archive:
        names = {name.rsplit("/", 1)[-1].casefold(): name for name in archive.namelist()}
        for filename, fields in NCUA_FIELDS.items():
            actual = names.get(filename.casefold())
            if actual is None:
                raise NcuaContractError(f"NCUA bulk file is missing {filename}")
            with archive.open(actual) as raw:
                rows = csv.DictReader(TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                if rows.fieldnames is None or not {"CU_NUMBER", "CYCLE_DATE", *fields}.issubset(
                    rows.fieldnames
                ):
                    raise NcuaContractError(f"NCUA fields changed in {filename}")
                by_cu: dict[str, dict[str, int]] = {}
                for row in rows:
                    cu_number = row.get("CU_NUMBER", "")
                    if not cu_number or cu_number in by_cu:
                        raise NcuaContractError(
                            f"NCUA CU identity is missing or duplicated in {filename}"
                        )
                    if _cycle_date(row.get("CYCLE_DATE", "")) != report_date:
                        raise NcuaContractError(f"NCUA cycle date changed in {filename}")
                    by_cu[cu_number] = {
                        field: _amount(row.get(field, ""), field) for field in fields
                    }
                if not by_cu:
                    raise NcuaContractError(f"NCUA file has no rows: {filename}")
                tables[filename] = by_cu
    key_sets = {frozenset(table) for table in tables.values()}
    if len(key_sets) != 1:
        raise NcuaContractError("NCUA schedule reporter panels differ")
    cu_numbers = next(iter(key_sets))
    totals: dict[str, int] = {}
    for filename, fields in NCUA_FIELDS.items():
        for field in fields:
            totals[field] = sum(tables[filename][cu][field] for cu in cu_numbers)

    observation = NcuaAutoObservation(
        report_date=report_date,
        record_count=len(cu_numbers),
        used_vehicle=totals["ACCT_370"],
        new_vehicle=totals["ACCT_385"],
        credit_cards=totals["ACCT_396"],
        leases=totals["ACCT_002"],
        total_loans_and_leases=totals["ACCT_025B1"],
        consumer_real_estate=totals["ACCT_RL0047"],
        commercial_member=totals["ACCT_400A1"],
        commercial_nonmember=totals["ACCT_400B1"],
        source_url=source_url,
        checksum=checksum,
    )
    residual = observation.nonrevolving_consumer_residual
    if residual <= 0 or observation.automobile > residual:
        raise NcuaContractError("NCUA residual consumer denominator is invalid")
    return observation


def _cycle_date(value: str) -> date:
    try:
        month, day, year = (int(part) for part in value.strip().split(" ", 1)[0].split("/"))
        return date(year, month, day)
    except (TypeError, ValueError) as exc:
        raise NcuaContractError("NCUA cycle date format changed") from exc


def _amount(value: str, field: str) -> int:
    try:
        parsed = Decimal(value or "0")
    except InvalidOperation as exc:
        raise NcuaContractError(f"NCUA amount is nonnumeric: {field}") from exc
    if parsed < 0 or parsed != parsed.to_integral_value():
        raise NcuaContractError(f"NCUA amount must be a nonnegative integer: {field}")
    return int(parsed)
