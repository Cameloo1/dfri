"""Read-only public uptime and freshness monitor with an owner-readable receipt."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import urljoin

import httpx

SITE_PATHS: Final = (
    "",
    "scoreboard/",
    "methodology/",
    "changelog/",
    "v1/feeds/schema.json",
    "v1/feeds/nowcast_predictions.json",
    "v1/feeds/scoreboard.json",
    "v1/feeds/dfri_companies.json",
    "v1/feeds/assumptions.json",
    "v2/feeds/schema.json",
    "v2/feeds/dfri_companies.json",
    "v1/status.json",
    "v1/events.json",
    "events.xml",
)
API_PATHS: Final = (
    "v1/nowcast/latest",
    "v1/scoreboard",
    "v1/companies",
    "v1/assumptions",
    "v1/methodology/versions",
    "v1/releases/calendar",
    "v1/health",
)
API_DEFERRED_REASON: Final = "owner_deferred_until_programmatic_demand_or_unwieldy_m5_feeds"


class UptimeCheckError(RuntimeError):
    """One or more required public uptime checks failed."""


def run_checks(
    site_base: str,
    *,
    api_base: str | None = None,
    require_api: bool = False,
    utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Check stable public URLs and fail closed on stale nowcast publication metadata."""

    current = utc_clock().astimezone(UTC)
    owned_client = client is None
    session = client or httpx.Client(timeout=20.0, follow_redirects=True)
    try:
        site_results = [_get(session, site_base, path) for path in SITE_PATHS]
        scoreboard = session.get(urljoin(_base(site_base), "v1/feeds/scoreboard.json"))
        freshness = _freshness(scoreboard, current)
        status_response = session.get(urljoin(_base(site_base), "v1/status.json"))
        automation = _automation_status(status_response)
        api_results = (
            [_get(session, api_base, path) for path in API_PATHS] if api_base is not None else []
        )
    finally:
        if owned_client:
            session.close()
    site_green = all(item["status"] == "GREEN" for item in site_results)
    api_green = bool(api_results) and all(item["status"] == "GREEN" for item in api_results)
    api_required = require_api or api_base is not None
    if api_base is None:
        api_status = "DEFERRED"
    else:
        api_status = "GREEN" if api_green else "RED"
    required_green = (
        site_green
        and freshness["status"] == "GREEN"
        and automation["status"] == "GREEN"
        and (api_green if api_required else True)
    )
    status = "GREEN" if required_green else "RED"
    return {
        "status": status,
        "checked_at": current.isoformat(),
        "site": {"status": "GREEN" if site_green else "RED", "checks": site_results},
        "nowcast_freshness": freshness,
        "automation": automation,
        "api": {
            "status": api_status,
            "required": api_required,
            "reason": API_DEFERRED_REASON if api_base is None else None,
            "checks": api_results,
        },
    }


def write_receipt(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _get(client: httpx.Client, base: str, path: str) -> dict[str, object]:
    url = urljoin(_base(base), path)
    started = time.perf_counter()
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        return {"url": url, "status": "RED", "error": type(exc).__name__}
    return {
        "url": url,
        "status": "GREEN" if response.status_code == 200 else "RED",
        "http_status": response.status_code,
        "elapsed_ms": round((time.perf_counter() - started) * 1_000, 3),
    }


def _freshness(response: httpx.Response, current: datetime) -> dict[str, object]:
    try:
        response.raise_for_status()
        payload = response.json()
        vintage = datetime.fromisoformat(
            str(payload["meta"]["data_vintage"]).replace("Z", "+00:00")
        )
        if vintage.tzinfo is None or vintage.utcoffset() is None:
            raise ValueError("timezone missing")
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return {"status": "RED", "error": type(exc).__name__}
    age = current - vintage.astimezone(UTC)
    max_age = timedelta(days=10)
    return {
        "status": "GREEN" if age <= max_age else "STALE",
        "data_vintage": vintage.astimezone(UTC).isoformat(),
        "age_seconds": max(0, int(age.total_seconds())),
        "max_age_seconds": int(max_age.total_seconds()),
    }


def _automation_status(response: httpx.Response) -> dict[str, object]:
    try:
        response.raise_for_status()
        payload = response.json()
        overall = payload["overall_status"]
        jobs = payload["jobs"]
        if overall not in {"CURRENT", "STALE", "UNKNOWN"} or not isinstance(jobs, list):
            raise ValueError("invalid status contract")
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return {"status": "RED", "error": type(exc).__name__}
    return {
        "status": "GREEN" if overall == "CURRENT" else "RED",
        "reported_status": overall,
        "job_count": len(jobs),
        "missed_jobs": sorted(
            str(item["job_id"])
            for item in jobs
            if isinstance(item, dict)
            and (item.get("missed_expected_run") or item.get("missed_expected_release"))
        ),
    }


def _base(value: str) -> str:
    return value.rstrip("/") + "/"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-base", required=True)
    parser.add_argument("--api-base")
    parser.add_argument("--require-api", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.require_api and args.api_base is None:
        raise UptimeCheckError("--require-api requires --api-base")
    payload = run_checks(
        args.site_base,
        api_base=args.api_base,
        require_api=args.require_api,
    )
    write_receipt(args.output, payload)
    print(json.dumps(payload, sort_keys=True))
    if payload["status"] == "RED":
        raise UptimeCheckError("Required public uptime or freshness check failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
