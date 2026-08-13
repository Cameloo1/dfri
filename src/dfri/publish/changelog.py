"""Validated append-only source registry for the public DFRI changelog."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, cast

CHANGELOG_PATH: Final = Path(__file__).with_name("changelog_v1.json")
ENTRY_ID: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KINDS: Final = {"methodology", "model", "publication", "restatement", "source_fallback"}


class ChangelogError(RuntimeError):
    """The public changelog violates its stable append-only contract."""


@dataclass(frozen=True)
class ChangelogEntry:
    entry_id: str
    published_on: date
    kind: str
    version: str
    title: str
    summary: str
    links: tuple[tuple[str, str], ...]

    @property
    def permalink(self) -> str:
        return f"/changelog/#{self.entry_id}"

    def display(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "published_on": self.published_on.isoformat(),
            "kind": self.kind,
            "version": self.version,
            "title": self.title,
            "summary": self.summary,
            "permalink": self.permalink,
            "links": [{"label": label, "href": href} for label, href in self.links],
        }


def load_changelog(path: Path = CHANGELOG_PATH) -> tuple[ChangelogEntry, ...]:
    """Load and validate the ordered public changelog source registry."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChangelogError(f"Unable to load changelog: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "v1":
        raise ChangelogError("Changelog schema_version must be v1")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ChangelogError("Changelog entries must be a non-empty list")
    entries = tuple(_entry(cast(object, item)) for item in raw_entries)
    ids = [item.entry_id for item in entries]
    if len(ids) != len(set(ids)):
        raise ChangelogError("Changelog entry IDs must be unique")
    order = [(item.published_on, item.entry_id) for item in entries]
    if order != sorted(order):
        raise ChangelogError("Changelog entries must be ordered oldest first")
    return entries


def verify_append_only(candidate: Path, baseline: Path) -> tuple[ChangelogEntry, ...]:
    """Reject deletion, reordering, or modification of an already-published entry."""

    current = load_changelog(candidate)
    previous = load_changelog(baseline)
    if len(current) < len(previous) or current[: len(previous)] != previous:
        raise ChangelogError("Published changelog history was deleted, reordered, or modified")
    return current


def _entry(raw: object) -> ChangelogEntry:
    if not isinstance(raw, dict):
        raise ChangelogError("Each changelog entry must be an object")
    item = cast(Mapping[str, object], raw)
    required = {"entry_id", "published_on", "kind", "version", "title", "summary", "links"}
    if set(item) != required:
        raise ChangelogError("Changelog entry fields do not match the v1 contract")
    entry_id = _text(item["entry_id"], "entry_id")
    if ENTRY_ID.fullmatch(entry_id) is None:
        raise ChangelogError(f"Invalid changelog entry ID: {entry_id}")
    try:
        published_on = date.fromisoformat(_text(item["published_on"], "published_on"))
    except ValueError as exc:
        raise ChangelogError(f"Invalid changelog date for {entry_id}") from exc
    kind = _text(item["kind"], "kind")
    if kind not in KINDS:
        raise ChangelogError(f"Invalid changelog kind for {entry_id}: {kind}")
    raw_links = item["links"]
    if not isinstance(raw_links, list) or not raw_links:
        raise ChangelogError(f"Changelog entry {entry_id} must have at least one link")
    links: list[tuple[str, str]] = []
    for raw_link in raw_links:
        if not isinstance(raw_link, dict) or set(raw_link) != {"label", "href"}:
            raise ChangelogError(f"Invalid changelog link for {entry_id}")
        label = _text(raw_link["label"], "link label")
        href = _text(raw_link["href"], "link href")
        if not (href.startswith("/") or href.startswith("https://")):
            raise ChangelogError(f"Changelog link must be root-relative or HTTPS: {entry_id}")
        links.append((label, href))
    return ChangelogEntry(
        entry_id=entry_id,
        published_on=published_on,
        kind=kind,
        version=_text(item["version"], "version"),
        title=_text(item["title"], "title"),
        summary=_text(item["summary"], "summary"),
        links=tuple(links),
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChangelogError(f"Changelog {label} must be a non-empty string")
    return value.strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=CHANGELOG_PATH)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args(argv)
    entries = (
        verify_append_only(args.candidate, args.baseline)
        if args.baseline is not None
        else load_changelog(args.candidate)
    )
    print(json.dumps({"entries": len(entries), "status": "PASS"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
