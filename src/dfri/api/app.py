"""Read-only FastAPI surface over a complete DFRI publication directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import pyarrow.parquet as pq
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from dfri.ingest.calendar import serializable_calendar_rows

CACHE_CONTROL: Final = "public, max-age=300, stale-while-revalidate=3600"
HEALTH_CACHE_CONTROL: Final = "public, max-age=60"
RATE_LIMIT: Final = 60
RATE_WINDOW_SECONDS: Final = 60.0


class PublishedDatasetError(RuntimeError):
    """The selected publication directory does not satisfy its stable contract."""


class PublishedDataset:
    """Lazy immutable view over one atomically promoted static publication."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._json_cache: dict[str, dict[str, Any]] = {}
        self._parquet_cache: dict[str, list[dict[str, Any]]] = {}

    def json_feed(self, filename: str) -> dict[str, Any]:
        if filename not in self._json_cache:
            path = self.root / "v1" / "feeds" / filename
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PublishedDatasetError(f"Unavailable published feed: {filename}") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise PublishedDatasetError(f"Invalid published feed: {filename}")
            self._json_cache[filename] = cast(dict[str, Any], payload)
        return self._json_cache[filename]

    def parquet_rows(self, filename: str) -> list[dict[str, Any]]:
        if filename not in self._parquet_cache:
            path = self.root / "v1" / "feeds" / filename
            try:
                rows = pq.read_table(path).to_pylist()
            except (OSError, ValueError) as exc:
                raise PublishedDatasetError(f"Unavailable published feed: {filename}") from exc
            if not all(isinstance(row, dict) for row in rows):
                raise PublishedDatasetError(f"Invalid published Parquet: {filename}")
            self._parquet_cache[filename] = cast(list[dict[str, Any]], rows)
        return self._parquet_cache[filename]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Small in-process fixed-window limiter for the unauthenticated v1 API."""

    def __init__(
        self,
        app: Any,
        *,
        limit: int = RATE_LIMIT,
        window_seconds: float = RATE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(app)
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._requests: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method != "GET":
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        current = self.clock()
        with self._lock:
            history = self._requests[client]
            boundary = current - self.window_seconds
            while history and history[0] <= boundary:
                history.popleft()
            if len(history) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (current - history[0])))
                return JSONResponse(
                    {"detail": "Rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
                )
            history.append(current)
            remaining = self.limit - len(history)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


def create_app(
    publication_root: Path = Path("published/public"),
    *,
    rate_limit: int = RATE_LIMIT,
    monotonic_clock: Callable[[], float] = time.monotonic,
    utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """Create the exact read-only v1 contract over one publication root."""

    dataset = PublishedDataset(publication_root)
    application = FastAPI(
        title="DFRI read-only API",
        version="1.0.0",
        description="Published DFRI nowcasts, grades, company estimates, and provenance.",
        docs_url="/docs",
        redoc_url=None,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["If-None-Match"],
        expose_headers=["ETag", "Cache-Control", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
    )
    application.add_middleware(
        RateLimitMiddleware,
        limit=rate_limit,
        clock=monotonic_clock,
    )

    def respond(
        request: Request,
        payload: object,
        *,
        cache_control: str = CACHE_CONTROL,
    ) -> Response:
        return _conditional_json(request, payload, cache_control=cache_control)

    @application.get("/v1/nowcast/latest", operation_id="get_nowcast_latest")
    async def nowcast_latest(request: Request) -> Response:
        feed = dataset.json_feed("nowcast_predictions.json")
        rows = dataset.parquet_rows("nowcast_predictions.parquet")
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            series = str(row["target_series"])
            if series not in latest or str(row["made_at"]) > str(latest[series]["made_at"]):
                latest[series] = row
        return respond(request, {"meta": feed["meta"], "data": list(latest.values())})

    @application.get("/v1/nowcast/history", operation_id="get_nowcast_history")
    async def nowcast_history(
        request: Request,
        limit: int = Query(default=1_000, ge=1, le=1_000),
    ) -> Response:
        feed = dataset.json_feed("nowcast_predictions.json")
        rows = dataset.parquet_rows("nowcast_predictions.parquet")
        return respond(request, {"meta": feed["meta"], "data": rows[-limit:]})

    @application.get("/v1/scoreboard", operation_id="get_scoreboard")
    async def scoreboard(request: Request) -> Response:
        return respond(request, dataset.json_feed("scoreboard.json"))

    @application.get("/v1/companies", operation_id="get_companies")
    async def companies(request: Request) -> Response:
        feed = dataset.json_feed("dfri_companies.json")
        rows = dataset.parquet_rows("dfri_companies.parquet")
        return respond(request, {"meta": feed["meta"], "data": rows})

    @application.get("/v1/companies/{ticker}", operation_id="get_company")
    async def company(ticker: str, request: Request) -> Response:
        rows = dataset.parquet_rows("dfri_companies.parquet")
        match = next((row for row in rows if str(row["ticker"]).upper() == ticker.upper()), None)
        if match is None:
            raise HTTPException(status_code=404, detail="Company is not in the published coverage")
        feed = dataset.json_feed("dfri_companies.json")
        return respond(request, {"meta": feed["meta"], "data": match})

    @application.get("/v1/assumptions", operation_id="get_assumptions")
    async def assumptions(request: Request) -> Response:
        return respond(request, dataset.json_feed("assumptions.json"))

    @application.get("/v1/methodology/versions", operation_id="get_methodology_versions")
    async def methodology_versions(request: Request) -> Response:
        feed = dataset.json_feed("dfri_companies.json")
        meta = cast(Mapping[str, Any], feed["meta"])
        payload = {
            "data": [
                {
                    "version": meta["methodology_version"],
                    "data_vintage": meta["data_vintage"],
                    "status": "active",
                    "methodology_url": "/methodology/",
                    "changelog_url": "/changelog/",
                }
            ]
        }
        return respond(request, payload)

    @application.get("/v1/releases/calendar", operation_id="get_releases_calendar")
    async def releases_calendar(request: Request) -> Response:
        return respond(request, {"data": serializable_calendar_rows()})

    @application.get("/v1/health", operation_id="get_health")
    async def health(request: Request) -> Response:
        now = utc_clock().astimezone(UTC)
        nowcast_meta = cast(Mapping[str, Any], dataset.json_feed("scoreboard.json")["meta"])
        attribution_meta = cast(Mapping[str, Any], dataset.json_feed("dfri_companies.json")["meta"])
        sources = (
            _freshness("NOWCAST_PUBLICATION", str(nowcast_meta["data_vintage"]), now, 10),
            _freshness("ATTRIBUTION_PUBLICATION", str(attribution_meta["data_vintage"]), now, 150),
        )
        payload = {
            "status": "GREEN" if all(item["status"] == "GREEN" for item in sources) else "STALE",
            "as_of": now.isoformat(),
            "sources": sources,
        }
        return respond(request, payload, cache_control=HEALTH_CACHE_CONTROL)

    @application.exception_handler(PublishedDatasetError)
    async def unavailable_dataset(_request: Request, exc: PublishedDatasetError) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "status": "BLOCKED"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    return application


def _conditional_json(request: Request, payload: object, *, cache_control: str) -> Response:
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    etag = f'"{hashlib.sha256(content).hexdigest()}"'
    headers = {"ETag": etag, "Cache-Control": cache_control}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content, media_type="application/json", headers=headers)


def _freshness(
    source_id: str, vintage_raw: str, as_of: datetime, max_age_days: int
) -> dict[str, Any]:
    try:
        vintage = datetime.fromisoformat(vintage_raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise PublishedDatasetError(f"Invalid data vintage for {source_id}") from exc
    age = as_of - vintage
    return {
        "source_id": source_id,
        "status": "GREEN" if age <= timedelta(days=max_age_days) else "STALE",
        "data_vintage": vintage.isoformat(),
        "age_seconds": max(0, int(age.total_seconds())),
        "max_age_seconds": max_age_days * 86_400,
    }


app = create_app()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication-root", type=Path, default=Path("published/public"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(args.publication_root), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
