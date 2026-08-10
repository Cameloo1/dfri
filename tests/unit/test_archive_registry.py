from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from dfri.publish import archive_registry
from dfri.publish.archive_registry import ArchiveRegistryError, load_archive_citation


def test_pending_archive_has_no_placeholder_doi_or_citation_block_input() -> None:
    payload = json.loads(
        resources.files("dfri.publish")
        .joinpath("archive_registry_v1.json")
        .read_text(encoding="utf-8")
    )

    assert payload["status"] == "PENDING_CREDENTIAL"
    assert payload["concept_doi"] is None
    assert payload["version_doi"] is None
    assert load_archive_citation() is None


def _load_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: object) -> object:
    (tmp_path / "archive_registry_v1.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(archive_registry.resources, "files", lambda _package: tmp_path)
    return load_archive_citation()


def test_verified_archive_returns_citation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    citation = _load_payload(
        monkeypatch,
        tmp_path,
        {
            "schema_version": "v1",
            "status": "VERIFIED",
            "provider": "Zenodo",
            "concept_doi": "10.5281/zenodo.123",
            "version_doi": "10.5281/zenodo.456",
            "record_url": "https://doi.org/10.5281/zenodo.456",
            "verified_at": "2026-08-10T12:00:00+00:00",
        },
    )

    assert citation is not None
    assert citation.version_doi == "10.5281/zenodo.456"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(schema_version="v2"), "schema_version"),
        (lambda value: value.update(extra="field"), "fields"),
        (lambda value: value.update(status="UNKNOWN"), "status"),
        (lambda value: value.update(concept_doi="placeholder"), "placeholder"),
    ],
)
def test_pending_archive_rejects_invalid_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    payload = {
        "schema_version": "v1",
        "status": "PENDING_CREDENTIAL",
        "provider": "Zenodo",
        "concept_doi": None,
        "version_doi": None,
        "record_url": None,
        "verified_at": None,
    }
    assert callable(mutation)
    mutation(payload)
    with pytest.raises(ArchiveRegistryError, match=message):
        _load_payload(monkeypatch, tmp_path, payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("provider", "", "non-empty"),
        ("version_doi", "10.9999/not-zenodo", "DOI"),
        ("record_url", "https://example.com/record", "doi.org"),
    ],
)
def test_verified_archive_rejects_unverified_citation_claims(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    payload = {
        "schema_version": "v1",
        "status": "VERIFIED",
        "provider": "Zenodo",
        "concept_doi": "10.5281/zenodo.123",
        "version_doi": "10.5281/zenodo.456",
        "record_url": "https://doi.org/10.5281/zenodo.456",
        "verified_at": "2026-08-10T12:00:00+00:00",
    }
    payload[field] = value
    with pytest.raises(ArchiveRegistryError, match=message):
        _load_payload(monkeypatch, tmp_path, payload)
