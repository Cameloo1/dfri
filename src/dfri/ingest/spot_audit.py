"""Deterministically compare 20 stored observations with live authoritative parsers."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import polars as pl

from dfri.ingest.bea import BeaClient
from dfri.ingest.board import RELEASE_URLS, FederalReserveBoardClient
from dfri.ingest.board_snapshot import BoardSnapshotIngestor
from dfri.ingest.census import CensusClient
from dfri.ingest.context_history import ContextHistoryIngestor
from dfri.ingest.http import HttpTransport
from dfri.ingest.nyfed import NyFedClient, NyFedHistoryIngestor
from dfri.lake.store import AppendOnlyParquetStore

AuditStatus = Literal["PASS", "FAIL", "BLOCKED"]
DEFAULT_START = date(2015, 1, 1)
DEFAULT_SEED = "dfri-m1-spot-audit-v1"
SAMPLE_SIZE = 20
SUPPORTED_GROUPS = ("BOARD_G19", "BOARD_H8", "BEA", "CENSUS", "NYFED")
AUDIT_COLUMNS = {
    "source",
    "series_id",
    "obs_period",
    "value",
    "unit",
    "source_url",
    "checksum",
}


class SpotAuditError(RuntimeError):
    """The stored lake, live snapshots, or receipt violated the spot-audit contract."""


@dataclass(frozen=True)
class AuditSample:
    source_group: str
    series_id: str
    obs_period: date
    stored_value: float
    live_value: float
    unit: str
    source_url: str
    checksum: str
    status: Literal["PASS", "FAIL"]


@dataclass(frozen=True)
class SpotAuditReceipt:
    status: AuditStatus
    audited_at: datetime
    seed: str
    requested_rows: int
    audited_rows: int
    passed_rows: int
    failed_rows: int
    blocked_groups: tuple[str, ...]
    rows_by_group: dict[str, int]
    evidence_hash: str
    samples: tuple[AuditSample, ...]


def fetch_live_observations(
    *,
    bea_api_key: str,
    census_api_key: str,
    start: date = DEFAULT_START,
    work_root: Path = Path(".local/tmp"),
) -> pl.DataFrame:
    """Run each completed production parser in a disposable lake and return its rows."""

    if not bea_api_key or not census_api_key:
        raise SpotAuditError("BEA_API_KEY and CENSUS_API_KEY are required")
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dfri-spot-audit-", dir=work_root) as temporary:
        store = AppendOnlyParquetStore(Path(temporary) / "lake")
        with HttpTransport(min_interval_seconds=0.5) as transport:
            board_client = FederalReserveBoardClient(transport)
            board_ingestor = BoardSnapshotIngestor(store, board_client)
            board_ingestor.fetch_and_ingest("g19", start=start)
            board_ingestor.fetch_and_ingest("h8", start=start)

            context_ingestor = ContextHistoryIngestor(store)
            context_ingestor.fetch_bea(BeaClient(transport, bea_api_key), start=start)
            context_ingestor.fetch_census(CensusClient(transport, census_api_key), start=start)

            NyFedHistoryIngestor(store).fetch(NyFedClient(transport), start=start)
        return store.read_table("raw_observations")


def audit_observations(
    stored: pl.DataFrame,
    live: pl.DataFrame,
    *,
    audited_at: datetime,
    seed: str = DEFAULT_SEED,
    sample_size: int = SAMPLE_SIZE,
    expected_groups: Sequence[str] = SUPPORTED_GROUPS,
) -> SpotAuditReceipt:
    """Select a stable, group-covering random sample and compare exact source identities."""

    audited_at = _utc_datetime(audited_at)
    if not seed:
        raise SpotAuditError("Spot-audit seed must not be empty")
    if sample_size < 1:
        raise SpotAuditError("Spot-audit sample size must be positive")
    groups = tuple(expected_groups)
    if not groups or len(groups) != len(set(groups)):
        raise SpotAuditError("Spot-audit source groups must be nonempty and unique")
    stored_rows = _audit_frame(stored, groups)
    live_rows = _audit_frame(live, groups)

    blocked_groups: list[str] = []
    for group in groups:
        live_group = live_rows.filter(pl.col("source_group") == group)
        stored_group = stored_rows.filter(pl.col("source_group") == group)
        live_identities = _identities(live_group)
        stored_identities = _identities(stored_group)
        if not live_identities or not live_identities.issubset(stored_identities):
            blocked_groups.append(group)

    key_columns = [
        "source_group",
        "source",
        "series_id",
        "obs_period",
        "unit",
        "source_url",
        "checksum",
    ]
    _reject_duplicate_keys(stored_rows, key_columns, "stored")
    _reject_duplicate_keys(live_rows, key_columns, "live")
    candidates = stored_rows.join(
        live_rows.select([*key_columns, pl.col("value").alias("live_value")]),
        on=key_columns,
        how="inner",
        validate="1:1",
    ).sort(["source_group", "series_id", "obs_period", "checksum"])

    selected = _select_rows(candidates, seed=seed, sample_size=sample_size, groups=groups)
    samples = tuple(_sample_from_row(row) for row in selected.iter_rows(named=True))
    failed = sum(sample.status == "FAIL" for sample in samples)
    blocked = bool(blocked_groups) or len(samples) < sample_size
    status: AuditStatus = "BLOCKED" if blocked else ("FAIL" if failed else "PASS")
    rows_by_group = dict(sorted(Counter(sample.source_group for sample in samples).items()))
    evidence_hash = _samples_hash(samples)
    return SpotAuditReceipt(
        status=status,
        audited_at=audited_at,
        seed=seed,
        requested_rows=sample_size,
        audited_rows=len(samples),
        passed_rows=len(samples) - failed,
        failed_rows=failed,
        blocked_groups=tuple(sorted(blocked_groups)),
        rows_by_group=rows_by_group,
        evidence_hash=evidence_hash,
        samples=samples,
    )


def write_receipt(receipt: SpotAuditReceipt, output: Path) -> None:
    """Atomically persist a secret-free local evidence receipt."""

    output.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(asdict(receipt))
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(payload)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def _audit_frame(frame: pl.DataFrame, groups: Sequence[str]) -> pl.DataFrame:
    missing = AUDIT_COLUMNS - set(frame.columns)
    if missing:
        raise SpotAuditError(f"Spot-audit frame is missing columns: {sorted(missing)}")
    selected = frame.select(sorted(AUDIT_COLUMNS)).with_columns(
        pl.struct(["source", "source_url"])
        .map_elements(
            lambda row: _source_group(str(row["source"]), str(row["source_url"])),
            return_dtype=pl.String,
        )
        .alias("source_group")
    )
    if selected.select(
        pl.col("source_url").str.contains(r"(?i)(?:[?&](?:UserID|key)=)").any()
    ).item():
        raise SpotAuditError("Spot-audit frame contains a credential-bearing source URL")
    return selected.filter(pl.col("source_group").is_in(list(groups)))


def _source_group(source: str, source_url: str) -> str:
    if source_url == RELEASE_URLS["g19"]:
        return "BOARD_G19"
    if source_url == RELEASE_URLS["h8"]:
        return "BOARD_H8"
    if source in {"BEA", "CENSUS", "NYFED"}:
        return source
    return "UNSUPPORTED"


def _identities(frame: pl.DataFrame) -> frozenset[tuple[str, str]]:
    return frozenset(
        (str(source_url), str(checksum))
        for source_url, checksum in frame.select(["source_url", "checksum"]).unique().iter_rows()
    )


def _reject_duplicate_keys(frame: pl.DataFrame, keys: Sequence[str], label: str) -> None:
    if frame.select(keys).is_duplicated().any():
        raise SpotAuditError(f"Spot-audit {label} frame contains duplicate source rows")


def _select_rows(
    candidates: pl.DataFrame,
    *,
    seed: str,
    sample_size: int,
    groups: Sequence[str],
) -> pl.DataFrame:
    if candidates.is_empty():
        return candidates
    seed_value = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest())
    generator = random.Random(seed_value)
    selected_indices: set[int] = set()
    group_values = candidates["source_group"].to_list()
    for group in groups:
        indices = [index for index, value in enumerate(group_values) if value == group]
        if indices:
            selected_indices.add(generator.choice(indices))
    remaining = [index for index in range(candidates.height) if index not in selected_indices]
    needed = min(sample_size, candidates.height) - len(selected_indices)
    if needed > 0:
        selected_indices.update(generator.sample(remaining, needed))
    return candidates[sorted(selected_indices)].sort(
        ["source_group", "series_id", "obs_period", "checksum"]
    )


def _sample_from_row(row: Mapping[str, object]) -> AuditSample:
    period = row["obs_period"]
    if not isinstance(period, date):
        raise SpotAuditError("Spot-audit observation period is invalid")
    stored_value = _float_value(row["value"], field="stored value")
    live_value = _float_value(row["live_value"], field="live value")
    matches = math.isclose(stored_value, live_value, rel_tol=0.0, abs_tol=1e-12)
    return AuditSample(
        source_group=str(row["source_group"]),
        series_id=str(row["series_id"]),
        obs_period=period,
        stored_value=stored_value,
        live_value=live_value,
        unit=str(row["unit"]),
        source_url=str(row["source_url"]),
        checksum=str(row["checksum"]),
        status="PASS" if matches else "FAIL",
    )


def _samples_hash(samples: Sequence[AuditSample]) -> str:
    canonical = _json_bytes([asdict(sample) for sample in samples])
    return hashlib.sha256(canonical).hexdigest()


def _float_value(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SpotAuditError(f"Spot-audit {field} is invalid")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise SpotAuditError(f"Spot-audit {field} is invalid")
    return numeric


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default) + "\n"
    ).encode("utf-8")


def _json_default(value: object) -> str:
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SpotAuditError("Spot-audit timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--work-root", type=Path, default=Path(".local/tmp"))
    parser.add_argument("--output", type=Path, default=Path(".local/evidence/spot-audit.json"))
    parser.add_argument("--start", type=_parse_date, default=DEFAULT_START)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bea_api_key = os.environ.get("BEA_API_KEY", "")
    census_api_key = os.environ.get("CENSUS_API_KEY", "")
    live = fetch_live_observations(
        bea_api_key=bea_api_key,
        census_api_key=census_api_key,
        start=args.start,
        work_root=args.work_root,
    )
    stored = AppendOnlyParquetStore(args.lake_root).read_table("raw_observations")
    receipt = audit_observations(stored, live, audited_at=datetime.now(UTC), seed=args.seed)
    write_receipt(receipt, args.output)
    summary = asdict(receipt)
    summary.pop("samples")
    print(_json_bytes(summary).decode("utf-8"), end="")
    return 0 if receipt.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
