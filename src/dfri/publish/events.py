"""Build deterministic JSON and RSS event feeds from versioned publication records."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time
from email.utils import format_datetime
from html import escape
from typing import Final

from dfri.publish.changelog import ChangelogEntry
from dfri.publish.ledger import GradeRecord, PredictionRecord

EVENT_SCHEMA_VERSION: Final = "v1"
TARGET_LABELS: Final = {
    "DELTA_DTCTLR.M": "Revolving credit flow",
    "DELTA_DTCTLN.M": "Nonrevolving credit flow",
    "MTS:DEFICIT.M": "Federal deficit",
    "MTS:OUTLAYS.M": "Federal outlays",
}
CHANGE_TYPES: Final = {
    "methodology": "methodology_change",
    "model": "model_change",
    "publication": "publication_change",
    "restatement": "restatement",
    "source_fallback": "source_fallback_activation",
}


class EventFeedError(RuntimeError):
    """A distribution event is ambiguous, mutable, or cannot be linked."""


@dataclass(frozen=True)
class PublicationEvent:
    event_id: str
    event_type: str
    occurred_at: str
    date_precision: str
    title: str
    summary: str
    url: str
    target_series: str | None


def build_events(
    predictions: tuple[PredictionRecord, ...],
    grades: tuple[GradeRecord, ...],
    changelog: tuple[ChangelogEntry, ...],
    *,
    site_url: str,
) -> tuple[PublicationEvent, ...]:
    """Return immutable events newest first, with IDs derived from source records."""

    base = _site_url(site_url)
    prediction_by_id = {item.prediction_id: item for item in predictions}
    events: list[PublicationEvent] = []
    for prediction_item in predictions:
        label = TARGET_LABELS.get(prediction_item.target_series, prediction_item.target_series)
        events.append(
            PublicationEvent(
                event_id=f"prediction:{prediction_item.prediction_id}",
                event_type="new_prediction",
                occurred_at=prediction_item.made_at.astimezone(UTC).isoformat(),
                date_precision="second",
                title=f"New prediction: {label}",
                summary=(
                    f"{prediction_item.point:,.0f} million U.S. dollars; 80% band "
                    f"{prediction_item.low80:,.0f} to {prediction_item.high80:,.0f}; 95% band "
                    f"{prediction_item.low95:,.0f} to {prediction_item.high95:,.0f}."
                ),
                url=f"{base}scoreboard/predictions/{prediction_item.prediction_id}/",
                target_series=prediction_item.target_series,
            )
        )
    for grade_item in grades:
        prediction = prediction_by_id.get(grade_item.prediction_id)
        if prediction is None:
            raise EventFeedError(f"Grade has no prediction event: {grade_item.prediction_id}")
        label = TARGET_LABELS.get(prediction.target_series, prediction.target_series)
        events.append(
            PublicationEvent(
                event_id=f"grade:{grade_item.prediction_id}",
                event_type="new_grade",
                occurred_at=grade_item.graded_at.astimezone(UTC).isoformat(),
                date_precision="second",
                title=f"New grade: {label}",
                summary=(
                    f"First print {grade_item.actual_first_print:,.0f} million U.S. dollars; "
                    f"absolute error {grade_item.abs_error:,.0f} million U.S. dollars."
                ),
                url=f"{base}scoreboard/predictions/{grade_item.prediction_id}/",
                target_series=prediction.target_series,
            )
        )
    for changelog_item in changelog:
        occurred_at = datetime.combine(changelog_item.published_on, time(), tzinfo=UTC).isoformat()
        events.append(
            PublicationEvent(
                event_id=f"changelog:{changelog_item.entry_id}",
                event_type=CHANGE_TYPES[changelog_item.kind],
                occurred_at=occurred_at,
                date_precision="day",
                title=changelog_item.title,
                summary=changelog_item.summary,
                url=f"{base}changelog/#{changelog_item.entry_id}",
                target_series=None,
            )
        )
    ids = [item.event_id for item in events]
    if len(ids) != len(set(ids)):
        raise EventFeedError("Event IDs must be unique")
    return tuple(sorted(events, key=lambda item: (item.occurred_at, item.event_id), reverse=True))


def json_feed(events: tuple[PublicationEvent, ...], *, generated_at: datetime) -> dict[str, object]:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise EventFeedError("generated_at must be timezone-aware")
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "event_types": sorted(set(CHANGE_TYPES.values()) | {"new_prediction", "new_grade"}),
        "data": [asdict(item) for item in events],
    }


def rss_feed(events: tuple[PublicationEvent, ...], *, site_url: str) -> bytes:
    base = _site_url(site_url)
    items: list[str] = []
    for item in events:
        published = datetime.fromisoformat(item.occurred_at)
        items.append(
            "".join(
                (
                    "    <item>\n",
                    f'      <guid isPermaLink="false">{escape(item.event_id)}</guid>\n',
                    f"      <title>{escape(item.title)}</title>\n",
                    f"      <link>{escape(item.url)}</link>\n",
                    f"      <description>{escape(item.summary)}</description>\n",
                    f"      <pubDate>{format_datetime(published, usegmt=True)}</pubDate>\n",
                    f"      <category>{escape(item.event_type)}</category>\n",
                    "    </item>\n",
                )
            )
        )
    content = "".join(items)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        "    <title>DFRI publication events</title>\n"
        f"    <link>{escape(base)}</link>\n"
        "    <description>New DFRI predictions, grades, restatements, source fallbacks, "
        "and methodology changes.</description>\n"
        '    <atom:link xmlns:atom="http://www.w3.org/2005/Atom" '
        f'href="{escape(base)}events.xml" rel="self" '
        'type="application/rss+xml" />\n'
        f"{content}"
        "  </channel>\n"
        "</rss>\n"
    )
    return xml.encode()


def canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _site_url(value: str) -> str:
    if not value.startswith("https://"):
        raise EventFeedError("site_url must be HTTPS")
    return value.rstrip("/") + "/"
