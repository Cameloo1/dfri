"""Load and validate pinned source and series contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from importlib import resources
from typing import cast


class RegistryError(ValueError):
    """A checked-in registry is missing or violates its schema contract."""


@dataclass(frozen=True)
class BoardSeriesDefinition:
    series_id: str
    release: str
    expected_title: str
    units: str
    frequency: str
    expected_source_attributes: dict[str, str]
    license_note: str
    verified_at: datetime


@dataclass(frozen=True)
class BoardTargetDefinition:
    target_series_id: str
    level_series_id: str
    expected_title: str
    source_row_label: str
    source: str
    derived_source: str
    release: str
    derivation: str
    source_url_pattern: str
    units: str
    frequency: str
    verified_at: datetime


@dataclass(frozen=True)
class CensusArchiveDefinition:
    series_id: str
    source: str
    lake_source: str
    expected_title: str
    expected_table_title: str
    derivation: str
    archive_index_url: str
    source_url_pattern: str
    units: str
    frequency: str
    release_time: str
    time_zone: str
    license_note: str
    terms_url: str
    verified_at: datetime


@dataclass(frozen=True)
class ContextSeriesDefinition:
    series_id: str
    source: str
    expected_title: str
    units: str
    frequency: str
    expected_source_attributes: dict[str, str]
    license_note: str
    verified_at: datetime


@dataclass(frozen=True)
class NyFedSeriesDefinition:
    series_id: str
    expected_title: str
    units: str
    frequency: str
    expected_source_attributes: dict[str, str]
    license_note: str
    verified_at: datetime


@dataclass(frozen=True)
class BoardArchiveException:
    release: str
    manifest_date: date
    archive_date: date
    declared_release_date: date
    evidence_url: str
    finding: str
    verified_at: datetime


@dataclass(frozen=True)
class SourceContract:
    source_id: str
    status: str
    automated_access: bool
    storage: bool
    derivative_redistribution: bool
    active_publication: bool
    terms_url: str
    conditions: tuple[str, ...]
    finding: str


def _load_json(filename: str) -> dict[str, object]:
    text = resources.files("dfri.ingest").joinpath(filename).read_text(encoding="utf-8")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise RegistryError(f"{filename} must contain a JSON object")
    return cast(dict[str, object], parsed)


def load_board_series() -> tuple[BoardSeriesDefinition, ...]:
    payload = _load_json("series_registry.json")
    verified_raw = payload.get("verified_at")
    series_raw = payload.get("series")
    if not isinstance(verified_raw, str) or not isinstance(series_raw, list):
        raise RegistryError("series registry requires verified_at and series")
    verified_at = datetime.fromisoformat(verified_raw.replace("Z", "+00:00"))
    definitions: list[BoardSeriesDefinition] = []
    for item in series_raw:
        if not isinstance(item, dict) or item.get("source") != "federal_reserve_board":
            continue
        attributes = item.get("expected_source_attributes")
        if not isinstance(attributes, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in attributes.items()
        ):
            raise RegistryError("Board series attributes must be string mappings")
        required = ("series_id", "release", "expected_title", "units", "frequency", "license_note")
        if not all(isinstance(item.get(key), str) for key in required):
            raise RegistryError("Board series definition is missing a required string")
        definitions.append(
            BoardSeriesDefinition(
                series_id=cast(str, item["series_id"]),
                release=cast(str, item["release"]).casefold(),
                expected_title=cast(str, item["expected_title"]),
                units=cast(str, item["units"]),
                frequency=cast(str, item["frequency"]),
                expected_source_attributes=cast(dict[str, str], attributes),
                license_note=cast(str, item["license_note"]),
                verified_at=verified_at,
            )
        )
    if not definitions:
        raise RegistryError("No Federal Reserve Board series are registered")
    ids = [definition.series_id for definition in definitions]
    if len(ids) != len(set(ids)):
        raise RegistryError("Duplicate Board series_id")
    return tuple(definitions)


def load_board_targets() -> tuple[BoardTargetDefinition, ...]:
    """Load the pinned release-coherent G.19 first-print target definitions."""

    payload = _load_json("board_target_registry.json")
    required_shared = (
        "verified_at",
        "source",
        "derived_source",
        "release",
        "derivation",
        "source_url_pattern",
        "unit",
        "frequency",
    )
    if not all(isinstance(payload.get(key), str) for key in required_shared):
        raise RegistryError("Board target registry is missing a required string")
    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise RegistryError("Board target registry requires targets")
    verified_at = datetime.fromisoformat(cast(str, payload["verified_at"]).replace("Z", "+00:00"))
    definitions: list[BoardTargetDefinition] = []
    for item in targets:
        if not isinstance(item, dict):
            raise RegistryError("Board target definition must be an object")
        required = ("target_series_id", "level_series_id", "expected_title", "source_row_label")
        if not all(isinstance(item.get(key), str) for key in required):
            raise RegistryError("Board target definition is missing a required string")
        target_series_id = cast(str, item["target_series_id"])
        level_series_id = cast(str, item["level_series_id"])
        if target_series_id == level_series_id:
            raise RegistryError("Board target and source level series IDs must differ")
        definitions.append(
            BoardTargetDefinition(
                target_series_id=target_series_id,
                level_series_id=level_series_id,
                expected_title=cast(str, item["expected_title"]),
                source_row_label=cast(str, item["source_row_label"]),
                source=cast(str, payload["source"]),
                derived_source=cast(str, payload["derived_source"]),
                release=cast(str, payload["release"]).casefold(),
                derivation=cast(str, payload["derivation"]),
                source_url_pattern=cast(str, payload["source_url_pattern"]),
                units=cast(str, payload["unit"]),
                frequency=cast(str, payload["frequency"]),
                verified_at=verified_at,
            )
        )
    target_ids = [definition.target_series_id for definition in definitions]
    level_ids = [definition.level_series_id for definition in definitions]
    if len(target_ids) != len(set(target_ids)) or len(level_ids) != len(set(level_ids)):
        raise RegistryError("Board target registry contains duplicate series IDs")
    return tuple(definitions)


def load_census_archive() -> CensusArchiveDefinition:
    """Load the pinned first-print MARTS archive and derived-flow contract."""

    payload = _load_json("census_archive_registry.json")
    required = (
        "series_id",
        "source",
        "lake_source",
        "expected_title",
        "expected_table_title",
        "derivation",
        "archive_index_url",
        "source_url_pattern",
        "units",
        "frequency",
        "release_time",
        "time_zone",
        "license_note",
        "terms_url",
        "verified_at",
    )
    if not all(isinstance(payload.get(key), str) for key in required):
        raise RegistryError("Census archive registry is missing a required string")
    definition = CensusArchiveDefinition(
        series_id=cast(str, payload["series_id"]),
        source=cast(str, payload["source"]),
        lake_source=cast(str, payload["lake_source"]),
        expected_title=cast(str, payload["expected_title"]),
        expected_table_title=cast(str, payload["expected_table_title"]),
        derivation=cast(str, payload["derivation"]),
        archive_index_url=cast(str, payload["archive_index_url"]),
        source_url_pattern=cast(str, payload["source_url_pattern"]),
        units=cast(str, payload["units"]),
        frequency=cast(str, payload["frequency"]),
        release_time=cast(str, payload["release_time"]),
        time_zone=cast(str, payload["time_zone"]),
        license_note=cast(str, payload["license_note"]),
        terms_url=cast(str, payload["terms_url"]),
        verified_at=datetime.fromisoformat(
            cast(str, payload["verified_at"]).replace("Z", "+00:00")
        ),
    )
    if definition.series_id == "CENSUS:MARTS:44X72:SM:SA":
        raise RegistryError("Census first-print flow must not reuse the revised level series ID")
    return definition


def load_context_series() -> tuple[ContextSeriesDefinition, ...]:
    """Load live-verified BEA and Census replacements for legacy context aliases."""

    payload = _load_json("series_registry.json")
    verified_raw = payload.get("verified_at")
    series_raw = payload.get("series")
    if not isinstance(verified_raw, str) or not isinstance(series_raw, list):
        raise RegistryError("series registry requires verified_at and series")
    verified_at = datetime.fromisoformat(verified_raw.replace("Z", "+00:00"))
    definitions: list[ContextSeriesDefinition] = []
    for item in series_raw:
        if not isinstance(item, dict) or item.get("source") not in {"bea", "census"}:
            continue
        attributes = item.get("expected_source_attributes")
        if not isinstance(attributes, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in attributes.items()
        ):
            raise RegistryError("Context series attributes must be string mappings")
        required = ("series_id", "source", "expected_title", "units", "frequency", "license_note")
        if not all(isinstance(item.get(key), str) for key in required):
            raise RegistryError("Context series definition is missing a required string")
        definitions.append(
            ContextSeriesDefinition(
                series_id=cast(str, item["series_id"]),
                source=cast(str, item["source"]),
                expected_title=cast(str, item["expected_title"]),
                units=cast(str, item["units"]),
                frequency=cast(str, item["frequency"]),
                expected_source_attributes=cast(dict[str, str], attributes),
                license_note=cast(str, item["license_note"]),
                verified_at=verified_at,
            )
        )
    if not definitions:
        raise RegistryError("No BEA or Census context series are registered")
    ids = [definition.series_id for definition in definitions]
    if len(ids) != len(set(ids)):
        raise RegistryError("Duplicate context series_id")
    return tuple(definitions)


def load_nyfed_series() -> tuple[NyFedSeriesDefinition, ...]:
    """Load live-verified NY Fed HHDC workbook series contracts."""

    payload = _load_json("series_registry.json")
    verified_raw = payload.get("verified_at")
    series_raw = payload.get("series")
    if not isinstance(verified_raw, str) or not isinstance(series_raw, list):
        raise RegistryError("series registry requires verified_at and series")
    definitions: list[NyFedSeriesDefinition] = []
    for item in series_raw:
        if not isinstance(item, dict) or item.get("source") != "new_york_fed":
            continue
        attributes = item.get("expected_source_attributes")
        if not isinstance(attributes, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in attributes.items()
        ):
            raise RegistryError("NY Fed series attributes must be string mappings")
        required = ("series_id", "expected_title", "units", "frequency", "license_note")
        if not all(isinstance(item.get(key), str) for key in required):
            raise RegistryError("NY Fed series definition is missing a required string")
        item_verified = item.get("verified_at", verified_raw)
        if not isinstance(item_verified, str):
            raise RegistryError("NY Fed series verified_at must be a string")
        definitions.append(
            NyFedSeriesDefinition(
                series_id=cast(str, item["series_id"]),
                expected_title=cast(str, item["expected_title"]),
                units=cast(str, item["units"]),
                frequency=cast(str, item["frequency"]),
                expected_source_attributes=cast(dict[str, str], attributes),
                license_note=cast(str, item["license_note"]),
                verified_at=datetime.fromisoformat(item_verified.replace("Z", "+00:00")),
            )
        )
    if not definitions:
        raise RegistryError("No New York Fed HHDC series are registered")
    ids = [definition.series_id for definition in definitions]
    if len(ids) != len(set(ids)):
        raise RegistryError("Duplicate NY Fed series_id")
    return tuple(definitions)


def load_board_archive_exceptions() -> tuple[BoardArchiveException, ...]:
    """Load live-verified mismatches between Board manifests, paths, and page dates."""

    payload = _load_json("series_registry.json")
    raw_exceptions = payload.get("board_archive_exceptions")
    if not isinstance(raw_exceptions, list):
        raise RegistryError("series registry requires board_archive_exceptions")
    exceptions: list[BoardArchiveException] = []
    for item in raw_exceptions:
        if not isinstance(item, dict):
            raise RegistryError("Board archive exception must be an object")
        required = (
            "release",
            "manifest_date",
            "archive_date",
            "declared_release_date",
            "evidence_url",
            "finding",
            "verified_at",
        )
        if not all(isinstance(item.get(key), str) for key in required):
            raise RegistryError("Board archive exception is missing a required string")
        release = cast(str, item["release"]).casefold()
        if release not in {"g19", "h8"}:
            raise RegistryError(f"Unknown Board archive release: {release}")
        try:
            exceptions.append(
                BoardArchiveException(
                    release=release,
                    manifest_date=date.fromisoformat(cast(str, item["manifest_date"])),
                    archive_date=date.fromisoformat(cast(str, item["archive_date"])),
                    declared_release_date=date.fromisoformat(
                        cast(str, item["declared_release_date"])
                    ),
                    evidence_url=cast(str, item["evidence_url"]),
                    finding=cast(str, item["finding"]),
                    verified_at=datetime.fromisoformat(
                        cast(str, item["verified_at"]).replace("Z", "+00:00")
                    ),
                )
            )
        except ValueError as exc:
            raise RegistryError("Board archive exception has an invalid date") from exc
    keys = [(item.release, item.manifest_date) for item in exceptions]
    if len(keys) != len(set(keys)):
        raise RegistryError("Duplicate Board archive exception")
    return tuple(exceptions)


def load_source_contracts() -> dict[str, SourceContract]:
    payload = _load_json("source_contracts.json")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, dict):
        raise RegistryError("source contracts require a sources object")
    contracts: dict[str, SourceContract] = {}
    for source_id, raw in raw_sources.items():
        if not isinstance(source_id, str) or not isinstance(raw, dict):
            raise RegistryError("invalid source contract entry")
        conditions = raw.get("conditions")
        if not isinstance(conditions, list) or not all(
            isinstance(condition, str) for condition in conditions
        ):
            raise RegistryError(f"{source_id} conditions must be strings")
        contracts[source_id] = SourceContract(
            source_id=source_id,
            status=_required_str(raw, "status", source_id),
            automated_access=_required_bool(raw, "automated_access", source_id),
            storage=_required_bool(raw, "storage", source_id),
            derivative_redistribution=_required_bool(raw, "derivative_redistribution", source_id),
            active_publication=_optional_bool(raw, "active_publication", source_id, True),
            terms_url=_required_str(raw, "terms_url", source_id),
            conditions=tuple(conditions),
            finding=_required_str(raw, "finding", source_id),
        )
    return contracts


def load_sec_contracts() -> dict[str, object]:
    payload = _load_json("sec_contracts.json")
    required = {"edgar_surfaces", "reg_ab_ii", "card_trusts", "auto_abs_ee_evidence"}
    if not required.issubset(payload):
        raise RegistryError("SEC contracts registry is incomplete")
    return payload


def _required_str(raw: dict[object, object], key: str, source_id: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise RegistryError(f"{source_id}.{key} must be a string")
    return value


def _required_bool(raw: dict[object, object], key: str, source_id: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise RegistryError(f"{source_id}.{key} must be a boolean")
    return value


def _optional_bool(raw: dict[object, object], key: str, source_id: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise RegistryError(f"{source_id}.{key} must be a boolean")
    return value
