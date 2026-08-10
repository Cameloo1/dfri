from __future__ import annotations

from dataclasses import replace

import pytest

import dfri.attribution.resilience as resilience_module
from dfri.attribution.engine import run_attribution
from dfri.attribution.registry import load_attribution_bundle
from dfri.attribution.resilience import SourceResilienceError, resolve_assumption_sources


def test_normal_attribution_uses_primary_sources_without_rewriting() -> None:
    bundle = load_attribution_bundle()
    resolved, notes = resolve_assumption_sources(bundle)

    assert resolved == bundle
    assert notes == ()
    assert run_attribution(bundle).source_degradations == ()


def test_unavailable_ffiec_source_switches_to_ncua_and_widens_the_band() -> None:
    bundle = load_attribution_bundle()
    original = bundle.assumptions_by_id["A-T2-NONREV-AUTO-002"]
    resolved, notes = resolve_assumption_sources(
        bundle,
        unavailable_source_ids=frozenset({"ffiec_call_reports"}),
    )
    effective = resolved.assumptions_by_id[original.assumption_id]

    assert len(notes) == 1
    assert notes[0].active_source_id == "ncua_call_reports"
    assert effective.prior.low == notes[0].effective_prior_low < original.prior.low
    assert effective.prior.high == notes[0].effective_prior_high > original.prior.high
    result = run_attribution(
        bundle,
        unavailable_source_ids=frozenset({"ffiec_call_reports"}),
    )
    assert result.source_degradations == notes


def test_nonpermitted_primary_uses_the_registered_independent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = resilience_module.load_source_registry()
    primary = sources["ffiec_call_reports"]
    sources[primary.source_id] = replace(
        primary,
        terms_status="NONPERMITTED",
        available=False,
    )
    monkeypatch.setattr(resilience_module, "load_source_registry", lambda: sources)

    _, notes = resolve_assumption_sources(load_attribution_bundle())

    assert notes[0].primary_source_id == "ffiec_call_reports"
    assert notes[0].active_source_id == "ncua_call_reports"


def test_missing_fallback_fails_with_an_explicit_blocked_marker() -> None:
    bundle = load_attribution_bundle()
    assumption = bundle.assumptions_by_id["A-T2-NONREV-AUTO-002"]
    broken = replace(assumption, fallback_source_ids=())
    assumptions = tuple(broken if item == assumption else item for item in bundle.assumptions)

    with pytest.raises(SourceResilienceError, match="BLOCKED"):
        resolve_assumption_sources(
            replace(bundle, assumptions=assumptions),
            unavailable_source_ids=frozenset({"ffiec_call_reports"}),
        )


def test_unregistered_fallback_is_rejected_even_while_the_primary_is_available() -> None:
    bundle = load_attribution_bundle()
    assumption = bundle.assumptions_by_id["A-T2-NONREV-AUTO-002"]
    broken = replace(assumption, fallback_source_ids=("unknown_source",))
    assumptions = tuple(broken if item == assumption else item for item in bundle.assumptions)

    with pytest.raises(SourceResilienceError, match="Unregistered fallback"):
        resolve_assumption_sources(replace(bundle, assumptions=assumptions))


def test_legacy_methodology_does_not_apply_the_new_source_policy() -> None:
    bundle = load_attribution_bundle("1.1.1")
    assert resolve_assumption_sources(bundle) == (bundle, ())


def test_unregistered_primary_and_duplicate_fallbacks_fail_closed() -> None:
    bundle = load_attribution_bundle()
    assumption = bundle.assumptions_by_id["A-T2-NONREV-AUTO-002"]

    broken_primary = replace(assumption, primary_source_id="unknown_source")
    assumptions = tuple(
        broken_primary if item == assumption else item for item in bundle.assumptions
    )
    with pytest.raises(SourceResilienceError, match="Unregistered primary"):
        resolve_assumption_sources(replace(bundle, assumptions=assumptions))

    duplicate = replace(
        assumption,
        fallback_source_ids=("ncua_call_reports", "ncua_call_reports"),
    )
    assumptions = tuple(duplicate if item == assumption else item for item in bundle.assumptions)
    with pytest.raises(SourceResilienceError, match="Duplicate fallback"):
        resolve_assumption_sources(replace(bundle, assumptions=assumptions))
