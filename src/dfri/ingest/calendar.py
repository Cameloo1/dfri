"""Deterministic 12-month release-calendar seed with explicit unknowns."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from importlib import resources
from typing import cast
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
WINDOW_START = date(2026, 8, 1)
WINDOW_END = date(2027, 7, 31)
H8_FRIDAY_HOLIDAYS = frozenset({date(2026, 12, 25), date(2027, 1, 1), date(2027, 6, 18)})
CENSUS_DATES = {
    date(2026, 8, 1): date(2026, 8, 14),
    date(2026, 9, 1): date(2026, 9, 16),
    date(2026, 10, 1): date(2026, 10, 15),
    date(2026, 11, 1): date(2026, 11, 17),
    date(2026, 12, 1): date(2026, 12, 16),
}
BEA_DATES = {
    date(2026, 8, 1): date(2026, 8, 26),
    date(2026, 9, 1): date(2026, 9, 30),
    date(2026, 10, 1): date(2026, 10, 29),
    date(2026, 11, 1): date(2026, 11, 25),
    date(2026, 12, 1): date(2026, 12, 23),
}
NY_FED_DATE_ONLY = {
    date(2026, 8, 1): date(2026, 8, 4),
    date(2026, 11, 1): date(2026, 11, 3),
}


def release_calendar_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    months = _months(WINDOW_START, WINDOW_END)
    for month in months:
        label = month.strftime("%Y-%m")
        rows.append(_blocked(f"G.19 {label}", "BLOCKED_DATE_UNANNOUNCED"))

        census_date = CENSUS_DATES.get(month)
        rows.append(
            _expected(f"Census MARTS release {label}", census_date, time(8, 30))
            if census_date
            else _blocked(f"Census MARTS release {label}", "BLOCKED_DATE_UNANNOUNCED")
        )

        bea_date = BEA_DATES.get(month)
        rows.append(
            _expected(f"BEA Personal Income and Outlays {label}", bea_date, time(8, 30))
            if bea_date
            else _blocked(f"BEA Personal Income and Outlays {label}", "BLOCKED_DATE_UNANNOUNCED")
        )

    rows.extend(_h8_rows())
    for month in (date(2026, 8, 1), date(2026, 11, 1), date(2027, 2, 1), date(2027, 5, 1)):
        announced = NY_FED_DATE_ONLY.get(month)
        suffix = announced.isoformat() if announced else month.strftime("%Y-%m")
        status = "BLOCKED_TIME_UNANNOUNCED" if announced else "BLOCKED_DATE_UNANNOUNCED"
        rows.append(_blocked(f"NY Fed HHDC {suffix}", status))
    rows.sort(key=_row_sort_key)
    return rows


def release_calendar_evidence() -> dict[str, object]:
    text = (
        resources.files("dfri.ingest")
        .joinpath("release_calendar_sources.json")
        .read_text(encoding="utf-8")
    )
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("release calendar evidence must be a JSON object")
    return cast(dict[str, object], payload)


def serializable_calendar_rows() -> list[dict[str, object]]:
    return [
        {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in row.items()
        }
        for row in release_calendar_rows()
    ]


def _h8_rows() -> list[dict[str, object]]:
    first_friday = WINDOW_START + timedelta(days=(4 - WINDOW_START.weekday()) % 7)
    friday = first_friday
    rows: list[dict[str, object]] = []
    while friday <= WINDOW_END:
        release_date = friday - timedelta(days=1) if friday in H8_FRIDAY_HOLIDAYS else friday
        rows.append(_expected(f"H.8 week {friday.isoformat()}", release_date, time(16, 15)))
        friday += timedelta(days=7)
    return rows


def _expected(release_name: str, release_date: date, release_time: time) -> dict[str, object]:
    expected = datetime.combine(release_date, release_time, tzinfo=EASTERN).astimezone(UTC)
    return {
        "release_name": release_name,
        "expected_at": expected,
        "actual_at": None,
        "status": "EXPECTED_OFFICIAL" if not release_name.startswith("H.8") else "EXPECTED_RULE",
    }


def _blocked(release_name: str, status: str) -> dict[str, object]:
    return {
        "release_name": release_name,
        "expected_at": None,
        "actual_at": None,
        "status": status,
    }


def _months(start: date, end: date) -> list[date]:
    current = start.replace(day=1)
    months: list[date] = []
    while current <= end:
        months.append(current)
        current = (
            date(current.year + 1, 1, 1)
            if current.month == 12
            else date(current.year, current.month + 1, 1)
        )
    return months


def _row_sort_key(row: dict[str, object]) -> tuple[str, str]:
    expected = row["expected_at"]
    expected_key = expected.isoformat() if isinstance(expected, datetime) else "9999"
    return expected_key, str(row["release_name"])
