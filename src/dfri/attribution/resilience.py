"""Source registry and automatic assumption fallback selection."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from importlib import resources
from typing import Any

from dfri.attribution.registry import AttributionBundle, Prior


class SourceResilienceError(RuntimeError):
    """No permitted, available source can support an active assumption."""


@dataclass(frozen=True)
class AssumptionSource:
    source_id: str
    family: str
    independent_group: str
    data_url: str
    terms_url: str
    terms_status: str
    permissions_summary: str
    available: bool


@dataclass(frozen=True)
class SourceDegradation:
    assumption_id: str
    primary_source_id: str
    active_source_id: str
    reason: str
    band_multiplier: float
    effective_prior_low: float
    effective_prior_mid: float
    effective_prior_high: float


def load_source_registry() -> dict[str, AssumptionSource]:
    payload = json.loads(
        resources.files("dfri.attribution").joinpath("source_registry_v1.json").read_text("utf-8")
    )
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise SourceResilienceError("Assumption source registry schema changed")
    values = payload.get("sources")
    if not isinstance(values, list):
        raise SourceResilienceError("Assumption source registry is missing sources")
    sources: dict[str, AssumptionSource] = {}
    for item in values:
        if not isinstance(item, dict):
            raise SourceResilienceError("Assumption source row is malformed")
        source = AssumptionSource(
            source_id=_required_str(item, "source_id"),
            family=_required_str(item, "family"),
            independent_group=_required_str(item, "independent_group"),
            data_url=_required_str(item, "data_url"),
            terms_url=_required_str(item, "terms_url"),
            terms_status=_required_str(item, "terms_status"),
            permissions_summary=_required_str(item, "permissions_summary"),
            available=_required_bool(item, "available"),
        )
        if source.source_id in sources:
            raise SourceResilienceError(f"Duplicate assumption source: {source.source_id}")
        if source.available and source.terms_status != "PERMITTED":
            raise SourceResilienceError(
                f"Non-permitted source cannot be marked available: {source.source_id}"
            )
        sources[source.source_id] = source
    return sources


def resolve_assumption_sources(
    bundle: AttributionBundle,
    *,
    unavailable_source_ids: frozenset[str] = frozenset(),
) -> tuple[AttributionBundle, tuple[SourceDegradation, ...]]:
    if bundle.methodology_version != "1.2.0":
        return bundle, ()
    sources = load_source_registry()
    resolved = []
    notes: list[SourceDegradation] = []
    for assumption in bundle.assumptions:
        primary = sources.get(assumption.primary_source_id)
        if primary is None:
            raise SourceResilienceError(
                f"Unregistered primary source for {assumption.assumption_id}: "
                f"{assumption.primary_source_id}"
            )
        if len(assumption.fallback_source_ids) != len(set(assumption.fallback_source_ids)):
            raise SourceResilienceError(f"Duplicate fallback source for {assumption.assumption_id}")
        unregistered = [
            source_id for source_id in assumption.fallback_source_ids if source_id not in sources
        ]
        if unregistered:
            raise SourceResilienceError(
                f"Unregistered fallback source for {assumption.assumption_id}: {unregistered[0]}"
            )
        primary_available = (
            primary.available
            and primary.terms_status == "PERMITTED"
            and primary.source_id not in unavailable_source_ids
        )
        if primary_available:
            resolved.append(assumption)
            continue
        fallback = next(
            (
                sources[source_id]
                for source_id in assumption.fallback_source_ids
                if source_id in sources
                and sources[source_id].available
                and sources[source_id].terms_status == "PERMITTED"
                and source_id not in unavailable_source_ids
                and sources[source_id].independent_group != primary.independent_group
            ),
            None,
        )
        if fallback is None:
            raise SourceResilienceError(
                "BLOCKED: no permitted independent fallback remains for "
                f"{assumption.assumption_id} after {primary.source_id} became unavailable"
            )
        widened = _widen(assumption.prior, assumption.fallback_band_multiplier)
        resolved.append(replace(assumption, prior=widened))
        notes.append(
            SourceDegradation(
                assumption_id=assumption.assumption_id,
                primary_source_id=primary.source_id,
                active_source_id=fallback.source_id,
                reason=(
                    f"Primary source {primary.source_id} is unavailable or non-permitted; "
                    f"using registered independent fallback {fallback.source_id}."
                ),
                band_multiplier=assumption.fallback_band_multiplier,
                effective_prior_low=widened.low,
                effective_prior_mid=widened.mid,
                effective_prior_high=widened.high,
            )
        )
    return replace(bundle, assumptions=tuple(resolved)), tuple(notes)


def _widen(prior: Prior, multiplier: float) -> Prior:
    low = max(0.0, prior.mid - (prior.mid - prior.low) * multiplier)
    high = min(1.0, prior.mid + (prior.high - prior.mid) * multiplier)
    widened = Prior(low=low, mid=prior.mid, high=high)
    widened.validate("fallback")
    return widened


def _required_str(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise SourceResilienceError(f"Assumption source field is missing: {key}")
    return value


def _required_bool(item: dict[str, Any], key: str) -> bool:
    value = item.get(key)
    if not isinstance(value, bool):
        raise SourceResilienceError(f"Assumption source boolean is missing: {key}")
    return value
