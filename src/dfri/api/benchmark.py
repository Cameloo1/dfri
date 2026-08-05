"""Measure the read-only API latency budget against a built publication."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median

from fastapi.testclient import TestClient

from dfri.api.app import create_app


class ApiLatencyError(RuntimeError):
    """The published-dataset API exceeds its blocking latency budget."""


@dataclass(frozen=True)
class ApiLatencyReceipt:
    status: str
    endpoint: str
    iterations: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    budget_ms: float


def benchmark_api(
    publication_root: Path,
    *,
    endpoint: str = "/v1/companies",
    iterations: int = 100,
    budget_ms: float = 300.0,
) -> ApiLatencyReceipt:
    if iterations < 20:
        raise ApiLatencyError("API latency benchmark requires at least 20 iterations")
    client = TestClient(create_app(publication_root, rate_limit=iterations + 10))
    warmup = client.get(endpoint)
    if warmup.status_code != 200:
        raise ApiLatencyError(f"API benchmark warmup failed with HTTP {warmup.status_code}")
    durations: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        response = client.get(endpoint)
        durations.append((time.perf_counter() - started) * 1_000)
        if response.status_code != 200:
            raise ApiLatencyError(f"API benchmark request failed with HTTP {response.status_code}")
    ordered = sorted(durations)
    p95 = ordered[max(0, int(iterations * 0.95) - 1)]
    receipt = ApiLatencyReceipt(
        status="PASS" if p95 < budget_ms else "FAIL",
        endpoint=endpoint,
        iterations=iterations,
        p50_ms=round(median(ordered), 3),
        p95_ms=round(p95, 3),
        max_ms=round(max(ordered), 3),
        budget_ms=budget_ms,
    )
    if receipt.status != "PASS":
        raise ApiLatencyError(
            f"API p95 latency {receipt.p95_ms:.3f}ms is not below {budget_ms:.3f}ms"
        )
    return receipt


def write_receipt(path: Path, receipt: ApiLatencyReceipt) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(asdict(receipt), indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication-root", type=Path, default=Path("published/public"))
    parser.add_argument("--endpoint", default="/v1/companies")
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--budget-ms", type=float, default=300.0)
    parser.add_argument("--output", type=Path, default=Path(".local/evidence/m4-api-latency.json"))
    args = parser.parse_args(argv)
    receipt = benchmark_api(
        args.publication_root,
        endpoint=args.endpoint,
        iterations=args.iterations,
        budget_ms=args.budget_ms,
    )
    write_receipt(args.output, receipt)
    print(json.dumps(asdict(receipt), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
