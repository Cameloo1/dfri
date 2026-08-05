"""Federal Reserve Board SDMX and dated-release archive client."""

from __future__ import annotations

import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Final, Literal
from zoneinfo import ZoneInfo

from dfri.ingest.http import HttpReceipt, HttpTransport
from dfri.ingest.registry import (
    BoardSeriesDefinition,
    load_board_archive_exceptions,
)

BoardRelease = Literal["g19", "h8"]
RELEASE_URLS: Final[dict[BoardRelease, str]] = {
    "g19": "https://www.federalreserve.gov/releases/g19/data/FRB_g19_xml.zip",
    "h8": "https://www.federalreserve.gov/releases/h8/data/FRB_h8_xml.zip",
}
DATA_MEMBERS: Final[dict[BoardRelease, str]] = {
    "g19": "G19_data.xml",
    "h8": "H8_data.xml",
}
MAX_ARCHIVE_BYTES: Final = 32 * 1024 * 1024
MAX_XML_BYTES: Final = 256 * 1024 * 1024
MAX_RELEASE_PAGE_BYTES: Final = 4 * 1024 * 1024
MAX_RELEASE_DATES_BYTES: Final = 2 * 1024 * 1024
BOARD_TIME_ZONE: Final = ZoneInfo("America/New_York")
G19_RELEASE_TIME: Final = time(15, 0)
H8_RELEASE_TIME: Final = time(16, 15)
MONTHS: Final[dict[str, int]] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


class BoardContractError(ValueError):
    """Board data failed a pinned metadata, archive, or value contract."""


@dataclass(frozen=True)
class BoardObservation:
    period: date
    value: Decimal | None
    status: str


@dataclass(frozen=True)
class BoardSeries:
    series_id: str
    title: str
    attributes: dict[str, str]
    observations: tuple[BoardObservation, ...]


@dataclass(frozen=True)
class BoardReleaseData:
    release: BoardRelease
    source_url: str
    checksum: str
    retrieved_at: str
    series: dict[str, BoardSeries]


@dataclass(frozen=True)
class BoardVerification:
    series_id: str
    release: BoardRelease
    title: str
    units: str
    frequency: str
    observations: int
    source_url: str
    checksum: str


@dataclass(frozen=True)
class BoardVintageObservation:
    series_id: str
    period: date
    value: Decimal
    status: str


@dataclass(frozen=True)
class BoardDatedReleaseData:
    release: BoardRelease
    archive_date: date
    release_date: date
    release_at: datetime
    source_url: str
    checksum: str
    retrieved_at: datetime
    observations: tuple[BoardVintageObservation, ...]


@dataclass(frozen=True)
class BoardFirstPrintFlow:
    target_series_id: str
    level_series_id: str
    target_period: date
    previous_period: date
    target_level: Decimal
    previous_level: Decimal
    value: Decimal


@dataclass(frozen=True)
class BoardG19FirstPrintData:
    archive_date: date
    release_date: date
    release_at: datetime
    source_url: str
    checksum: str
    retrieved_at: datetime
    flows: tuple[BoardFirstPrintFlow, ...]


