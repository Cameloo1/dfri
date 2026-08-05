"""Shared, redacted HTTP behavior for authoritative public sources."""

from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

DEFAULT_USER_AGENT = "Camelon Systems DFRI/0.1 (contact: ops@camelon.app)"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
type QueryValue = str | int | float | bool | None
type QueryMapping = Mapping[str, QueryValue]


class SourceRequestError(RuntimeError):
    """A redacted source request failure with no credential-bearing URL."""


@dataclass(frozen=True)
class HttpReceipt:
    content: bytes
    source_url: str
    checksum: str
    retrieved_at: datetime
    status_code: int


@dataclass(frozen=True)
class HttpFileReceipt:
    """Receipt for a streamed source body stored as deterministic gzip."""

    path: Path
    source_url: str
    checksum: str
    compressed_checksum: str
    retrieved_at: datetime
    status_code: int
    byte_count: int
    compressed_byte_count: int


def safe_source_url(url: str, params: QueryMapping | None = None) -> str:
    """Build a provenance URL while omitting credential-like parameters."""

    if not params:
        return str(httpx.URL(url).copy_with(query=None))
    forbidden = {"api_key", "key", "token", "userid", "user_id"}
    safe_params = {
        key: str(value) for key, value in params.items() if key.casefold() not in forbidden
    }
    return str(httpx.URL(url, params=safe_params))


class HttpTransport:
    """Bounded retries, source pacing, checksums, and secret-safe errors."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        min_interval_seconds: float = 0.0,
        max_attempts: int = 3,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(60.0), follow_redirects=True)
        self._user_agent = user_agent
        self._min_interval_seconds = min_interval_seconds
        self._max_attempts = max_attempts
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request_at: float | None = None

    def __enter__(self) -> HttpTransport:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get(
        self,
        url: str,
        *,
        params: QueryMapping | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpReceipt:
        source_url = safe_source_url(url, params)
        request_headers = {"User-Agent": self._user_agent, "Accept": "*/*"}
        if headers:
            request_headers.update(headers)

        for attempt in range(self._max_attempts):
            self._pace()
            try:
                response = self._client.get(url, params=params, headers=request_headers)
            except httpx.HTTPError as exc:
                if attempt + 1 < self._max_attempts:
                    self._sleep(2.0**attempt)
                    continue
                raise SourceRequestError(f"GET failed after retries: {source_url}") from exc

            if response.status_code in RETRYABLE_STATUS and attempt + 1 < self._max_attempts:
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit() else 2.0**attempt
                )
                self._sleep(delay)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SourceRequestError(
                    f"GET returned HTTP {response.status_code}: {source_url}"
                ) from exc
            content = response.content
            return HttpReceipt(
                content=content,
                source_url=source_url,
                checksum=hashlib.sha256(content).hexdigest(),
                retrieved_at=self._now(),
                status_code=response.status_code,
            )
        raise AssertionError("retry loop exited unexpectedly")

    def get_to_gzip(self, url: str, destination: Path) -> HttpFileReceipt:
        """Stream one response to a new deterministic gzip file without buffering it."""

        source_url = safe_source_url(url)
        if destination.exists():
            raise SourceRequestError(f"Refusing to overwrite streamed source: {source_url}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        for attempt in range(self._max_attempts):
            self._pace()
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            os.close(file_descriptor)
            temporary_path = Path(temporary_name)
            digest = hashlib.sha256()
            byte_count = 0
            try:
                with self._client.stream(
                    "GET",
                    url,
                    headers={"User-Agent": self._user_agent, "Accept": "application/xml"},
                ) as response:
                    if response.status_code in RETRYABLE_STATUS:
                        raise httpx.HTTPStatusError(
                            "retryable response", request=response.request, response=response
                        )
                    response.raise_for_status()
                    with temporary_path.open("wb") as raw_handle:
                        with gzip.GzipFile(
                            filename="", mode="wb", fileobj=raw_handle, compresslevel=6, mtime=0
                        ) as compressed:
                            for chunk in response.iter_bytes():
                                digest.update(chunk)
                                byte_count += len(chunk)
                                compressed.write(chunk)
                temporary_path.replace(destination)
                compressed_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                return HttpFileReceipt(
                    path=destination,
                    source_url=source_url,
                    checksum=digest.hexdigest(),
                    compressed_checksum=compressed_digest,
                    retrieved_at=self._now(),
                    status_code=response.status_code,
                    byte_count=byte_count,
                    compressed_byte_count=destination.stat().st_size,
                )
            except httpx.HTTPError as exc:
                temporary_path.unlink(missing_ok=True)
                if attempt + 1 < self._max_attempts:
                    exception_response = getattr(exc, "response", None)
                    retry_after = (
                        exception_response.headers.get("Retry-After")
                        if exception_response
                        else None
                    )
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.isdigit()
                        else 2.0**attempt
                    )
                    self._sleep(delay)
                    continue
                raise SourceRequestError(f"GET failed after retries: {source_url}") from exc
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise
        raise AssertionError("retry loop exited unexpectedly")

    def _pace(self) -> None:
        current = self._monotonic()
        if self._last_request_at is not None:
            remaining = self._min_interval_seconds - (current - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
                current = self._monotonic()
        self._last_request_at = current
