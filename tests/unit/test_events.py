from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime

from dfri.nowcast.bridge import BridgeForecast
from dfri.nowcast.targets import FirstPrintTarget
from dfri.publish.changelog import ChangelogEntry
from dfri.publish.events import build_events, canonical_json, json_feed, rss_feed
from dfri.publish.ledger import grade_record, prediction_record


def test_event_feeds_cover_predictions_grades_and_versioned_change_types() -> None:
    prediction = prediction_record(
        BridgeForecast(
            model_version="model-v1",
            target_series="DELTA_DTCTLR.M",
            target_period=date(2026, 7, 31),
            made_at=datetime(2026, 8, 7, 20, tzinfo=UTC),
            point=1_000,
            low80=500,
            high80=1_500,
            low95=0,
            high95=2_000,
            training_observations=100,
            inputs_hash="a" * 64,
        )
    )
    grade = grade_record(
        prediction,
        FirstPrintTarget(
            target_series=prediction.target_series,
            level_series="DTCTLR.M",
            target_period=prediction.target_period,
            value=1_250,
            unit="Millions of U.S. Dollars",
            release_at=datetime(2026, 9, 8, 19, tzinfo=UTC),
            vintage_date=date(2026, 9, 8),
            source_url="https://www.federalreserve.gov/releases/g19/20260908/",
            checksum="b" * 64,
        ),
    )
    changelog = tuple(
        ChangelogEntry(
            entry_id=f"entry-{kind.replace('_', '-')}",
            published_on=date(2026, 8, index + 1),
            kind=kind,
            version="1.0",
            title=f"{kind} event",
            summary="Versioned evidence.",
            links=(("Evidence", "/methodology/"),),
        )
        for index, kind in enumerate(("restatement", "methodology", "source_fallback"))
    )

    events = build_events((prediction,), (grade,), changelog, site_url="https://example.test/dfri/")
    payload = json_feed(events, generated_at=datetime(2026, 9, 8, 20, tzinfo=UTC))
    rss = rss_feed(events, site_url="https://example.test/dfri/")
    types = {item.event_type for item in events}

    assert {
        "new_prediction",
        "new_grade",
        "restatement",
        "methodology_change",
        "source_fallback_activation",
    } <= types
    assert canonical_json(payload) == canonical_json(payload)
    assert payload["schema_version"] == "v1"
    assert len(payload["data"]) == 5
    assert ET.fromstring(rss).tag == "rss"
    assert prediction.prediction_id.encode() in rss
