"""Live authoritative-source contract verification with redacted receipts."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from dfri.ingest.bea import BeaClient
from dfri.ingest.board import FederalReserveBoardClient
from dfri.ingest.census import CensusClient
from dfri.ingest.edgar import EdgarClient, EdgarJsonReceipt
from dfri.ingest.http import DEFAULT_USER_AGENT, HttpTransport
from dfri.ingest.registry import (
    SourceContract,
    load_board_series,
    load_context_series,
    load_sec_contracts,
    load_source_contracts,
)


class VerificationError(RuntimeError):
    """Live verification could not establish every required source contract."""


def validate_source_contracts(contracts: dict[str, SourceContract]) -> None:
    required = {
        "federal_reserve_board",
        "bea",
        "census",
        "new_york_fed",
        "sec_edgar",
        "wikimedia",
    }
    if set(contracts) != required:
        raise VerificationError("Source-contract registry is incomplete")
    for contract in contracts.values():
        if not (
            contract.automated_access and contract.storage and contract.derivative_redistribution
        ):
            raise VerificationError(
                f"Source contract is not publication-safe: {contract.source_id}"
            )


def write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode(
        "utf-8"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _verify_live(  # pragma: no cover - exercised only by the opt-in live smoke
    *, bea_api_key: str, census_api_key: str, year: int, census_month: str
) -> dict[str, object]:
    contracts = load_source_contracts()
    sec_contracts = load_sec_contracts()
    validate_source_contracts(contracts)
    board_definitions = load_board_series()
    context_definitions = load_context_series()

    with HttpTransport(user_agent=DEFAULT_USER_AGENT) as transport:
        board = FederalReserveBoardClient(transport)
        board_receipts = [
            *board.verify_release("g19", board_definitions),
            *board.verify_release("h8", board_definitions),
        ]
        archive_depth = {
            "g19": [item.isoformat() for item in board.discover_archive_dates("g19", 2015)],
            "h8": [item.isoformat() for item in board.discover_archive_dates("h8", 2015)],
        }
        bea_receipts = BeaClient(transport, bea_api_key).verify_series(
            context_definitions, year=str(year)
        )
        census_receipts = CensusClient(transport, census_api_key).verify_series(
            context_definitions, month=census_month
        )

    with HttpTransport(user_agent=DEFAULT_USER_AGENT, min_interval_seconds=0.11) as sec_transport:
        edgar = EdgarClient(sec_transport)
        submissions = edgar.submissions("0000320193")
        companyfacts = edgar.companyfacts("0000320193")
        archive_index = edgar.archive_index("0000320193", "0000320193-25-000079")
        efts = edgar.efts_search(
            query="auto loan",
            forms="ABS-EE",
            start_date=f"{year}-01-01",
            end_date=datetime.now(UTC).date().isoformat(),
            size=1,
        )

    return {
        "schema_version": 1,
        "verified_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "contracts": {
            source_id: {
                "status": contract.status,
                "terms_url": contract.terms_url,
                "conditions": list(contract.conditions),
            }
            for source_id, contract in sorted(contracts.items())
        },
        "sources": {
            "federal_reserve_board": {
                "series": [asdict(receipt) for receipt in board_receipts],
                "archive_2015": archive_depth,
            },
            "bea": {"series": [asdict(receipt) for receipt in bea_receipts]},
            "census": {"series": [asdict(receipt) for receipt in census_receipts]},
            "sec_edgar": {
                "submissions": _edgar_receipt(submissions),
                "companyfacts": _edgar_receipt(companyfacts),
                "archive_index": _edgar_receipt(archive_index),
                "efts": _edgar_receipt(efts),
                "filing_contracts": sec_contracts,
                "max_requests_per_second": 10,
                "configured_interval_seconds": 0.11,
            },
        },
    }


def _edgar_receipt(receipt: EdgarJsonReceipt) -> dict[str, object]:
    values = cast(dict[str, object], asdict(receipt))
    values.pop("payload", None)
    return values


def main() -> None:  # pragma: no cover - CLI boundary
    current_year = datetime.now(UTC).year
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--year", type=int, default=current_year)
    parser.add_argument("--census-month", default=f"{current_year}-01")
    args = parser.parse_args()
    bea_key = os.environ.get("BEA_API_KEY", "")
    census_key = os.environ.get("CENSUS_API_KEY", "")
    if not bea_key or not census_key:
        blocked = {
            "schema_version": 1,
            "verified_at": datetime.now(UTC).isoformat(),
            "status": "BLOCKED",
            "error": "BEA_API_KEY and CENSUS_API_KEY are required in the process environment",
        }
        write_receipt(args.output, blocked)
        raise SystemExit(2)
    try:
        payload = _verify_live(
            bea_api_key=bea_key,
            census_api_key=census_key,
            year=args.year,
            census_month=args.census_month,
        )
    except Exception as exc:
        blocked = {
            "schema_version": 1,
            "verified_at": datetime.now(UTC).isoformat(),
            "status": "BLOCKED",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        write_receipt(args.output, blocked)
        raise SystemExit(1) from exc
    write_receipt(args.output, payload)
    print(json.dumps({"output": str(args.output), "status": "PASS"}, sort_keys=True))


if __name__ == "__main__":
    main()
