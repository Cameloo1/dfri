from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfri.api.app import create_app
from dfri.api.benchmark import ApiLatencyError, benchmark_api
from dfri.api.benchmark import main as benchmark_main
from dfri.api.openapi import OpenApiDriftError, check_openapi, write_openapi
from dfri.lake.store import AppendOnlyParquetStore
from dfri.nowcast.bridge import BridgeForecast
from dfri.publish.ledger import PredictionLedger
from dfri.publish.site import publish_scoreboard

PUBLISHED_AT = datetime(2026, 8, 5, 5, 0, tzinfo=UTC)
DATA_VINTAGE = datetime(2026, 7, 31, 20, 15, tzinfo=UTC)


def build_publication(tmp_path: Path) -> Path:
    store = AppendOnlyParquetStore(tmp_path / "ledger")
    ledger = PredictionLedger(store)
    for series, point, suffix in (
        ("DELTA_DTCTLR.M", 4_300.0, "a"),
        ("DELTA_DTCTLN.M", 9_800.0, "b"),
    ):
        ledger.append(
            BridgeForecast(
                model_version="bridge-ridge-v2-alpha10",
                target_series=series,
                target_period=date(2026, 7, 31),
                made_at=datetime(2026, 8, 4, 20, 0, tzinfo=UTC),
                point=point,
                low80=point - 1_000,
                high80=point + 1_000,
                low95=point - 2_000,
                high95=point + 2_000,
                training_observations=101,
                inputs_hash=suffix * 64,
            )
        )
    output = tmp_path / "published"
    publish_scoreboard(
        store,
        output,
        published_at=PUBLISHED_AT,
        data_vintage=DATA_VINTAGE,
        publication_mode="preview",
        project_root=Path(__file__).parents[2],
    )
    return output


def test_exact_read_only_api_contract_and_payloads(tmp_path: Path) -> None:
    output = build_publication(tmp_path)
    client = TestClient(
        create_app(output, rate_limit=1_000, utc_clock=lambda: datetime(2026, 8, 5, tzinfo=UTC))
    )
    expected = {
        "/v1/nowcast/latest": 2,
        "/v1/nowcast/history": 2,
        "/v1/scoreboard": 2,
        "/v1/companies": 10,
        "/v1/assumptions": None,
        "/v1/methodology/versions": 1,
        "/v1/releases/calendar": None,
    }
    for path, count in expected.items():
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"].startswith("public")
        assert response.headers["etag"].startswith('"')
        assert response.headers["x-ratelimit-limit"] == "1000"
        if count is not None:
            assert len(response.json()["data"]) == count

    company = client.get("/v1/companies/gm")
    assert company.status_code == 200
    assert company.json()["data"]["ticker"] == "GM"
    assert client.get("/v1/companies/not-covered").status_code == 404
    health = client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "GREEN"
    assert {item["source_id"] for item in health.json()["sources"]} == {
        "NOWCAST_PUBLICATION",
        "ATTRIBUTION_PUBLICATION",
    }


def test_etag_cors_query_bounds_and_missing_publication_fail_closed(tmp_path: Path) -> None:
    output = build_publication(tmp_path)
    client = TestClient(create_app(output, rate_limit=1_000))

    first = client.get("/v1/companies", headers={"Origin": "https://reader.example"})
    cached = client.get("/v1/companies", headers={"If-None-Match": first.headers["etag"]})
    assert first.headers["access-control-allow-origin"] == "*"
    assert cached.status_code == 304
    assert cached.content == b""
    assert client.get("/v1/nowcast/history?limit=0").status_code == 422
    assert client.get("/v1/nowcast/history?limit=1001").status_code == 422

    missing = TestClient(create_app(tmp_path / "missing", rate_limit=1_000))
    response = missing.get("/v1/companies")
    assert response.status_code == 503
    assert response.json()["status"] == "BLOCKED"


def test_rate_limit_is_per_ip_and_returns_retry_contract(tmp_path: Path) -> None:
    output = build_publication(tmp_path)
    clock_value = [100.0]
    client = TestClient(create_app(output, rate_limit=3, monotonic_clock=lambda: clock_value[0]))

    assert [client.get("/v1/companies").status_code for _ in range(3)] == [200, 200, 200]
    blocked = client.get("/v1/companies")
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    clock_value[0] = 161.0
    assert client.get("/v1/companies").status_code == 200


def test_openapi_is_committed_shape_and_contains_no_mutations(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"
    write_openapi(output)
    first = output.read_bytes()
    write_openapi(output)
    assert output.read_bytes() == first

    payload = json.loads(first)
    exact = {
        "/v1/nowcast/latest",
        "/v1/nowcast/history",
        "/v1/scoreboard",
        "/v1/companies",
        "/v1/companies/{ticker}",
        "/v1/assumptions",
        "/v1/methodology/versions",
        "/v1/releases/calendar",
        "/v1/health",
    }
    assert set(payload["paths"]) == exact
    assert all(set(contract) == {"get"} for contract in payload["paths"].values())
    assert check_openapi(output) == output
    output.write_bytes(output.read_bytes().replace(b"\n", b"\r\n"))
    assert check_openapi(output) == output
    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(OpenApiDriftError, match="stale"):
        check_openapi(output)


def test_published_dataset_api_p95_is_under_300ms(tmp_path: Path) -> None:
    output = build_publication(tmp_path)
    client = TestClient(create_app(output, rate_limit=1_000))
    client.get("/v1/companies")
    durations: list[float] = []
    for _ in range(100):
        started = time.perf_counter()
        response = client.get("/v1/companies")
        durations.append((time.perf_counter() - started) * 1_000)
        assert response.status_code == 200

    p95 = sorted(durations)[94]
    assert p95 < 300, f"API p95 was {p95:.2f} ms"

    receipt = benchmark_api(output, iterations=20)
    assert receipt.status == "PASS"
    assert receipt.p95_ms < receipt.budget_ms


def test_api_benchmark_cli_receipt_and_failure_boundaries(tmp_path: Path) -> None:
    output = build_publication(tmp_path)
    evidence = tmp_path / "latency.json"

    assert (
        benchmark_main(
            [
                "--publication-root",
                str(output),
                "--iterations",
                "20",
                "--output",
                str(evidence),
            ]
        )
        == 0
    )
    assert json.loads(evidence.read_text(encoding="utf-8"))["status"] == "PASS"
    with pytest.raises(ApiLatencyError, match="at least 20"):
        benchmark_api(output, iterations=19)
    with pytest.raises(ApiLatencyError, match="warmup failed"):
        benchmark_api(tmp_path / "missing", iterations=20)
    with pytest.raises(ApiLatencyError, match="not below"):
        benchmark_api(output, iterations=20, budget_ms=0.000001)
