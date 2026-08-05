"""Point-in-time Census MARTS release discovery, parsing, and persistence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from io import BytesIO
from itertools import pairwise
from math import isfinite
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import polars as pl
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from dfri.ingest.http import HttpReceipt, HttpTransport, SourceRequestError
from dfri.ingest.registry import CensusArchiveDefinition, load_census_archive
from dfri.lake.store import AppendOnlyParquetStore

DEFAULT_START: Final = date(2015, 1, 1)
PDF_MAGIC: Final = b"%PDF-"
NUMBER_PATTERN: Final = re.compile(
    r"(?<!\w)[-+]?\d{1,3}(?:,\s*\d{3})+(?:\.\d+)?"
    r"|(?<!\w)[-+]?\d+\.\d+"
    r"|(?<!\w)[-+]?\d+(?!\w)"
)
MODERN_RELEASE_PATTERN: Final = re.compile(
    r"FOR RELEASE AT\s+8:30\s+(?:A\.M\.|AM)\s+(E[DS]\s*T),\s+"
    r"[A-Z]+,\s+([A-Z]+\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
LEGACY_RELEASE_PATTERN: Final = re.compile(
    r"(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY),?\s+"
    r"([A-Z]+\s+\d{1,2},\s+\d{4}),\s+AT\s+8:30\s+"
    r"(?:A\.M\.|AM)\s+(E[DS]\s*T)",
    re.IGNORECASE,
)


class CensusArchiveError(RuntimeError):
    """A dated MARTS artifact failed its discovery, parsing, or lake contract."""


@dataclass(frozen=True)
class CensusArchiveEntry:
    target_period: date
    source_url: str


@dataclass(frozen=True)
class CensusMartsReleaseData:
    target_period: date
    prior_period: date
    release_at: datetime
    current_level: float
    prior_level: float
    flow: float
    source_url: str
    checksum: str
    retrieved_at: datetime


@dataclass(frozen=True)
class CensusArchiveReceipt:
    target_period: date
    release_at: datetime
    source_url: str
    checksum: str
    flow: float
    already_present: bool
    batch_path: Path | None
    content_hash: str | None


@dataclass(frozen=True)
class CensusArchiveValidation:
    total_rows: int
    earliest_period: date
    latest_period: date
    earliest_release: datetime
    latest_release: datetime


class CensusMartsArchiveClient:
    def __init__(
        self,
        transport: HttpTransport,
        definition: CensusArchiveDefinition | None = None,
    ) -> None:
        self._transport = transport
        self._definition = definition or load_census_archive()

    def discover(self, *, start: date = DEFAULT_START) -> tuple[CensusArchiveEntry, ...]:
        receipt = self._transport.get(
            self._definition.archive_index_url,
            headers={"Accept": "text/html"},
        )
        return discover_marts_releases(receipt.content, start=start, definition=self._definition)

    def fetch(self, entry: CensusArchiveEntry) -> CensusMartsReleaseData:
        receipt = self._transport.get(entry.source_url, headers={"Accept": "application/pdf"})
        return parse_marts_release(receipt, definition=self._definition)


class CensusArchiveIngestor:
    def __init__(
        self,
        store: AppendOnlyParquetStore,
        client: CensusMartsArchiveClient | None = None,
        definition: CensusArchiveDefinition | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._definition = definition or load_census_archive()
        self._known_rows: dict[tuple[str, str], pl.DataFrame] = {}
        self._checksums_by_url: dict[str, set[str]] = {}
        self._known_batches: dict[tuple[str, str], Path | None] = {}
        self._load_existing_index()

    def backfill(
        self,
        entries: Sequence[CensusArchiveEntry],
        *,
        recheck_complete: bool = False,
    ) -> tuple[CensusArchiveReceipt, ...]:
        if self._client is None:
            raise CensusArchiveError("backfill requires a Census archive client")
        receipts: list[CensusArchiveReceipt] = []
        for entry in entries:
            known = self._checksums_by_url.get(entry.source_url)
            if known and not recheck_complete:
                continue
            receipts.append(self.ingest(self._client.fetch(entry)))
        return tuple(receipts)

    def ingest(self, data: CensusMartsReleaseData) -> CensusArchiveReceipt:
        row = marts_release_row(data, definition=self._definition)
        identity = (data.source_url, data.checksum)
        different = self._checksums_by_url.get(data.source_url, set()) - {data.checksum}
        if different:
            raise CensusArchiveError(f"Dated MARTS artifact changed checksum: {data.source_url}")
        matching = self._known_rows.get(identity)
        if matching is not None:
            _verify_existing_row(matching, row)
            batch_path = self._known_batches.get(identity)
            return CensusArchiveReceipt(
                target_period=data.target_period,
                release_at=data.release_at,
                source_url=data.source_url,
                checksum=data.checksum,
                flow=data.flow,
                already_present=True,
                batch_path=batch_path,
                content_hash=(
                    batch_path.stem.removeprefix("batch-") if batch_path is not None else None
                ),
            )
        write = self._store.append("raw_observations", [row])
        stored = pl.read_parquet(write.path)
        self._known_rows[identity] = stored
        self._known_batches[identity] = write.path
        self._checksums_by_url.setdefault(data.source_url, set()).add(data.checksum)
        return CensusArchiveReceipt(
            target_period=data.target_period,
            release_at=data.release_at,
            source_url=data.source_url,
            checksum=data.checksum,
            flow=data.flow,
            already_present=write.already_present,
            batch_path=write.path,
            content_hash=write.content_hash,
        )

    def _load_existing_index(self) -> None:
        paths = sorted((self._store.root / "raw_observations").glob("batch-*.parquet"))
        for path in paths:
            frame = pl.read_parquet(path).filter(
                (pl.col("source") == self._definition.lake_source)
                & (pl.col("series_id") == self._definition.series_id)
            )
            if frame.is_empty():
                continue
            identities = frame.select(["source_url", "checksum"]).unique()
            for source_url, checksum in identities.iter_rows():
                identity = (str(source_url), str(checksum))
                matching = frame.filter(
                    (pl.col("source_url") == source_url) & (pl.col("checksum") == checksum)
                )
                prior = self._known_rows.get(identity)
                if prior is None:
                    self._known_rows[identity] = matching
                    self._known_batches[identity] = path
                else:
                    self._known_rows[identity] = pl.concat([prior, matching], how="vertical")
                    self._known_batches[identity] = None
                self._checksums_by_url.setdefault(str(source_url), set()).add(str(checksum))


def discover_marts_releases(
    content: bytes,
    *,
    start: date,
    definition: CensusArchiveDefinition | None = None,
) -> tuple[CensusArchiveEntry, ...]:
    definition = definition or load_census_archive()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CensusArchiveError("Census archive index is not UTF-8") from exc
    escaped_pattern = re.escape(
        definition.source_url_pattern.replace("YYMM", "__PERIOD__")
    ).replace("__PERIOD__", r"(?P<period>\d{4})")
    matches = list(re.finditer(escaped_pattern, text))
    entries_by_period: dict[date, CensusArchiveEntry] = {}
    for match in matches:
        raw = match.group("period")
        short_year = int(raw[:2])
        year = 2000 + short_year if short_year < 50 else 1900 + short_year
        month = int(raw[2:])
        if not 1 <= month <= 12:
            raise CensusArchiveError(f"Census archive contains an invalid month: {raw}")
        period = _month_end(date(year, month, 1))
        if period < start:
            continue
        entry = CensusArchiveEntry(target_period=period, source_url=match.group(0))
        prior = entries_by_period.get(period)
        if prior is not None and prior != entry:
            raise CensusArchiveError(f"Census archive has conflicting artifacts for {period}")
        entries_by_period[period] = entry
    entries = tuple(entries_by_period[key] for key in sorted(entries_by_period))
    if not entries:
        raise CensusArchiveError(f"Census archive has no releases since {start}")
    _validate_continuous_periods(tuple(item.target_period for item in entries))
    return entries


def parse_marts_release(
    receipt: HttpReceipt,
    *,
    definition: CensusArchiveDefinition | None = None,
) -> CensusMartsReleaseData:
    definition = definition or load_census_archive()
    expected_url = re.escape(definition.source_url_pattern).replace("YYMM", r"(?P<period>\d{4})")
    match = re.fullmatch(expected_url, receipt.source_url)
    if match is None:
        raise CensusArchiveError(f"MARTS artifact URL mismatch: {receipt.source_url!r}")
    raw_period = match.group("period")
    short_year = int(raw_period[:2])
    year = 2000 + short_year if short_year < 50 else 1900 + short_year
    target_period = _month_end(date(year, int(raw_period[2:]), 1))
    if not receipt.content.startswith(PDF_MAGIC):
        raise CensusArchiveError("MARTS artifact is not a PDF")
    if hashlib.sha256(receipt.content).hexdigest() != receipt.checksum:
        raise CensusArchiveError("MARTS artifact checksum does not match its bytes")
    if receipt.retrieved_at.tzinfo is None or receipt.retrieved_at.utcoffset() is None:
        raise CensusArchiveError("MARTS retrieval timestamp must be timezone-aware")
    try:
        reader = PdfReader(BytesIO(receipt.content))
        text_content = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PdfReadError, OSError, ValueError) as exc:
        raise CensusArchiveError("MARTS artifact cannot be read") from exc
    return parse_marts_release_text(
        text_content,
        target_period=target_period,
        source_url=receipt.source_url,
        checksum=receipt.checksum,
        retrieved_at=receipt.retrieved_at,
        definition=definition,
    )


def parse_marts_release_text(
    content: str,
    *,
    target_period: date,
    source_url: str,
    checksum: str,
    retrieved_at: datetime,
    definition: CensusArchiveDefinition | None = None,
) -> CensusMartsReleaseData:
    definition = definition or load_census_archive()
    compact = " ".join(content.split())
    release_at = _release_timestamp(compact, definition)
    expected_period_label = target_period.strftime("%B %Y").upper()
    if expected_period_label not in compact.upper():
        raise CensusArchiveError("MARTS PDF target period does not match its archive URL")
    if release_at.date() <= target_period:
        raise CensusArchiveError("MARTS release does not postdate its target month")
    if (release_at.date() - target_period).days > 100:
        raise CensusArchiveError("MARTS release lag exceeds the verified contract")

    table_index = compact.find("Table 1.")
    if table_index < 0 or definition.expected_table_title.casefold() not in compact.casefold():
        raise CensusArchiveError("MARTS PDF is missing the registered Table 1 title")
    table = compact[table_index:]
    row_match = re.search(
        r"Retail & food services,\s*total\b(?P<row>.*?)(?=\s*Total \(excl\.)",
        table,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if row_match is None:
        raise CensusArchiveError("MARTS Table 1 total row is missing")
    numeric = [
        float(raw.replace(",", "").replace(" ", ""))
        for raw in NUMBER_PATTERN.findall(row_match.group("row"))
    ]
    if len(numeric) != 12:
        raise CensusArchiveError(
            f"MARTS Table 1 total row shape changed: found {len(numeric)} numeric cells"
        )
    current_level = numeric[7]
    prior_level = numeric[8]
    if not all(isfinite(value) and value > 100_000 for value in (current_level, prior_level)):
        raise CensusArchiveError("MARTS adjusted total levels are invalid")
    flow = current_level - prior_level
    return CensusMartsReleaseData(
        target_period=target_period,
        prior_period=_previous_month_end(target_period),
        release_at=release_at,
        current_level=current_level,
        prior_level=prior_level,
        flow=flow,
        source_url=source_url,
        checksum=checksum,
        retrieved_at=retrieved_at.astimezone(UTC),
    )


def marts_release_row(
    data: CensusMartsReleaseData,
    *,
    definition: CensusArchiveDefinition | None = None,
) -> dict[str, object]:
    definition = definition or load_census_archive()
    expected_url = definition.source_url_pattern.replace(
        "YYMM", data.target_period.strftime("%y%m")
    )
    if data.source_url != expected_url:
        raise CensusArchiveError("MARTS release source URL does not match its target period")
    if data.prior_period != _previous_month_end(data.target_period):
        raise CensusArchiveError("MARTS release prior period is not contiguous")
    if data.flow != data.current_level - data.prior_level:
        raise CensusArchiveError("MARTS flow is not release-coherent")
    if not re.fullmatch(r"[0-9a-f]{64}", data.checksum):
        raise CensusArchiveError("MARTS checksum is not lowercase SHA-256")
    if data.release_at.tzinfo is None or data.release_at.utcoffset() is None:
        raise CensusArchiveError("MARTS release timestamp must be timezone-aware")
    release_at = data.release_at.astimezone(UTC)
    if (
        release_at.date() <= data.target_period
        or (release_at.date() - data.target_period).days > 100
    ):
        raise CensusArchiveError("MARTS release timestamp is outside the target-month boundary")
    if data.retrieved_at.tzinfo is None or data.retrieved_at.utcoffset() is None:
        raise CensusArchiveError("MARTS retrieval timestamp must be timezone-aware")
    if data.retrieved_at.astimezone(UTC) < release_at:
        raise CensusArchiveError("MARTS retrieval timestamp predates the release")
    return {
        "source": definition.lake_source,
        "series_id": definition.series_id,
        "obs_period": data.target_period,
        "value": data.flow,
        "unit": definition.units,
        "release_date": release_at,
        "vintage_date": release_at.date(),
        "ingested_at": data.retrieved_at.astimezone(UTC),
        "source_url": data.source_url,
        "checksum": data.checksum,
    }


def validate_census_archive(
    store: AppendOnlyParquetStore,
    entries: Sequence[CensusArchiveEntry],
    *,
    definition: CensusArchiveDefinition | None = None,
) -> CensusArchiveValidation:
    definition = definition or load_census_archive()
    frame = store.read_table("raw_observations").filter(
        (pl.col("source") == definition.lake_source) & (pl.col("series_id") == definition.series_id)
    )
    if frame.is_empty():
        raise CensusArchiveError("Census MARTS archive history is empty")
    expected = {entry.target_period: entry.source_url for entry in entries}
    actual_periods = set(frame["obs_period"].to_list())
    if actual_periods != set(expected):
        raise CensusArchiveError("Census MARTS archive period coverage mismatch")
    if frame.height != len(expected) or frame["obs_period"].n_unique() != frame.height:
        raise CensusArchiveError("Census MARTS archive contains duplicate releases")
    if frame.select((pl.col("unit") == definition.units).all()).item() is not True:
        raise CensusArchiveError("Census MARTS archive unit changed")
    if (
        frame.select((pl.col("vintage_date") == pl.col("release_date").dt.date()).all()).item()
        is not True
    ):
        raise CensusArchiveError("Census MARTS release and vintage dates disagree")
    if frame.select(pl.col("checksum").str.contains(r"^[0-9a-f]{64}$").all()).item() is not True:
        raise CensusArchiveError("Census MARTS archive contains an invalid checksum")
    for row in frame.iter_rows(named=True):
        period = row["obs_period"]
        if not isinstance(period, date) or row["source_url"] != expected.get(period):
            raise CensusArchiveError("Census MARTS archive provenance mismatch")
        if not isfinite(float(row["value"])):
            raise CensusArchiveError("Census MARTS archive contains a non-finite flow")
    ordered = frame.sort("obs_period")
    _validate_continuous_periods(tuple(ordered["obs_period"].to_list()))
    releases = ordered["release_date"].to_list()
    if any(
        not isinstance(previous, datetime)
        or not isinstance(current, datetime)
        or previous >= current
        for previous, current in pairwise(releases)
    ):
        raise CensusArchiveError("Census MARTS releases are not strictly increasing")
    earliest_period = ordered["obs_period"].min()
    latest_period = ordered["obs_period"].max()
    earliest_release = ordered["release_date"].min()
    latest_release = ordered["release_date"].max()
    if (
        not isinstance(earliest_period, date)
        or isinstance(earliest_period, datetime)
        or not isinstance(latest_period, date)
        or isinstance(latest_period, datetime)
        or not isinstance(earliest_release, datetime)
        or not isinstance(latest_release, datetime)
    ):
        raise CensusArchiveError("Census MARTS archive bounds are invalid")
    return CensusArchiveValidation(
        total_rows=frame.height,
        earliest_period=earliest_period,
        latest_period=latest_period,
        earliest_release=earliest_release,
        latest_release=latest_release,
    )


def _release_timestamp(content: str, definition: CensusArchiveDefinition) -> datetime:
    modern = MODERN_RELEASE_PATTERN.search(content)
    legacy = LEGACY_RELEASE_PATTERN.search(content)
    if modern is not None and legacy is not None:
        raise CensusArchiveError("MARTS PDF contains multiple release timestamp formats")
    if modern is not None:
        abbreviation, raw_date = modern.groups()
    elif legacy is not None:
        raw_date, abbreviation = legacy.groups()
    else:
        raise CensusArchiveError("MARTS PDF release timestamp is missing")
    release_date = datetime.strptime(raw_date.title(), "%B %d, %Y").replace(tzinfo=UTC).date()
    hour, minute = map(int, definition.release_time.split(":"))
    local = datetime.combine(release_date, time(hour, minute), ZoneInfo(definition.time_zone))
    if local.tzname() != abbreviation.upper().replace(" ", ""):
        raise CensusArchiveError("MARTS release timezone label disagrees with Eastern time")
    return local.astimezone(UTC)


def _validate_continuous_periods(periods: tuple[date, ...]) -> None:
    for previous, current in pairwise(periods):
        if _next_month_end(previous) != current:
            raise CensusArchiveError(f"Census MARTS archive has a gap: {previous} -> {current}")


def _month_end(period: date) -> date:
    return date(period.year, period.month, monthrange(period.year, period.month)[1])


def _previous_month_end(period: date) -> date:
    return period.replace(day=1) - date.resolution


def _next_month_end(period: date) -> date:
    year = period.year + int(period.month == 12)
    month = 1 if period.month == 12 else period.month + 1
    return _month_end(date(year, month, 1))


def _verify_existing_row(existing: pl.DataFrame, expected: Mapping[str, object]) -> None:
    columns = list(expected)
    if existing.height != 1 or tuple(existing.select(columns).row(0)) != tuple(
        expected[column] for column in columns
    ):
        raise CensusArchiveError(
            "Stored MARTS release with the same checksum does not match the live parse"
        )


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_date, default=DEFAULT_START)
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--recheck-complete", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = AppendOnlyParquetStore(args.lake_root)
    with HttpTransport(min_interval_seconds=0.5) as transport:
        client = CensusMartsArchiveClient(transport)
        entries = client.discover(start=args.start)
        receipts = CensusArchiveIngestor(store, client).backfill(
            entries,
            recheck_complete=args.recheck_complete,
        )
        validation = validate_census_archive(store, entries)
    print(
        json.dumps(
            {
                "discovered": len(entries),
                "attempted": len(receipts),
                "already_present": sum(item.already_present for item in receipts),
                "validation": asdict(validation),
            },
            sort_keys=True,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CensusArchiveError, SourceRequestError) as exc:
        raise SystemExit(f"BLOCKED: {exc}") from exc
