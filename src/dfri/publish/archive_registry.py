"""Validated DOI registry used to render citation blocks only after a verified deposit."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Final, cast

DOI: Final = re.compile(r"^10\.5281/zenodo\.[0-9]+$")


class ArchiveRegistryError(RuntimeError):
    """Archive citation metadata is unsupported or claims an unverified DOI."""


@dataclass(frozen=True)
class ArchiveCitation:
    provider: str
    concept_doi: str
    version_doi: str
    record_url: str
    verified_at: str


def load_archive_citation() -> ArchiveCitation | None:
    payload = json.loads(
        resources.files("dfri.publish")
        .joinpath("archive_registry_v1.json")
        .read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict) or payload.get("schema_version") != "v1":
        raise ArchiveRegistryError("Archive registry schema_version must be v1")
    required = {
        "schema_version",
        "status",
        "provider",
        "concept_doi",
        "version_doi",
        "record_url",
        "verified_at",
    }
    if set(payload) != required:
        raise ArchiveRegistryError("Archive registry fields do not match the v1 contract")
    if payload["status"] == "PENDING_CREDENTIAL":
        citation_keys = ("concept_doi", "version_doi", "record_url", "verified_at")
        if any(payload[key] is not None for key in citation_keys):
            raise ArchiveRegistryError(
                "Pending archive registry cannot contain placeholder citation data"
            )
        return None
    if payload["status"] != "VERIFIED":
        raise ArchiveRegistryError("Archive registry status must be PENDING_CREDENTIAL or VERIFIED")
    verified_keys = ("provider", "concept_doi", "version_doi", "record_url", "verified_at")
    values = {key: payload[key] for key in verified_keys}
    if not all(isinstance(value, str) and value.strip() for value in values.values()):
        raise ArchiveRegistryError("Verified archive citation fields must be non-empty strings")
    concept_doi = cast(str, payload["concept_doi"])
    version_doi = cast(str, payload["version_doi"])
    record_url = cast(str, payload["record_url"])
    if DOI.fullmatch(concept_doi) is None or DOI.fullmatch(version_doi) is None:
        raise ArchiveRegistryError("Verified archive DOI does not match the Zenodo DOI contract")
    if record_url != f"https://doi.org/{version_doi}":
        raise ArchiveRegistryError("Verified archive record URL must resolve through doi.org")
    return ArchiveCitation(
        provider=cast(str, payload["provider"]),
        concept_doi=concept_doi,
        version_doi=version_doi,
        record_url=record_url,
        verified_at=cast(str, payload["verified_at"]),
    )
