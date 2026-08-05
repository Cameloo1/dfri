"""Deterministic, evidence-registered company attribution."""

from dfri.attribution.engine import AttributionResult, run_attribution
from dfri.attribution.registry import AttributionBundle, load_attribution_bundle

__all__ = [
    "AttributionBundle",
    "AttributionResult",
    "load_attribution_bundle",
    "run_attribution",
]
