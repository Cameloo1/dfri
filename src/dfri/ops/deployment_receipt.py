"""Write a durable M2 deployment receipt and enforce the four-hour release SLA."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

SLA = timedelta(hours=4)


class DeploymentReceiptError(RuntimeError):
    """Deployment evidence is incomplete or temporally inconsistent."""


@dataclass(frozen=True)
class DeploymentReceipt:
    output: Path
    latency_seconds: int
    sla_status: str


def write_deployment_receipt(
    output: Path,
    *,
    mode: str,
    source_release_at: datetime,
    published_at: datetime,
    deployed_at: datetime,
    page_url: str,
    workflow_url: str,
    commit_sha: str,
    prediction_appended: int,
    grade_appended: int,
) -> DeploymentReceipt:
    """Validate, atomically persist, and return one release-to-publication receipt."""

    if mode not in {"predict", "grade", "all"}:
        raise DeploymentReceiptError("Deployment mode is invalid")
    for value, label in (
        (source_release_at, "source release"),
        (published_at, "publication"),
        (deployed_at, "deployment"),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise DeploymentReceiptError(f"{label} timestamp must be timezone-aware")
    if not source_release_at <= published_at <= deployed_at:
        raise DeploymentReceiptError("Deployment timestamps are out of order")
    if not _https_url(page_url) or not _https_url(workflow_url):
        raise DeploymentReceiptError("Deployment evidence URLs must use HTTPS")
    if len(commit_sha) != 40 or any(
        character not in "0123456789abcdef" for character in commit_sha
    ):
        raise DeploymentReceiptError("Deployment commit must be a lowercase SHA-1")
    if prediction_appended < 0 or grade_appended < 0:
        raise DeploymentReceiptError("Deployment append counts cannot be negative")
    latency = round((deployed_at - source_release_at).total_seconds())
    status = "PASS" if latency <= round(SLA.total_seconds()) else "FAIL"
    payload = {
        "schema_version": 1,
        "mode": mode,
        "source_release_at": source_release_at.astimezone(UTC).isoformat(),
        "published_at": published_at.astimezone(UTC).isoformat(),
        "deployed_at": deployed_at.astimezone(UTC).isoformat(),
        "latency_seconds": latency,
        "sla_seconds": round(SLA.total_seconds()),
        "sla_status": status,
        "page_url": page_url,
        "workflow_url": workflow_url,
        "commit_sha": commit_sha,
        "prediction_appended": prediction_appended,
        "grade_appended": grade_appended,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return DeploymentReceipt(output=output, latency_seconds=latency, sla_status=status)


def _https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("Timestamp must include a timezone")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("predict", "grade", "all"), required=True)
    parser.add_argument("--source-release-at", type=_timestamp, required=True)
    parser.add_argument("--published-at", type=_timestamp, required=True)
    parser.add_argument("--deployed-at", type=_timestamp)
    parser.add_argument("--page-url", required=True)
    parser.add_argument("--workflow-url", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--prediction-appended", type=int, required=True)
    parser.add_argument("--grade-appended", type=int, required=True)
    args = parser.parse_args()
    receipt = write_deployment_receipt(
        args.output,
        mode=args.mode,
        source_release_at=args.source_release_at,
        published_at=args.published_at,
        deployed_at=args.deployed_at or datetime.now(UTC),
        page_url=args.page_url,
        workflow_url=args.workflow_url,
        commit_sha=args.commit_sha,
        prediction_appended=args.prediction_appended,
        grade_appended=args.grade_appended,
    )
    print(
        json.dumps(
            {
                "output": str(receipt.output),
                "latency_seconds": receipt.latency_seconds,
                "sla_status": receipt.sla_status,
            },
            sort_keys=True,
        )
    )
    if receipt.sla_status != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