class FederalReserveBoardClient:
    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport
        self._archive_exceptions = {
            (exception.release, exception.manifest_date): exception
            for exception in load_board_archive_exceptions()
        }

    def fetch_release(self, release: BoardRelease) -> BoardReleaseData:
        receipt = self._transport.get(RELEASE_URLS[release])
        series = parse_sdmx_archive(receipt.content, release)
        return BoardReleaseData(
            release=release,
            source_url=receipt.source_url,
            checksum=receipt.checksum,
            retrieved_at=receipt.retrieved_at.isoformat(),
            series=series,
        )

    def verify_release(
        self, release: BoardRelease, definitions: tuple[BoardSeriesDefinition, ...]
    ) -> tuple[BoardVerification, ...]:
        data = self.fetch_release(release)
        applicable = tuple(
            definition for definition in definitions if definition.release == release
        )
        if not applicable:
            raise BoardContractError(f"No registered definitions for {release}")
        return verify_series(data, applicable)

    def fetch_archive_index(self, release: BoardRelease, year: int) -> HttpReceipt:
        if year < 1900 or year > 2200:
            raise ValueError("archive year is outside supported bounds")
        url = f"https://www.federalreserve.gov/releases/{release}/{year}.htm"
        return self._transport.get(url, headers={"Accept": "text/html"})

    def fetch_release_index(self, release: BoardRelease) -> HttpReceipt:
        """Fetch the Board-maintained release-date index spanning all published years."""

        url = f"https://www.federalreserve.gov/releases/{release}/releaseDates.json"
        return self._transport.get(url, headers={"Accept": "application/json"})

    def discover_release_dates(self, release: BoardRelease) -> tuple[date, ...]:
        receipt = self.fetch_release_index(release)
        return parse_release_dates_manifest(receipt.content, release)

    def discover_archive_dates(self, release: BoardRelease, year: int) -> tuple[date, ...]:
        receipt = self.fetch_archive_index(release, year)
        return parse_archive_dates(receipt.content, release)

    def fetch_dated_release(
        self, release: BoardRelease, release_date: date
    ) -> BoardDatedReleaseData:
        if release_date.year < 1900 or release_date.year > 2200:
            raise ValueError("release date is outside supported bounds")
        exception = self._archive_exceptions.get((release, release_date))
        archive_date = exception.archive_date if exception else release_date
        compact_date = archive_date.strftime("%Y%m%d")
        url = f"https://www.federalreserve.gov/releases/{release}/{compact_date}/"
        receipt = self._transport.get(url, headers={"Accept": "text/html"})
        declared_date, observations = parse_dated_release_page(receipt.content, release)
        expected_declared_date = exception.declared_release_date if exception else release_date
        if declared_date != expected_declared_date:
            raise BoardContractError(
                "Board dated release date mismatch: "
                f"{declared_date.isoformat()} != {expected_declared_date.isoformat()}"
            )
        return BoardDatedReleaseData(
            release=release,
            archive_date=archive_date,
            release_date=declared_date,
            release_at=release_timestamp(release, declared_date),
            source_url=receipt.source_url,
            checksum=receipt.checksum,
            retrieved_at=receipt.retrieved_at,
            observations=observations,
        )

    def fetch_g19_first_print(self, release_date: date) -> BoardG19FirstPrintData:
        """Fetch one dated G.19 page and derive its release-coherent monthly targets."""

        if release_date.year < 1900 or release_date.year > 2200:
            raise ValueError("release date is outside supported bounds")
        exception = self._archive_exceptions.get(("g19", release_date))
        archive_date = exception.archive_date if exception else release_date
        compact_date = archive_date.strftime("%Y%m%d")
        url = f"https://www.federalreserve.gov/releases/g19/{compact_date}/"
        receipt = self._transport.get(url, headers={"Accept": "text/html"})
        expected_declared_date = exception.declared_release_date if exception else release_date
        flows = parse_g19_first_print_flows(receipt.content, expected_declared_date)
        return BoardG19FirstPrintData(
            archive_date=archive_date,
            release_date=expected_declared_date,
            release_at=release_timestamp("g19", expected_declared_date),
            source_url=receipt.source_url,
            checksum=g19_first_print_checksum(expected_declared_date, flows),
            retrieved_at=receipt.retrieved_at,
            flows=flows,
        )


