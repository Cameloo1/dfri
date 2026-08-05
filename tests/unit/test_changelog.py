from __future__ import annotations

import json
from pathlib import Path

import pytest

from dfri.publish.changelog import ChangelogError, load_changelog, verify_append_only


def test_public_changelog_is_ordered_permalinked_and_complete() -> None:
    entries = load_changelog()

    assert [item.entry_id for item in entries] == [
        "public-scoreboard-clock-started",
        "methodology-1-0-0",
        "registry-digest-cross-platform",
        "site-nowcast-units-provenance-and-orientation",
        "v1-1-methodology",
        "v1-1-quarterly-refresh-2026-q1",
    ]
    assert all(item.permalink == f"/changelog/#{item.entry_id}" for item in entries)
    assert {item.kind for item in entries} == {"publication", "methodology"}


def test_append_only_check_allows_append_and_rejects_rewrite(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    payload = {
        "schema_version": "v1",
        "entries": [
            {
                "entry_id": "first",
                "published_on": "2026-08-04",
                "kind": "publication",
                "version": "1",
                "title": "First",
                "summary": "First published state.",
                "links": [{"label": "Scoreboard", "href": "/scoreboard/"}],
            }
        ],
    }
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    payload["entries"].append(
        {
            "entry_id": "second",
            "published_on": "2026-08-05",
            "kind": "model",
            "version": "2",
            "title": "Second",
            "summary": "Second published state.",
            "links": [{"label": "Methodology", "href": "/methodology/"}],
        }
    )
    candidate.write_text(json.dumps(payload), encoding="utf-8")

    assert len(verify_append_only(candidate, baseline)) == 2
    payload["entries"][0]["summary"] = "Silently edited"
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ChangelogError, match="deleted, reordered, or modified"):
        verify_append_only(candidate, baseline)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update(schema_version="v2"), "schema_version"),
        (lambda payload: payload["entries"].append(payload["entries"][0]), "unique"),
        (lambda payload: payload["entries"][0].update(kind="unknown"), "kind"),
        (
            lambda payload: payload["entries"][0]["links"][0].update(href="http://unsafe"),
            "root-relative or HTTPS",
        ),
    ],
)
def test_changelog_rejects_invalid_contract(tmp_path: Path, mutation: object, message: str) -> None:
    payload = {
        "schema_version": "v1",
        "entries": [
            {
                "entry_id": "first",
                "published_on": "2026-08-04",
                "kind": "publication",
                "version": "1",
                "title": "First",
                "summary": "First published state.",
                "links": [{"label": "Scoreboard", "href": "/scoreboard/"}],
            }
        ],
    }
    assert callable(mutation)
    mutation(payload)
    path = tmp_path / "changelog.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ChangelogError, match=message):
        load_changelog(path)
