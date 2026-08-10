"""Content-addressed scheduled-job receipts and a deterministic public health report."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from html import escape
from pathlib import Path
from typing import Final, Literal, cast
from zoneinfo import ZoneInfo

from dfri.ingest.calendar import release_calendar_rows
from dfri.ingest.registry import load_treasury_mts

SCHEMA_VERSION: Final = "v1"
RECEIPT_SCHEMA_VERSION: Final = 1
Status = Literal["CURRENT", "STALE", "UNKNOWN"]
Cadence = Literal["weekday", "weekly-monday", "hourly"]
ReleaseMode = Literal["none", "g19", "h8", "mts-before", "mts-after"]


class JobStatusError(RuntimeError):
    """Job-health state is incomplete, corrupt, or internally inconsistent."""


@dataclass(frozen=True)
class JobDefinition:
    job_id: str
    label: str
    cadence: Cadence
    hour: int
    minute: int
    grace_minutes: int
    release_mode: ReleaseMode
    release_sla_minutes: int | None


@dataclass(frozen=True)
class JobSuccessReceipt:
    job_id: str
    succeeded_at: datetime
    workflow_run_url: str


JOBS: Final = (
    JobDefinition("mts-predict", "Treasury prediction", "weekday", 16, 17, 120, "mts-before", 0),
    JobDefinition("mts-grade", "Treasury grading", "weekday", 19, 17, 120, "mts-after", 240),
    JobDefinition("g19-grade", "G.19 grading", "weekday", 21, 17, 120, "g19", 240),
    JobDefinition("h8-predict", "H.8 prediction", "weekday", 23, 17, 120, "h8", 300),
    JobDefinition(
        "quarterly-refresh",
        "Quarterly attribution refresh",
        "weekly-monday",
        14,
        43,
        360,
        "none",
        None,
    ),
)
JOB_IDS: Final = frozenset(item.job_id for item in JOBS)


def record_success(
    directory: Path,
    *,
    job_id: str,
    succeeded_at: datetime,
    workflow_run_url: str,
) -> Path:
    """Append one immutable, content-addressed job-success receipt."""

    if job_id not in JOB_IDS:
        raise JobStatusError(f"Unknown scheduled job: {job_id}")
    _aware(succeeded_at, "succeeded_at")
    if not workflow_run_url.startswith("https://github.com/"):
        raise JobStatusError("workflow_run_url must be an HTTPS GitHub run URL")
    payload = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "job_id": job_id,
        "succeeded_at": succeeded_at.astimezone(UTC).isoformat(),
        "workflow_run_url": workflow_run_url,
    }
    encoded = _canonical_json(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    path = directory / f"{job_id}-{digest}.json"
    if path.exists():
        if path.read_bytes() != encoded:
            raise JobStatusError(f"Job receipt hash collision: {path}")
        return path
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{path.name}.tmp"
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return path


def load_success_receipts(directory: Path) -> tuple[JobSuccessReceipt, ...]:
    """Read and verify all success receipts, rejecting mutable or malformed state."""

    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise JobStatusError(f"Job receipt path is not a directory: {directory}")
    receipts: list[JobSuccessReceipt] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise JobStatusError(f"Cannot read job receipt: {path}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise JobStatusError(f"Invalid job receipt schema: {path}")
        if set(payload) != {"schema_version", "job_id", "succeeded_at", "workflow_run_url"}:
            raise JobStatusError(f"Invalid job receipt fields: {path}")
        job_id = payload["job_id"]
        workflow_url = payload["workflow_run_url"]
        if not isinstance(job_id, str) or job_id not in JOB_IDS:
            raise JobStatusError(f"Unknown job in receipt: {path}")
        if not isinstance(workflow_url, str) or not workflow_url.startswith("https://github.com/"):
            raise JobStatusError(f"Invalid workflow URL in receipt: {path}")
        succeeded_at = _timestamp(payload["succeeded_at"], path)
        expected_name = f"{job_id}-{hashlib.sha256(_canonical_json(payload)).hexdigest()}.json"
        if path.name != expected_name or raw != _canonical_json(payload):
            raise JobStatusError(f"Job receipt is not canonical or content-addressed: {path}")
        receipts.append(JobSuccessReceipt(job_id, succeeded_at, workflow_url))
    return tuple(receipts)


def build_status_report(
    *,
    as_of: datetime,
    receipt_directory: Path,
    publication_mode: str,
) -> dict[str, object]:
    """Build the public status snapshot from immutable receipts and pinned calendars."""

    _aware(as_of, "as_of")
    if publication_mode not in {"preview", "live"}:
        raise JobStatusError("publication_mode must be preview or live")
    as_of = as_of.astimezone(UTC)
    by_job: dict[str, list[JobSuccessReceipt]] = {item.job_id: [] for item in JOBS}
    for receipt in load_success_receipts(receipt_directory):
        if receipt.succeeded_at > as_of:
            raise JobStatusError("Job receipt cannot follow the status snapshot")
        by_job[receipt.job_id].append(receipt)
    rows = [
        _job_row(definition, tuple(by_job[definition.job_id]), as_of, publication_mode)
        for definition in JOBS
    ]
    overall: Status
    if any(item["status"] == "STALE" for item in rows):
        overall = "STALE"
    elif any(item["status"] == "UNKNOWN" for item in rows):
        overall = "UNKNOWN"
    else:
        overall = "CURRENT"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": as_of.isoformat(),
        "publication_mode": publication_mode,
        "overall_status": overall,
        "jobs": rows,
    }


def render_status_banner(report: dict[str, object]) -> bytes:
    """Render the small no-JavaScript status document embedded by every page."""

    status = report.get("overall_status")
    generated_at = report.get("generated_at")
    jobs = report.get("jobs")
    if status not in {"CURRENT", "STALE", "UNKNOWN"} or not isinstance(generated_at, str):
        raise JobStatusError("Status banner input does not match the public v1 contract")
    if not isinstance(jobs, list):
        raise JobStatusError("Status banner jobs must be a list")
    missed = [
        str(item["label"])
        for item in jobs
        if isinstance(item, dict)
        and (item.get("missed_expected_run") or item.get("missed_expected_release"))
    ]
    if status == "CURRENT":
        lead = "Automation current."
        detail = "All registered lanes are within their recorded run and release SLAs."
    elif status == "STALE":
        lead = "Automation stale."
        detail = (
            "SLA exceeded: " + ", ".join(missed)
            if missed
            else "One or more registered lanes exceeded its SLA."
        )
    else:
        lead = "Automation status incomplete."
        detail = "At least one registered lane has no verified success receipt."
    content = "".join(
        (
            '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n',
            "<style>html{background:#f2eee4;color:#171715;",
            "font:14px ui-monospace,monospace}body{margin:0;",
            "border-block:1px solid #8b8579}p{box-sizing:border-box;",
            "max-width:1096px;margin:0 auto;padding:9px 0}strong{font-weight:800}",
            "a{color:inherit;text-underline-offset:2px}",
            "a:focus{outline:3px double #171715;outline-offset:3px}",
            "@media(max-width:700px){p{padding:8px 16px}}</style></head>\n",
            f"<body><p><strong>{escape(lead)}</strong> {escape(detail)} ",
            '<a target="_top" href="../v1/status.json">Status record</a> · checked ',
            f"{escape(generated_at)}</p></body></html>\n",
        )
    )
    return content.encode()


def _job_row(
    definition: JobDefinition,
    receipts: tuple[JobSuccessReceipt, ...],
    as_of: datetime,
    publication_mode: str,
) -> dict[str, object]:
    ordered = sorted(receipts, key=lambda item: (item.succeeded_at, item.workflow_run_url))
    last = ordered[-1] if ordered else None
    latest_due = _latest_due(definition, as_of)
    next_run = _next_run(definition, as_of)
    missed_run = False
    if publication_mode == "live" and latest_due is not None:
        deadline = latest_due + timedelta(minutes=definition.grace_minutes)
        missed_run = as_of > deadline and (last is None or last.succeeded_at < latest_due)
    release = _release_state(definition, tuple(ordered), as_of, publication_mode)
    if missed_run or release["missed_expected_release"] is True:
        status = "STALE"
    elif publication_mode != "live" or last is None:
        status = "UNKNOWN"
    else:
        status = "CURRENT"
    return {
        "job_id": definition.job_id,
        "label": definition.label,
        "status": status,
        "last_successful_run": last.succeeded_at.isoformat() if last else None,
        "last_successful_run_url": last.workflow_run_url if last else None,
        "expected_next_run": next_run.isoformat(),
        "run_sla_minutes": definition.grace_minutes,
        "missed_expected_run": missed_run,
        **release,
    }


def _release_state(
    definition: JobDefinition,
    receipts: tuple[JobSuccessReceipt, ...],
    as_of: datetime,
    publication_mode: str,
) -> dict[str, object]:
    if definition.release_mode == "none":
        return {
            "expected_release_at": None,
            "release_sla_minutes": None,
            "missed_expected_release": None,
            "release_check_status": "NOT_APPLICABLE",
        }
    release_at = _latest_release(definition.release_mode, as_of)
    if release_at is None:
        return {
            "expected_release_at": None,
            "release_sla_minutes": definition.release_sla_minutes,
            "missed_expected_release": None,
            "release_check_status": "DATE_UNANNOUNCED",
        }
    if publication_mode != "live":
        missed: bool | None = None
        release_status = "PREVIEW_NOT_EVALUATED"
    else:
        deadline = release_at + timedelta(minutes=definition.release_sla_minutes or 0)
        if as_of <= deadline:
            missed = False
            release_status = "WITHIN_SLA"
        else:
            if definition.release_mode == "mts-before":
                window_start = release_at - timedelta(days=4)
                observed = any(window_start <= item.succeeded_at <= release_at for item in receipts)
            else:
                observed = any(release_at <= item.succeeded_at <= deadline for item in receipts)
            missed = not observed
            release_status = "MISSED" if missed else "OBSERVED_ON_TIME"
    return {
        "expected_release_at": release_at.isoformat(),
        "release_sla_minutes": definition.release_sla_minutes,
        "missed_expected_release": missed,
        "release_check_status": release_status,
    }


def _latest_release(mode: ReleaseMode, as_of: datetime) -> datetime | None:
    releases: list[datetime] = []
    if mode == "h8":
        for row in release_calendar_rows():
            expected = row["expected_at"]
            if str(row["release_name"]).startswith("H.8") and isinstance(expected, datetime):
                releases.append(expected.astimezone(UTC))
    elif mode in {"mts-before", "mts-after"}:
        definition = load_treasury_mts()
        hour, minute, second = (int(item) for item in definition.release_time.split(":"))
        zone = ZoneInfo(definition.time_zone)
        releases.extend(
            datetime.combine(release_date, time(hour, minute, second), tzinfo=zone).astimezone(UTC)
            for release_date in definition.release_schedule.values()
        )
    elif mode == "g19":
        # The Board does not publish the next G.19 date far enough ahead for the committed
        # calendar to claim one. The workflow still has a cron SLA, but release status is unknown.
        return None
    eligible = [item for item in releases if item <= as_of]
    return max(eligible) if eligible else None


def _latest_due(definition: JobDefinition, as_of: datetime) -> datetime | None:
    if definition.cadence == "hourly":
        candidate = as_of.replace(minute=definition.minute, second=0, microsecond=0)
        return candidate if candidate <= as_of else candidate - timedelta(hours=1)
    cursor = as_of.date()
    for _ in range(8):
        allowed = cursor.weekday() < 5 if definition.cadence == "weekday" else cursor.weekday() == 0
        candidate = datetime.combine(cursor, time(definition.hour, definition.minute), tzinfo=UTC)
        if allowed and candidate <= as_of:
            return candidate
        cursor -= timedelta(days=1)
    return None


def _next_run(definition: JobDefinition, as_of: datetime) -> datetime:
    if definition.cadence == "hourly":
        candidate = as_of.replace(minute=definition.minute, second=0, microsecond=0)
        return candidate if candidate > as_of else candidate + timedelta(hours=1)
    cursor = as_of.date()
    for _ in range(9):
        allowed = cursor.weekday() < 5 if definition.cadence == "weekday" else cursor.weekday() == 0
        candidate = datetime.combine(cursor, time(definition.hour, definition.minute), tzinfo=UTC)
        if allowed and candidate > as_of:
            return candidate
        cursor += timedelta(days=1)
    raise JobStatusError(f"Unable to calculate next run for {definition.job_id}")


def _canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise JobStatusError(f"{label} must be timezone-aware")


def _timestamp(value: object, path: Path) -> datetime:
    if not isinstance(value, str):
        raise JobStatusError(f"Invalid timestamp in receipt: {path}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JobStatusError(f"Invalid timestamp in receipt: {path}") from exc
    _aware(parsed, "receipt timestamp")
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--directory", type=Path, default=Path(".local/evidence/job_status"))
    record.add_argument("--job-id", required=True, choices=sorted(JOB_IDS))
    record.add_argument("--succeeded-at", required=True)
    record.add_argument("--workflow-run-url", required=True)
    report = subparsers.add_parser("report")
    report.add_argument("--directory", type=Path, default=Path(".local/evidence/job_status"))
    report.add_argument("--as-of", required=True)
    report.add_argument("--publication-mode", choices=("preview", "live"), default="preview")
    report.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "record":
        succeeded_at = datetime.fromisoformat(cast(str, args.succeeded_at).replace("Z", "+00:00"))
        output = record_success(
            args.directory,
            job_id=args.job_id,
            succeeded_at=succeeded_at,
            workflow_run_url=args.workflow_run_url,
        )
        print(json.dumps({"path": str(output), "status": "PASS"}, sort_keys=True))
        return 0
    as_of = datetime.fromisoformat(cast(str, args.as_of).replace("Z", "+00:00"))
    payload = build_status_report(
        as_of=as_of,
        receipt_directory=args.directory,
        publication_mode=args.publication_mode,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(_canonical_json(payload))
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