def parse_sdmx_archive(content: bytes, release: BoardRelease) -> dict[str, BoardSeries]:
    if len(content) > MAX_ARCHIVE_BYTES:
        raise BoardContractError("Board archive exceeds compressed-size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            member_name = DATA_MEMBERS[release]
            try:
                info = archive.getinfo(member_name)
            except KeyError as exc:
                raise BoardContractError(f"Board archive missing {member_name}") from exc
            if info.file_size > MAX_XML_BYTES:
                raise BoardContractError("Board XML exceeds uncompressed-size limit")
            xml_content = archive.read(member_name)
    except zipfile.BadZipFile as exc:
        raise BoardContractError("Board response is not a valid ZIP archive") from exc

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        raise BoardContractError("Board SDMX XML is malformed") from exc

    parsed: dict[str, BoardSeries] = {}
    for element in root.iter():
        if _local_name(element.tag) != "Series":
            continue
        series_id = element.attrib.get("SERIES_NAME")
        if not series_id:
            raise BoardContractError("Board SDMX series is missing SERIES_NAME")
        titles = [
            (child.text or "").strip()
            for child in element.iter()
            if _local_name(child.tag) == "AnnotationText" and (child.text or "").strip()
        ]
        if not titles:
            raise BoardContractError(f"{series_id} is missing its source title")
        observations = tuple(
            _parse_observation(child, series_id)
            for child in element.iter()
            if _local_name(child.tag) == "Obs"
        )
        parsed[series_id] = BoardSeries(
            series_id=series_id,
            title=titles[0],
            attributes=dict(element.attrib),
            observations=observations,
        )
    if not parsed:
        raise BoardContractError("Board SDMX archive contains no series")
    return parsed


def verify_series(
    data: BoardReleaseData, definitions: tuple[BoardSeriesDefinition, ...]
) -> tuple[BoardVerification, ...]:
    verified: list[BoardVerification] = []
    for definition in definitions:
        try:
            actual = data.series[definition.series_id]
        except KeyError as exc:
            raise BoardContractError(
                f"Registered Board series missing: {definition.series_id}"
            ) from exc
        if actual.title != definition.expected_title:
            raise BoardContractError(f"{definition.series_id} title mismatch: {actual.title!r}")
        for key, expected in definition.expected_source_attributes.items():
            if actual.attributes.get(key) != expected:
                raise BoardContractError(
                    f"{definition.series_id} attribute {key} mismatch: "
                    f"{actual.attributes.get(key)!r} != {expected!r}"
                )
        units = _normalized_units(actual.attributes)
        frequency = _normalized_frequency(actual.attributes)
        if units != definition.units:
            raise BoardContractError(f"{definition.series_id} units mismatch: {units!r}")
        if frequency != definition.frequency:
            raise BoardContractError(f"{definition.series_id} frequency mismatch: {frequency!r}")
        if not actual.observations:
            raise BoardContractError(f"{definition.series_id} has no observations")
        verified.append(
            BoardVerification(
                series_id=definition.series_id,
                release=data.release,
                title=actual.title,
                units=units,
                frequency=frequency,
                observations=len(actual.observations),
                source_url=data.source_url,
                checksum=data.checksum,
            )
        )
    return tuple(verified)


def parse_archive_dates(content: bytes, release: BoardRelease) -> tuple[date, ...]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BoardContractError("Board archive index is not UTF-8") from exc
    parser = _ArchiveLinkParser(release)
    parser.feed(text)
    dates = tuple(sorted(parser.dates))
    if not dates:
        raise BoardContractError(f"No dated {release.upper()} releases found")
    return dates


def parse_release_dates_manifest(content: bytes, release: BoardRelease) -> tuple[date, ...]:
    """Parse the JSON manifest used by the Board's official Release Dates page."""

    if len(content) > MAX_RELEASE_DATES_BYTES:
        raise BoardContractError("Board release-dates manifest exceeds size limit")
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoardContractError("Board release-dates manifest is not valid UTF-8 JSON") from exc
    if not isinstance(raw, list):
        raise BoardContractError("Board release-dates manifest root is not a list")

    parsed: set[date] = set()
    for year_entry in raw:
        if not isinstance(year_entry, dict):
            raise BoardContractError("Board release-dates year entry is not an object")
        year_value = year_entry.get("yearValue")
        months = year_entry.get("Months")
        if not isinstance(year_value, str) or re.fullmatch(r"\d{4}", year_value) is None:
            raise BoardContractError("Board release-dates yearValue is invalid")
        if not isinstance(months, list):
            raise BoardContractError("Board release-dates Months is not a list")
        for month_entry in months:
            if not isinstance(month_entry, dict):
                raise BoardContractError("Board release-dates month entry is not an object")
            month_value = month_entry.get("MonthValue")
            dates = month_entry.get("Dates")
            if not isinstance(month_value, str) or re.fullmatch(r"\d{6}", month_value) is None:
                raise BoardContractError("Board release-dates MonthValue is invalid")
            if not isinstance(dates, list):
                raise BoardContractError("Board release-dates Dates is not a list")
            if not month_value.startswith(year_value):
                raise BoardContractError("Board release-dates year and month disagree")
            for raw_date in dates:
                if not isinstance(raw_date, str) or re.fullmatch(r"\d{8}", raw_date) is None:
                    raise BoardContractError("Board release date is invalid")
                if not raw_date.startswith(month_value):
                    raise BoardContractError("Board release date is outside its month")
                try:
                    release_date = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:]))
                except ValueError as exc:
                    raise BoardContractError(f"Board release date is invalid: {raw_date}") from exc
                if release_date in parsed:
                    raise BoardContractError(f"Board release date is duplicated: {raw_date}")
                parsed.add(release_date)
    if not parsed:
        raise BoardContractError(f"No dated {release.upper()} releases found")
    return tuple(sorted(parsed))


def parse_dated_release(
    content: bytes, release: BoardRelease, expected_release_date: date
) -> tuple[BoardVintageObservation, ...]:
    """Parse first-print observations from an immutable dated Board release page."""

    parser, parsed_release_date = _parse_release_page(content)
    if parsed_release_date != expected_release_date:
        raise BoardContractError(
            "Board dated release date mismatch: "
            f"{parsed_release_date.isoformat()} != {expected_release_date.isoformat()}"
        )
    if release == "g19":
        return _parse_g19_dated(parser, parsed_release_date)
    return _parse_h8_dated(parser, parsed_release_date)


def parse_g19_first_print_flows(
    content: bytes, expected_release_date: date
) -> tuple[BoardFirstPrintFlow, ...]:
    """Parse release-coherent target and prior levels from an immutable G.19 page."""

    parser, parsed_release_date = _parse_release_page(content)
    if parsed_release_date != expected_release_date:
        raise BoardContractError(
            "Board dated release date mismatch: "
            f"{parsed_release_date.isoformat()} != {expected_release_date.isoformat()}"
        )
    table = _find_table(
        parser.tables,
        required=(
            "Consumer Credit Outstanding",
            "Seasonally adjusted. Billions of dollars except as noted.",
        ),
        forbidden=("(Levels)",),
    )
    target_period = _g19_target_period(table, parser.document_text, parsed_release_date)
    previous_period = _previous_month_end(target_period)
    target_header = _g19_period_header(table, target_period, required_status="p")
    previous_header = _g19_previous_period_header(table, target_header, previous_period)
    if _g19_header_status(previous_header) == "p":
        raise BoardContractError("G.19 previous-month level is unexpectedly preliminary")

    rows = _g19_adjusted_level_rows(table)
    target_values = (
        _g19_header_value(rows[0], target_header),
        _g19_header_value(rows[1], target_header),
        _g19_header_value(rows[2], target_header),
    )
    previous_values = (
        _g19_header_value(rows[0], previous_header),
        _g19_header_value(rows[1], previous_header),
        _g19_header_value(rows[2], previous_header),
    )
    _verify_component_sum(target_values, "G.19 first-print target month")
    _verify_component_sum(previous_values, "G.19 first-print previous month")

    mappings = (
        ("DELTA_DTCTLR.M", "DTCTLR.M", target_values[1], previous_values[1]),
        ("DELTA_DTCTLN.M", "DTCTLN.M", target_values[2], previous_values[2]),
    )
    return tuple(
        BoardFirstPrintFlow(
            target_series_id=target_series_id,
            level_series_id=level_series_id,
            target_period=target_period,
            previous_period=previous_period,
            target_level=target_level,
            previous_level=previous_level,
            value=target_level - previous_level,
        )
        for target_series_id, level_series_id, target_level, previous_level in mappings
    )


