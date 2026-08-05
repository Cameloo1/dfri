"""SEC EDGAR read-only client with fair-access URL and shape contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from dfri.ingest.http import HttpFileReceipt, HttpReceipt, HttpTransport

DATA_BASE: Final = "https://data.sec.gov"
ARCHIVES_BASE: Final = "https://www.sec.gov/Archives/edgar/data"
EFTS_ENDPOINT: Final = "https://efts.sec.gov/LATEST/search-index"
CIK_PATTERN: Final = re.compile(r"^\d{1,10}$")
ACCESSION_PATTERN: Final = re.compile(r"^\d{10}-\d{2}-\d{6}$")
DOCUMENT_PATTERN: Final = re.compile(r"^[A-Za-z0-9._-]+$")


class EdgarContractError(ValueError):
    """An EDGAR identifier or response failed a pinned contract."""


@dataclass(frozen=True)
class EdgarJsonReceipt:
    kind: str
    source_url: str
    checksum: str
    retrieved_at: str
    payload: dict[str, object]


class EdgarClient:
    """Read-only EDGAR endpoints; caller must supply a transport paced at >=0.1s."""

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def submissions(self, cik: str) -> EdgarJsonReceipt:
        normalized = normalize_cik(cik)
        return self._get_json(
            "submissions",
            f"{DATA_BASE}/submissions/CIK{normalized}.json",
            {"cik", "name", "filings"},
        )

    def companyfacts(self, cik: str) -> EdgarJsonReceipt:
        normalized = normalize_cik(cik)
        return self._get_json(
            "companyfacts",
            f"{DATA_BASE}/api/xbrl/companyfacts/CIK{normalized}.json",
            {"cik", "entityName", "facts"},
        )

    def archive_index(self, cik: str, accession: str) -> EdgarJsonReceipt:
        normalized = normalize_cik(cik)
        if not ACCESSION_PATTERN.fullmatch(accession):
            raise EdgarContractError("Invalid SEC accession number")
        accession_compact = accession.replace("-", "")
        cik_number = str(int(normalized))
        return self._get_json(
            "archive_index",
            f"{ARCHIVES_BASE}/{cik_number}/{accession_compact}/index.json",
            {"directory"},
        )

    def archive_document(self, cik: str, accession: str, primary_document: str) -> bytes:
        return self.archive_document_receipt(cik, accession, primary_document).content

    def archive_document_receipt(
        self, cik: str, accession: str, primary_document: str
    ) -> HttpReceipt:
        normalized = normalize_cik(cik)
        if not ACCESSION_PATTERN.fullmatch(accession):
            raise EdgarContractError("Invalid SEC accession number")
        if not DOCUMENT_PATTERN.fullmatch(primary_document):
            raise EdgarContractError("Invalid SEC primary-document name")
        accession_compact = accession.replace("-", "")
        cik_number = str(int(normalized))
        return self._transport.get(
            f"{ARCHIVES_BASE}/{cik_number}/{accession_compact}/{primary_document}"
        )

    def archive_document_to_gzip(
        self, cik: str, accession: str, document: str, destination: Path
    ) -> HttpFileReceipt:
        """Stream a large immutable archive exhibit into a private gzip file."""

        normalized = normalize_cik(cik)
        if not ACCESSION_PATTERN.fullmatch(accession):
            raise EdgarContractError("Invalid SEC accession number")
        if not DOCUMENT_PATTERN.fullmatch(document):
            raise EdgarContractError("Invalid SEC document name")
        accession_compact = accession.replace("-", "")
        cik_number = str(int(normalized))
        return self._transport.get_to_gzip(
            f"{ARCHIVES_BASE}/{cik_number}/{accession_compact}/{document}", destination
        )

    def efts_search(
        self, *, query: str, forms: str, start_date: str, end_date: str, size: int = 10
    ) -> EdgarJsonReceipt:
        if not query or size < 1 or size > 100:
            raise EdgarContractError("Invalid EFTS query or result size")
        return self._get_json(
            "efts",
            EFTS_ENDPOINT,
            {"hits"},
            params={
                "q": query,
                "forms": forms,
                "startdt": start_date,
                "enddt": end_date,
                "from": 0,
                "size": size,
            },
        )

    def _get_json(
        self,
        kind: str,
        url: str,
        required: set[str],
        *,
        params: dict[str, str | int] | None = None,
    ) -> EdgarJsonReceipt:
        receipt = self._transport.get(url, params=params, headers={"Accept": "application/json"})
        try:
            raw = json.loads(receipt.content)
        except json.JSONDecodeError as exc:
            raise EdgarContractError(f"SEC {kind} response is not JSON") from exc
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise EdgarContractError(f"SEC {kind} response is missing required fields")
        return EdgarJsonReceipt(
            kind=kind,
            source_url=receipt.source_url,
            checksum=receipt.checksum,
            retrieved_at=receipt.retrieved_at.isoformat(),
            payload=cast(dict[str, object], raw),
        )


def normalize_cik(cik: str) -> str:
    if not CIK_PATTERN.fullmatch(cik):
        raise EdgarContractError("Invalid SEC CIK")
    return cik.zfill(10)
