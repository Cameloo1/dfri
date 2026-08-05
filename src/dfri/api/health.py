"""Calculate the read-only freshness precursor from lake watermarks and release SLAs."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

import polars as pl

from dfri.ingest.auto_abs import load_auto_abs_registry
from dfri.ingest.calendar import release_calendar_rows
from dfri.ingest.card_trust import load_card_trust_registry
from dfri.ingest.filing_facts import load_issuer_registry
from dfri.ingest.registry import load_board_series, load_context_series, load_nyfed_series
from dfri.lake.store import AppendOnlyParquetStore

HealthStatus = Literal["GREEN", "STALE", "BLOCKED", "OPTIONAL-DEGRADED"]


class HealthContractError(RuntimeError):
    """Freshness inputs or policies violate the health precursor contract."""


@dataclass(frozen=True)
class FreshnessPolicy:
    source_id: str
    source: str
    series_ids: frozenset[str]
    calendar_prefixes: tuple[str, ...]
    max_age: timedelta
    release_grace: timedelta
    optional: bool = False


@dataclass(frozen=True)
class LakeFreshnessPolicy:
    source_id: str
    table_name: str
    entity_column: str
    expected_entities: frozenset[str]
    watermark_column: str
    max_age: timedelta
    optional: bool = False


@dataclass(frozen=True)
class SourceFreshness:
    source_id: str
    status: HealthStatus
    reason: str
    expected_series_count: int
    observed_series_count: int
    watermark: datetime | None
    latest_due_at: datetime | None
    next_expected_at: datetime | None


@dataclass(frozen=True)
class HealthReport:
    status: HealthStatus
    as_of: datetime
    sources: tuple[SourceFreshness, ...]


def default_freshness_policies() -> tuple[FreshnessPolicy, ...]:
    """Build policies from the checked-in, live-verified series registry."""

    board = load_board_series()
    context = load_context_series()
    nyfed = load_nyfed_series()
    policies = (
        FreshnessPolicy(
            source_id="BOARD_G19",
            source="FEDERAL_RESERVE_BOARD",
            series_ids=frozenset(item.series_id for item in board if item.release == "g19"),
            calendar_prefixes=("G.19 ",),
            max_age=timedelta(days=45),
            release_grace=timedelta(hours=4),
        ),
        FreshnessPolicy(
            source_id="BOARD_H8",
            source="FEDERAL_RESERVE_BOARD",
            series_ids=frozenset(item.series_id for item in board if item.release == "h8"),
            calendar_prefixes=("H.8 ",),
            max_age=timedelta(days=10),
            release_grace=timedelta(hours=4),
        ),
        FreshnessPolicy(
            source_id="BEA_CONTEXT",
            source="BEA",
            series_ids=frozenset(item.series_id for item in context if item.source == "bea"),
            calendar_prefixes=("BEA Personal Income and Outlays ",),
            max_age=timedelta(days=45),
            release_grace=timedelta(hours=24),
        ),
        FreshnessPolicy(
            source_id="CENSUS_MARTS",
            source="CENSUS",
            series_ids=frozenset(item.series_id for item in context if item.source == "census"),
            calendar_prefixes=("Census MARTS release ",),
            max_age=timedelta(days=45),
            release_grace=timedelta(hours=24),
        ),
        FreshnessPolicy(
            source_id="NYFED_HHDC",
            source="NYFED",
            series_ids=frozenset(item.series_id for item in nyfed),
            calendar_prefixes=("NY Fed HHDC ",),
            max_age=timedelta(days=120),
            release_grace=timedelta(hours=24),
        ),
    )
    _validate_policies(policies)
    return policies


def default_lake_freshness_policies() -> tuple[LakeFreshnessPolicy, ...]:
    """Build non-calendar policies for the verified SEC ingest lanes."""

    issuers, fallbacks = load_issuer_registry()
    auto_trusts = load_auto_abs_registry()
    active_auto = tuple(item for item in auto_trusts if item.freshness_mode == "active")
    terminal_auto = tuple(item for item in auto_trusts if item.freshness_mode == "terminal_history")
    card_trusts = load_card_trust_registry()
    policies = (
        LakeFreshnessPolicy(
            source_id="SEC_XBRL",
            table_name="sec_xbrl_facts",
            entity_column="ticker",
            expected_entities=frozenset(item.ticker for item in issuers),
            watermark_column="ingested_at",
            max_age=timedelta(days=45),
        ),
        LakeFreshnessPolicy(
            source_id="SEC_HTML_EVIDENCE",
            table_name="sec_filing_evidence",
            entity_column="ticker",
            expected_entities=frozenset(item.ticker for item in fallbacks),
            watermark_column="ingested_at",
            max_age=timedelta(days=45),
        ),
        LakeFreshnessPolicy(
            source_id="SEC_AUTO_ABS_ACTIVE",
            table_name="auto_abs_aggregates",
            entity_column="trust_id",
            expected_entities=frozenset(item.trust_id for item in active_auto),
            watermark_column="reporting_period_end",
            max_age=timedelta(days=62),
        ),
        LakeFreshnessPolicy(
            source_id="SEC_AUTO_ABS_TERMINAL",
            table_name="auto_abs_aggregates",
            entity_column="trust_id",
            expected_entities=frozenset(item.trust_id for item in terminal_auto),
            watermark_column="ingested_at",
            max_age=timedelta(days=45),
        ),
        LakeFreshnessPolicy(
            source_id="SEC_CARD_10D",
            table_name="card_trust_aggregates",
            entity_column="trust_id",
            expected_entities=frozenset(item.trust_id for item in card_trusts),
            watermark_column="reporting_period_end",
            max_age=timedelta(days=62),
        ),
    )
    _validate_lake_policies(policies)
    return policies


def calculate_health(
    observations: pl.DataFrame,
    calendar: Sequence[Mapping[str, object]],
    *,
    as_of: datetime,
    policies: Sequence[FreshnessPolicy] | None = None,
) -> HealthReport:
    """Return deterministic source states at an explicit point in time."""

    as_of = _utc_datetime(as_of, field="as_of")
    required_columns = {"source", "series_id", "ingested_at"}
    if not required_columns.issubset(observations.columns):
        missing = sorted(required_columns - set(observations.columns))
        raise HealthContractError(f"Health observations are missing columns: {missing}")
    active_policies = tuple(policies or default_freshness_policies())
    _validate_policies(active_policies)

    visible = observations.filter(pl.col("ingested_at") <= as_of)
    results = tuple(
        _source_freshness(visible, calendar, as_of=as_of, policy=policy)
        for policy in active_policies
    )
    overall = _overall_status(results)
    return HealthReport(status=overall, as_of=as_of, sources=results)


def calculate_store_health(
    store: AppendOnlyParquetStore,
    *,
    as_of: datetime,
    calendar: Sequence[Mapping[str, object]] | None = None,
    policies: Sequence[FreshnessPolicy] | None = None,
    lake_policies: Sequence[LakeFreshnessPolicy] | None = None,
) -> HealthReport:
    macro = calculate_health(
        store.read_table("raw_observations"),
        tuple(calendar or release_calendar_rows()),
        as_of=as_of,
        policies=policies,
    )
    active_lake_policies = tuple(
        default_lake_freshness_policies() if lake_policies is None else lake_policies
    )
    _validate_lake_policies(active_lake_policies, allow_empty=True)
    lake_results = tuple(
        _lake_source_freshness(store, as_of=macro.as_of, policy=policy)
        for policy in active_lake_policies
    )
    sources = macro.sources + lake_results
    return HealthReport(
        status=_overall_status(sources),
        as_of=macro.as_of,
        sources=sources,
    )


def _lake_source_freshness(
    store: AppendOnlyParquetStore,
    *,
    as_of: datetime,
    policy: LakeFreshnessPolicy,
) -> SourceFreshness:
    frame = store.read_table(policy.table_name)
    required = {policy.entity_column, policy.watermark_column, "ingested_at"}
    if not required.issubset(frame.columns):
        missing = sorted(required - set(frame.columns))
        raise HealthContractError(
            f"Lake freshness table {policy.table_name} is missing columns: {missing}"
        )
    visible = frame.filter(pl.col("ingested_at") <= as_of).filter(
        pl.col(policy.entity_column).is_in(sorted(policy.expected_entities))
    )
    observed = frozenset(str(item) for item in visible[policy.entity_column].unique().to_list())
    if observed != policy.expected_entities:
        return _lake_result(
            policy,
            status=_lake_degraded_status(policy),
            reason="ENTITY_COVERAGE_INCOMPLETE" if observed else "NO_OBSERVATION",
            observed_entities=len(observed),
            watermark=None,
        )
    latest = visible.group_by(policy.entity_column).agg(
        pl.col(policy.watermark_column).max().alias("latest")
    )
    watermarks = tuple(_watermark_datetime(value) for value in latest["latest"].to_list())
    if len(watermarks) != len(policy.expected_entities):
        raise HealthContractError(f"Invalid lake freshness watermark for {policy.source_id}")
    watermark = min(watermarks)
    if as_of - watermark > policy.max_age:
        return _lake_result(
            policy,
            status=_lake_degraded_status(policy, stale=True),
            reason="MAX_AGE_EXCEEDED",
            observed_entities=len(observed),
            watermark=watermark,
        )
    return _lake_result(
        policy,
        status="GREEN",
        reason="CURRENT",
        observed_entities=len(observed),
        watermark=watermark,
    )


def _lake_result(
    policy: LakeFreshnessPolicy,
    *,
    status: HealthStatus,
    reason: str,
    observed_entities: int,
    watermark: datetime | None,
) -> SourceFreshness:
    return SourceFreshness(
        source_id=policy.source_id,
        status=status,
        reason=reason,
        expected_series_count=len(policy.expected_entities),
        observed_series_count=observed_entities,
        watermark=watermark,
        latest_due_at=None,
        next_expected_at=None,
    )


def _watermark_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return _utc_datetime(value, field="lake watermark")
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)
    raise HealthContractError("Lake freshness watermark must be a date or aware datetime")


def _overall_status(sources: Sequence[SourceFreshness]) -> HealthStatus:
    if not sources:
        raise HealthContractError("Health report requires at least one source")
    precedence: dict[HealthStatus, int] = {
        "GREEN": 0,
        "OPTIONAL-DEGRADED": 1,
        "BLOCKED": 2,
        "STALE": 3,
    }
    return max((item.status for item in sources), key=precedence.__getitem__)


def _source_freshness(
    observations: pl.DataFrame,
    calendar: Sequence[Mapping[str, object]],
    *,
    as_of: datetime,
    policy: FreshnessPolicy,
) -> SourceFreshness:
    latest_due, next_expected, has_blocked_calendar = _calendar_state(calendar, policy, as_of=as_of)
    matching = observations.filter(
        (pl.col("source") == policy.source) & pl.col("series_id").is_in(sorted(policy.series_ids))
    )
    observed_ids = frozenset(str(item) for item in matching["series_id"].unique().to_list())
    if observed_ids != policy.series_ids:
        return _result(
            policy,
            status=_degraded_status(policy),
            reason="SERIES_COVERAGE_INCOMPLETE" if observed_ids else "NO_OBSERVATION",
            observed_series_count=len(observed_ids),
            watermark=None,
            latest_due_at=latest_due,
            next_expected_at=next_expected,
        )

    per_series = matching.group_by("series_id").agg(pl.col("ingested_at").max().alias("latest"))
    watermarks = per_series["latest"].to_list()
    if len(watermarks) != len(policy.series_ids) or any(
        not isinstance(value, datetime) for value in watermarks
    ):
        raise HealthContractError(f"Invalid freshness watermark for {policy.source_id}")
    watermark = min(_utc_datetime(value, field="ingested_at") for value in watermarks)

    if latest_due is not None and watermark < latest_due:
        if as_of <= latest_due + policy.release_grace:
            return _result(
                policy,
                status="GREEN",
                reason="AWAITING_RELEASE_WITHIN_SLA",
                observed_series_count=len(observed_ids),
                watermark=watermark,
                latest_due_at=latest_due,
                next_expected_at=next_expected,
            )
        return _result(
            policy,
            status=_degraded_status(policy, stale=True),
            reason="MISSED_RELEASE_SLA",
            observed_series_count=len(observed_ids),
            watermark=watermark,
            latest_due_at=latest_due,
            next_expected_at=next_expected,
        )

    if as_of - watermark > policy.max_age:
        reason = "CALENDAR_SLA_BLOCKED" if has_blocked_calendar else "MAX_AGE_EXCEEDED"
        status = _degraded_status(policy, blocked=has_blocked_calendar)
        return _result(
            policy,
            status=status,
            reason=reason,
            observed_series_count=len(observed_ids),
            watermark=watermark,
            latest_due_at=latest_due,
            next_expected_at=next_expected,
        )

    return _result(
        policy,
        status="GREEN",
        reason="CURRENT",
        observed_series_count=len(observed_ids),
        watermark=watermark,
        latest_due_at=latest_due,
        next_expected_at=next_expected,
    )


def _calendar_state(
    calendar: Sequence[Mapping[str, object]],
    policy: FreshnessPolicy,
    *,
    as_of: datetime,
) -> tuple[datetime | None, datetime | None, bool]:
    due: list[datetime] = []
    future: list[datetime] = []
    blocked = False
    for row in calendar:
        release_name = row.get("release_name")
        if not isinstance(release_name, str) or not release_name.startswith(
            policy.calendar_prefixes
        ):
            continue
        status = row.get("status")
        if not isinstance(status, str):
            raise HealthContractError(f"Calendar status is invalid for {policy.source_id}")
        blocked = blocked or status.startswith("BLOCKED")
        expected = _optional_utc_datetime(row.get("expected_at"), field="expected_at")
        actual = _optional_utc_datetime(row.get("actual_at"), field="actual_at")
        event_at = actual or expected
        if event_at is None:
            continue
        (due if event_at <= as_of else future).append(event_at)
    return (max(due) if due else None, min(future) if future else None, blocked)


def _result(
    policy: FreshnessPolicy,
    *,
    status: HealthStatus,
    reason: str,
    observed_series_count: int,
    watermark: datetime | None,
    latest_due_at: datetime | None,
    next_expected_at: datetime | None,
) -> SourceFreshness:
    return SourceFreshness(
        source_id=policy.source_id,
        status=status,
        reason=reason,
        expected_series_count=len(policy.series_ids),
        observed_series_count=observed_series_count,
        watermark=watermark,
        latest_due_at=latest_due_at,
        next_expected_at=next_expected_at,
    )


def _degraded_status(
    policy: FreshnessPolicy, *, stale: bool = False, blocked: bool = True
) -> HealthStatus:
    if policy.optional:
        return "OPTIONAL-DEGRADED"
    if stale:
        return "STALE"
    return "BLOCKED" if blocked else "STALE"


def _validate_policies(policies: Sequence[FreshnessPolicy]) -> None:
    if not policies:
        raise HealthContractError("At least one freshness policy is required")
    ids = [policy.source_id for policy in policies]
    if len(ids) != len(set(ids)):
        raise HealthContractError("Freshness policy IDs must be unique")
    for policy in policies:
        if not policy.source_id or not policy.source or not policy.series_ids:
            raise HealthContractError("Freshness policies require IDs, sources, and series")
        if policy.max_age <= timedelta(0) or policy.release_grace < timedelta(0):
            raise HealthContractError(f"Freshness policy durations are invalid: {policy.source_id}")


def _validate_lake_policies(
    policies: Sequence[LakeFreshnessPolicy], *, allow_empty: bool = False
) -> None:
    if not policies and not allow_empty:
        raise HealthContractError("At least one lake freshness policy is required")
    ids = [policy.source_id for policy in policies]
    if len(ids) != len(set(ids)):
        raise HealthContractError("Lake freshness policy IDs must be unique")
    for policy in policies:
        if (
            not policy.source_id
            or not policy.table_name
            or not policy.entity_column
            or not policy.expected_entities
            or not policy.watermark_column
            or policy.max_age <= timedelta(0)
        ):
            raise HealthContractError(f"Lake freshness policy is invalid: {policy.source_id}")


def _lake_degraded_status(policy: LakeFreshnessPolicy, *, stale: bool = False) -> HealthStatus:
    if policy.optional:
        return "OPTIONAL-DEGRADED"
    return "STALE" if stale else "BLOCKED"


def _optional_utc_datetime(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise HealthContractError(f"{field} must be a timezone-aware datetime or null")
    return _utc_datetime(value, field=field)


def _utc_datetime(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HealthContractError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_as_of(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _utc_datetime(parsed, field="as_of")
    except (ValueError, HealthContractError) as exc:
        raise argparse.ArgumentTypeError("expected a timezone-aware ISO-8601 timestamp") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--as-of", type=_parse_as_of)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_of = args.as_of or datetime.now(UTC)
    report = calculate_store_health(AppendOnlyParquetStore(args.lake_root), as_of=as_of)
    print(json.dumps(asdict(report), sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