def g19_first_print_checksum(release_date: date, flows: tuple[BoardFirstPrintFlow, ...]) -> str:
    """Hash only stable first-print evidence, excluding volatile page-wrapper markup."""

    payload = {
        "release_date": release_date.isoformat(),
        "flows": [
            {
                "target_series_id": flow.target_series_id,
                "level_series_id": flow.level_series_id,
                "target_period": flow.target_period.isoformat(),
                "previous_period": flow.previous_period.isoformat(),
                "target_level": str(flow.target_level),
                "previous_level": str(flow.previous_level),
                "value": str(flow.value),
            }
            for flow in sorted(flows, key=lambda item: item.target_series_id)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_dated_release_page(
    content: bytes, release: BoardRelease
) -> tuple[date, tuple[BoardVintageObservation, ...]]:
    """Parse a dated Board page and return its own declared release date."""

    parser, parsed_release_date = _parse_release_page(content)
    if release == "g19":
        observations = _parse_g19_dated(parser, parsed_release_date)
    else:
        observations = _parse_h8_dated(parser, parsed_release_date)
    return parsed_release_date, observations


def _parse_release_page(content: bytes) -> tuple[_ReleasePageParser, date]:
    """Parse bounded HTML and extract the page-declared release date."""

    if len(content) > MAX_RELEASE_PAGE_BYTES:
        raise BoardContractError("Board dated release exceeds HTML-size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BoardContractError("Board dated release is not UTF-8") from exc
    parser = _ReleasePageParser()
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise BoardContractError("Board dated release HTML is malformed") from exc

    return parser, _page_release_date(parser.document_text)


def release_timestamp(release: BoardRelease, release_date: date) -> datetime:
    """Return the Board's authoritative scheduled release time in UTC."""

    release_time = G19_RELEASE_TIME if release == "g19" else H8_RELEASE_TIME
    return datetime.combine(release_date, release_time, BOARD_TIME_ZONE).astimezone(UTC)


class _ArchiveLinkParser(HTMLParser):
    def __init__(self, release: BoardRelease) -> None:
        super().__init__()
        self._pattern = re.compile(rf"(?:/releases/{release}/)?(\d{{8}})(?:/|$)", re.IGNORECASE)
        self.dates: set[date] = set()

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key.casefold() != "href" or value is None:
                continue
            match = self._pattern.search(value)
            if match:
                raw = match.group(1)
                self.dates.add(date(int(raw[:4]), int(raw[4:6]), int(raw[6:])))


@dataclass(frozen=True)
class _HtmlCell:
    tag: str
    attributes: dict[str, str]
    text: str


@dataclass(frozen=True)
class _HtmlTable:
    attributes: dict[str, str]
    heading: str
    subheading: str
    context: str
    unit: str
    rows: tuple[tuple[_HtmlCell, ...], ...]


class _ReleasePageParser(HTMLParser):
    """Small, fail-closed table parser for the Board's archived HTML releases."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._document_parts: list[str] = []
        self._heading_parts: list[str] | None = None
        self._heading_tag: str | None = None
        self._last_h3 = ""
        self._last_h4 = ""
        self._last_h5 = ""
        self._unit_parts: list[str] | None = None
        self._last_unit = ""
        self._table_attributes: dict[str, str] | None = None
        self._table_heading = ""
        self._table_subheading = ""
        self._table_context = ""
        self._table_unit = ""
        self._rows: list[tuple[_HtmlCell, ...]] = []
        self._row: list[_HtmlCell] | None = None
        self._cell_tag: str | None = None
        self._cell_attributes: dict[str, str] = {}
        self._cell_parts: list[str] = []
        self.tables: list[_HtmlTable] = []

    @property
    def document_text(self) -> str:
        return _clean_text(" ".join(self._document_parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        tag = tag.casefold()
        if tag in {"h3", "h4", "h5"} and self._table_attributes is None:
            self._heading_tag = tag
            self._heading_parts = []
        elif (
            tag == "span"
            and "tableunit" in attributes.get("class", "").casefold().split()
            and self._table_attributes is None
        ):
            self._unit_parts = []
        elif tag == "table":
            if self._table_attributes is not None:
                raise ValueError("nested table")
            self._table_attributes = attributes
            self._table_heading = self._last_h3
            self._table_subheading = self._last_h5
            self._table_context = self._last_h4
            self._table_unit = self._last_unit
            self._rows = []
        elif tag == "tr" and self._table_attributes is not None:
            if self._row is not None:
                self._finish_row()
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            if self._cell_tag is not None:
                self._finish_cell()
            self._cell_tag = tag
            self._cell_attributes = attributes
            self._cell_parts = []
        elif tag == "br":
            self._append_text(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"th", "td"} and self._cell_tag is not None and self._row is not None:
            self._finish_cell()
        elif tag == "tr" and self._row is not None:
            self._finish_row()
        elif tag == "table" and self._table_attributes is not None:
            if self._row is not None:
                self._finish_row()
            self.tables.append(
                _HtmlTable(
                    attributes=self._table_attributes,
                    heading=self._table_heading,
                    subheading=self._table_subheading,
                    context=self._table_context,
                    unit=self._table_unit,
                    rows=tuple(self._rows),
                )
            )
            self._table_attributes = None
            self._rows = []
        elif tag == self._heading_tag and self._heading_parts is not None:
            heading = _clean_text(" ".join(self._heading_parts))
            if tag == "h3":
                self._last_h3 = heading
            elif tag == "h4":
                self._last_h4 = heading
            elif tag == "h5":
                self._last_h5 = heading
            self._heading_tag = None
            self._heading_parts = None
        elif tag == "span" and self._unit_parts is not None:
            self._last_unit = _clean_text(" ".join(self._unit_parts))
            self._unit_parts = None

    def handle_data(self, data: str) -> None:
        self._document_parts.append(data)
        self._append_text(data)

    def _append_text(self, data: str) -> None:
        if self._cell_tag is not None:
            self._cell_parts.append(data)
        if self._heading_parts is not None:
            self._heading_parts.append(data)
        if self._unit_parts is not None:
            self._unit_parts.append(data)

    def _finish_cell(self) -> None:
        if self._cell_tag is None or self._row is None:
            return
        self._row.append(
            _HtmlCell(
                tag=self._cell_tag,
                attributes=self._cell_attributes,
                text=_clean_text(" ".join(self._cell_parts)),
            )
        )
        self._cell_tag = None
        self._cell_attributes = {}
        self._cell_parts = []

    def _finish_row(self) -> None:
        if self._row is None:
            return
        self._finish_cell()
        if self._row:
            self._rows.append(tuple(self._row))
        self._row = None


def _parse_g19_dated(
    parser: _ReleasePageParser, release_date: date
) -> tuple[BoardVintageObservation, ...]:
    adjusted = _find_table(
        parser.tables,
        required=(
            "Consumer Credit Outstanding",
            "Seasonally adjusted. Billions of dollars except as noted.",
        ),
        forbidden=("(Levels)",),
    )
    not_adjusted = _find_table(
        parser.tables,
        required=("Consumer Credit Outstanding (Levels)", "Not seasonally adjusted"),
    )
    target_period = _g19_target_period(adjusted, parser.document_text, release_date)
    _verify_g19_preliminary_header(adjusted, target_period)
    _verify_g19_preliminary_header(not_adjusted, target_period)

    sa_values = _g19_adjusted_values(adjusted)
    nsa_values = _g19_not_adjusted_values(not_adjusted)
    _verify_component_sum(sa_values, "G.19 seasonally adjusted")
    _verify_component_sum(nsa_values, "G.19 not seasonally adjusted")

    mappings = (
        ("DTCTL.M", sa_values[0]),
        ("DTCTLR.M", sa_values[1]),
        ("DTCTLN.M", sa_values[2]),
        ("DTCTL_N.M", nsa_values[0]),
        ("DTCTLR_N.M", nsa_values[1]),
        ("DTCTLN_N.M", nsa_values[2]),
    )
    return tuple(
        BoardVintageObservation(series_id, target_period, value, "p")
        for series_id, value in mappings
    )


def _parse_h8_dated(
    parser: _ReleasePageParser, release_date: date
) -> tuple[BoardVintageObservation, ...]:
    candidates = [table for table in parser.tables if _is_h8_all_banks_sa_table(table)]
    if len(candidates) != 1:
        raise BoardContractError(
            f"Expected one H.8 all-commercial-bank SA table, found {len(candidates)}"
        )
    table = candidates[0]
    week_cells = [
        cell
        for row in table.rows
        for cell in row
        if cell.tag == "th" and re.fullmatch(r"[A-Za-z]{3}\s+\d{1,2}", cell.text)
    ]
    if len(week_cells) != 4:
        raise BoardContractError(f"Expected four H.8 week headers, found {len(week_cells)}")
    periods = tuple(_h8_week_date(cell.text, release_date) for cell in week_cells)
    if tuple(sorted(set(periods))) != periods or any(period.weekday() != 2 for period in periods):
        raise BoardContractError("H.8 week headers are not four ordered Wednesdays")

    series_by_label = {
        "Consumer loans": "B1029NCBA",
        "Credit cards and other revolving plans": "B1247NCBA",
        "Other consumer loans": "B3248NCBA",
    }
    rows_by_label: dict[str, tuple[int, tuple[_HtmlCell, ...]]] = {}
    for row in table.rows:
        if len(row) < 2 or row[0].tag != "th":
            continue
        line_text = _clean_text(row[0].text)
        label = _strip_footnote(row[1].text)
        if label not in series_by_label:
            continue
        if not line_text.isdigit():
            raise BoardContractError(f"H.8 target row {label!r} is missing its line number")
        line = int(line_text)
        if label in rows_by_label:
            raise BoardContractError(f"H.8 target row {label!r} is duplicated")
        rows_by_label[label] = (line, row)
    if set(rows_by_label) != set(series_by_label):
        missing = sorted(set(series_by_label) - set(rows_by_label))
        raise BoardContractError(f"H.8 target table is missing rows: {missing}")

    line_numbers = tuple(rows_by_label[label][0] for label in series_by_label)
    if line_numbers not in {(15, 16, 17), (20, 21, 22)}:
        raise BoardContractError(
            f"H.8 target rows have unrecognized presentation lines: {line_numbers}"
        )

    values_by_label: dict[str, tuple[Decimal, ...]] = {}
    for label, (line, row) in rows_by_label.items():
        value_cells = tuple(cell for cell in row if cell.tag == "td")
        if len(value_cells) < 4:
            raise BoardContractError(f"H.8 line {line} has fewer than four weekly values")
        values_by_label[label] = tuple(
            _billions_to_millions(cell.text) for cell in value_cells[-4:]
        )
    for index in range(4):
        _verify_component_sum(
            (
                values_by_label["Consumer loans"][index],
                values_by_label["Credit cards and other revolving plans"][index],
                values_by_label["Other consumer loans"][index],
            ),
            f"H.8 {periods[index].isoformat()}",
        )

    return tuple(
        BoardVintageObservation(
            series_by_label[label], periods[index], values[index], "first_print"
        )
        for label, values in values_by_label.items()
        for index in range(4)
    )


def _page_release_date(document_text: str) -> date:
    match = re.search(
        r"\bRelease Date(?:\s*\*)?\s*:\s*([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b",
        document_text,
    )
    if not match:
        raise BoardContractError("Board dated release is missing its release date")
    raw = match.group(1)
    parts = raw.replace(",", "").split()
    month = MONTHS.get(parts[0][:3].casefold())
    if month is None:
        raise BoardContractError("Board dated release has an unknown release month")
    try:
        return date(int(parts[2]), month, int(parts[1]))
    except ValueError as exc:
        raise BoardContractError(f"Board dated release has invalid date {raw!r}") from exc


def _g19_target_period(table: _HtmlTable, document_text: str, release_date: date) -> date:
    old_layout = re.search(
        r"\b([A-Z][a-z]+)\s+(\d{4})\s+Release Date(?:\s*\*)?\s*:",
        document_text,
    )
    if old_layout:
        month = MONTHS.get(old_layout.group(1)[:3].casefold())
        year = int(old_layout.group(2))
    else:
        context = re.fullmatch(r"([A-Z][a-z]+)\s+(\d{4})", table.context)
        if context:
            month = MONTHS.get(context.group(1)[:3].casefold())
            year = int(context.group(2))
        else:
            month, year = _g19_period_from_header(table)
    if month is None:
        raise BoardContractError("G.19 dated release has an unknown target month")
    target_period = date(year, month, monthrange(year, month)[1])
    if target_period >= release_date:
        raise BoardContractError("G.19 target period is not before its release date")
    return target_period


def _g19_period_from_header(table: _HtmlTable) -> tuple[int | None, int]:
    preliminary = [
        cell
        for row in table.rows
        for cell in row
        if cell.tag == "th" and re.fullmatch(r"[A-Za-z]{3}\s+p", cell.text)
    ]
    if len(preliminary) != 1:
        raise BoardContractError(
            f"Expected one G.19 preliminary month header, found {len(preliminary)}"
        )
    month = MONTHS.get(preliminary[0].text[:3].casefold())
    referenced_ids = set(preliminary[0].attributes.get("headers", "").split())
    years = {
        int(cell.text)
        for row in table.rows
        for cell in row
        if cell.attributes.get("id") in referenced_ids and re.fullmatch(r"\d{4}", cell.text)
    }
    if len(years) != 1:
        raise BoardContractError(
            f"Expected one G.19 year for preliminary month, found {len(years)}"
        )
    return month, years.pop()


def _is_h8_all_banks_sa_table(table: _HtmlTable) -> bool:
    identity = (table.attributes.get("summary") or table.attributes.get("title") or "").strip()
    valid_identity = identity in {
        "Assets and Liabilities of Commercial Banks in the United States",
        "Table 2. Assets and Liabilities of Commercial Banks in the United States",
    }
    unit = (table.unit or table.subheading).removesuffix(".")
    valid_unit = unit == "Seasonally adjusted, billions of dollars"
    if table.attributes.get("id") == "h8t2":
        return valid_identity and valid_unit
    legacy_page = table.heading.startswith("H.8; Page 2 ") or table.subheading.startswith(
        "H.8; Page 2 "
    )
    return valid_identity and valid_unit and legacy_page


def _find_table(
    tables: list[_HtmlTable], *, required: tuple[str, ...], forbidden: tuple[str, ...] = ()
) -> _HtmlTable:
    matches = []
    for table in tables:
        text = _table_text(table)
        if all(value.casefold() in text.casefold() for value in required) and not any(
            value.casefold() in text.casefold() for value in forbidden
        ):
            matches.append(table)
    if len(matches) != 1:
        raise BoardContractError(
            f"Expected one Board table matching {required!r}, found {len(matches)}"
        )
    return matches[0]


def _verify_g19_preliminary_header(table: _HtmlTable, period: date) -> None:
    expected_month = tuple(MONTHS)[period.month - 1]
    headers = [
        cell
        for row in table.rows
        for cell in row
        if cell.tag == "th" and re.fullmatch(rf"{expected_month}\s+p", cell.text, re.IGNORECASE)
    ]
    if len(headers) != 1:
        raise BoardContractError(
            f"Expected one preliminary G.19 {expected_month.title()} header, found {len(headers)}"
        )


def _g19_period_header(
    table: _HtmlTable, period: date, *, required_status: str | None
) -> _HtmlCell:
    month_name = tuple(MONTHS)[period.month - 1]
    candidates: list[_HtmlCell] = []
    for row in table.rows:
        for cell in row:
            if cell.tag != "th" or not cell.attributes.get("id"):
                continue
            match = re.fullmatch(rf"{month_name}\s*([rp])?", cell.text, re.IGNORECASE)
            header_year = _g19_header_year(table, cell) if match else None
            if not match or (header_year is not None and header_year != period.year):
                continue
            status = match.group(1).casefold() if match.group(1) else None
            if required_status is not None and status != required_status:
                continue
            candidates.append(cell)
    if len(candidates) != 1:
        qualifier = f" {required_status}" if required_status else ""
        raise BoardContractError(
            f"Expected one G.19 {period:%b %Y}{qualifier} header, found {len(candidates)}"
        )
    return candidates[0]


def _g19_header_year(table: _HtmlTable, header: _HtmlCell) -> int | None:
    referenced_ids = set(header.attributes.get("headers", "").split())
    years = {
        int(cell.text)
        for row in table.rows
        for cell in row
        if cell.attributes.get("id") in referenced_ids and re.fullmatch(r"\d{4}", cell.text)
    }
    if len(years) > 1:
        raise BoardContractError(f"G.19 month header {header.text!r} references multiple years")
    return years.pop() if years else None


def _g19_header_status(header: _HtmlCell) -> str | None:
    match = re.fullmatch(r"[A-Za-z]{3}\s*([rp])?", header.text)
    if match is None:
        raise BoardContractError(f"G.19 month header is invalid: {header.text!r}")
    return match.group(1).casefold() if match.group(1) else None


def _g19_previous_period_header(
    table: _HtmlTable, target_header: _HtmlCell, previous_period: date
) -> _HtmlCell:
    try:
        return _g19_period_header(table, previous_period, required_status=None)
    except BoardContractError as exact_error:
        month_name = tuple(MONTHS)[previous_period.month - 1]
        candidates = [
            cell
            for row in table.rows
            for cell in row
            if cell.tag == "th"
            and cell.attributes.get("id")
            and re.fullmatch(rf"{month_name}\s*r?", cell.text, re.IGNORECASE)
        ]
        if len(candidates) != 1:
            raise exact_error
        candidate = candidates[0]
        adjacent = any(
            candidate in row
            and target_header in row
            and row.index(candidate) + 1 == row.index(target_header)
            for row in table.rows
        )
        if not adjacent:
            raise exact_error
        return candidate


def _g19_adjusted_level_rows(
    table: _HtmlTable,
) -> tuple[tuple[_HtmlCell, ...], tuple[_HtmlCell, ...], tuple[_HtmlCell, ...]]:
    for index, row in enumerate(table.rows):
        if _row_label(row) != "Total outstanding":
            continue
        following = table.rows[index : index + 3]
        labels = tuple(_strip_footnote(_row_label(candidate)) for candidate in following)
        if labels != ("Total outstanding", "Revolving", "Nonrevolving"):
            raise BoardContractError("G.19 adjusted level rows changed order or labels")
        return following  # type: ignore[return-value]
    raise BoardContractError("G.19 adjusted table is missing total outstanding")


def _g19_adjusted_values(table: _HtmlTable) -> tuple[Decimal, Decimal, Decimal]:
    return tuple(  # type: ignore[return-value]
        _last_row_value(row) for row in _g19_adjusted_level_rows(table)
    )


def _g19_header_value(row: tuple[_HtmlCell, ...], header: _HtmlCell) -> Decimal:
    header_id = header.attributes.get("id")
    matches = [
        cell
        for cell in row
        if cell.tag == "td" and header_id in cell.attributes.get("headers", "").split()
    ]
    if len(matches) != 1:
        raise BoardContractError(
            f"G.19 row {_row_label(row)!r} has {len(matches)} values for {header.text!r}"
        )
    return _billions_to_millions(matches[0].text)


def _g19_not_adjusted_values(table: _HtmlTable) -> tuple[Decimal, Decimal, Decimal]:
    total_row = next((row for row in table.rows if _row_label(row) == "Total"), None)
    if total_row is None:
        raise BoardContractError("G.19 NSA table is missing total")
    section_index = next(
        (
            index
            for index, row in enumerate(table.rows)
            if _row_label(row) == "Major types of credit, by holder"
        ),
        None,
    )
    if section_index is None:
        raise BoardContractError("G.19 NSA table is missing the credit-type section")
    component_rows: dict[str, tuple[_HtmlCell, ...]] = {}
    for row in table.rows[section_index + 1 :]:
        label = _strip_footnote(_row_label(row))
        if label in {"Revolving", "Nonrevolving"} and label not in component_rows:
            component_rows[label] = row
        if len(component_rows) == 2:
            break
    if set(component_rows) != {"Revolving", "Nonrevolving"}:
        raise BoardContractError("G.19 NSA table is missing revolving/nonrevolving totals")
    return (
        _last_row_value(total_row),
        _last_row_value(component_rows["Revolving"]),
        _last_row_value(component_rows["Nonrevolving"]),
    )


def _h8_week_date(raw: str, release_date: date) -> date:
    parts = raw.split()
    if len(parts) != 2:
        raise BoardContractError(f"H.8 week header has invalid date {raw!r}")
    month = MONTHS.get(parts[0][:3].casefold())
    if month is None:
        raise BoardContractError(f"H.8 week header has unknown month {raw!r}")
    try:
        candidate = date(release_date.year, month, int(parts[1]))
        if candidate > release_date:
            candidate = date(release_date.year - 1, month, int(parts[1]))
    except ValueError as exc:
        raise BoardContractError(f"H.8 week header has invalid date {raw!r}") from exc
    age = (release_date - candidate).days
    if age < 1 or age > 40:
        raise BoardContractError(f"H.8 week header is implausible for release: {raw!r}")
    return candidate


def _previous_month_end(period: date) -> date:
    first_of_month = period.replace(day=1)
    return first_of_month - timedelta(days=1)


def _last_row_value(row: tuple[_HtmlCell, ...]) -> Decimal:
    cells = [cell for cell in row if cell.tag == "td"]
    if not cells:
        raise BoardContractError(f"Board row has no values: {_row_label(row)!r}")
    return _billions_to_millions(cells[-1].text)


def _billions_to_millions(raw: str) -> Decimal:
    normalized = raw.replace(",", "").strip()
    if normalized.casefold() in {"", "n.a.", "...", "nan"}:
        raise BoardContractError(f"Required Board value is unavailable: {raw!r}")
    try:
        return Decimal(normalized) * Decimal(1000)
    except InvalidOperation as exc:
        raise BoardContractError(f"Board value is not numeric: {raw!r}") from exc


def _verify_component_sum(values: tuple[Decimal, Decimal, Decimal], context: str) -> None:
    total, first_component, second_component = values
    if abs(total - first_component - second_component) > Decimal(200):
        raise BoardContractError(f"{context} components do not sum to total within rounding")


def _row_label(row: tuple[_HtmlCell, ...]) -> str:
    return next((cell.text for cell in row if cell.tag == "th"), "")


def _strip_footnote(value: str) -> str:
    return re.sub(r"\s+\d+(?:\s*,\s*\d+)*$", "", value).strip()


def _table_text(table: _HtmlTable) -> str:
    return _clean_text(
        " ".join(
            (
                table.heading,
                table.subheading,
                table.context,
                table.unit,
                *(cell.text for row in table.rows for cell in row),
            )
        )
    )


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _parse_observation(element: ET.Element, series_id: str) -> BoardObservation:
    raw_period = element.attrib.get("TIME_PERIOD")
    raw_value = element.attrib.get("OBS_VALUE")
    status = element.attrib.get("OBS_STATUS", "")
    if not raw_period or raw_value is None:
        raise BoardContractError(f"{series_id} observation is missing period or value")
    try:
        period = date.fromisoformat(raw_period)
    except ValueError as exc:
        raise BoardContractError(f"{series_id} has invalid TIME_PERIOD {raw_period!r}") from exc
    if raw_value in {"", ".", "NaN"}:
        value = None
    else:
        try:
            value = Decimal(raw_value)
        except InvalidOperation as exc:
            raise BoardContractError(f"{series_id} has invalid OBS_VALUE {raw_value!r}") from exc
    return BoardObservation(period=period, value=value, status=status)


def _normalized_units(attributes: dict[str, str]) -> str:
    if attributes.get("UNIT") == "Currency" and attributes.get("UNIT_MULT") == "1000000":
        return "Millions of U.S. Dollars"
    return f"{attributes.get('UNIT', '')} x {attributes.get('UNIT_MULT', '')}".strip()


def _normalized_frequency(attributes: dict[str, str]) -> str:
    frequency = attributes.get("FREQ")
    if frequency == "129":
        return "Monthly"
    if frequency == "19":
        return "Weekly, Ending Wednesday"
    return f"Unknown ({frequency})"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]
