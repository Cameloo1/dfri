from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import dfri.ops.uptime as uptime_module
from dfri.ops.uptime import UptimeCheckError, main, run_checks


def client_for(*, vintage: str = "2026-07-31T20:15:00+00:00") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/feeds/scoreboard.json"):
            return httpx.Response(200, json={"meta": {"data_vintage": vintage}, "data": []})
        if request.url.path.endswith("/v1/health"):
            return httpx.Response(200, json={"status": "GREEN"})
        if request.url.path.endswith("/v1/status.json"):
            return httpx.Response(200, json={"overall_status": "CURRENT", "jobs": []})
        return httpx.Response(200, text="ok")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_uptime_receipt_is_green_when_api_is_owner_deferred() -> None:
    with client_for() as client:
        receipt = run_checks(
            "https://cameloo1.github.io/dfri/",
            utc_clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
            client=client,
        )

    assert receipt["status"] == "GREEN"
    assert receipt["site"]["status"] == "GREEN"
    assert len(receipt["site"]["checks"]) == 14
    assert any(item["url"].endswith("/v2/feeds/schema.json") for item in receipt["site"]["checks"])
    assert receipt["nowcast_freshness"]["status"] == "GREEN"
    assert receipt["automation"]["status"] == "GREEN"
    assert receipt["api"] == {
        "status": "DEFERRED",
        "required": False,
        "reason": "owner_deferred_until_programmatic_demand_or_unwieldy_m5_feeds",
        "checks": [],
    }


def test_uptime_receipt_is_green_with_required_live_api() -> None:
    with client_for() as client:
        receipt = run_checks(
            "https://cameloo1.github.io/dfri/",
            api_base="https://api.example/",
            require_api=True,
            utc_clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
            client=client,
        )

    assert receipt["status"] == "GREEN"
    assert receipt["api"]["status"] == "GREEN"
    assert len(receipt["api"]["checks"]) == 7


def test_configured_api_is_required_even_without_explicit_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/feeds/scoreboard.json"):
            return httpx.Response(
                200,
                json={"meta": {"data_vintage": "2026-07-31T20:15:00+00:00"}, "data": []},
            )
        if request.url.path.startswith("/v1/"):
            if request.url.path.endswith("/v1/status.json"):
                return httpx.Response(200, json={"overall_status": "CURRENT", "jobs": []})
            return httpx.Response(503)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        receipt = run_checks(
            "https://cameloo1.github.io/dfri/",
            api_base="https://api.example/",
            utc_clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
            client=client,
        )

    assert receipt["status"] == "RED"
    assert receipt["api"]["status"] == "RED"
    assert receipt["api"]["required"] is True


def test_uptime_receipt_fails_closed_on_stale_publication() -> None:
    with client_for(vintage="2026-06-01T00:00:00+00:00") as client:
        receipt = run_checks(
            "https://cameloo1.github.io/dfri/",
            utc_clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
            client=client,
        )

    assert receipt["status"] == "RED"
    assert receipt["nowcast_freshness"]["status"] == "STALE"


def test_uptime_fails_closed_on_http_and_feed_contract_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scoreboard/"):
            raise httpx.ConnectError("offline", request=request)
        if request.url.path.endswith("/v1/feeds/scoreboard.json"):
            return httpx.Response(200, json={"meta": {"data_vintage": "not-a-date"}})
        if request.url.path.endswith("/v1/status.json"):
            return httpx.Response(200, json={"overall_status": "STALE", "jobs": []})
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        receipt = run_checks(
            "https://cameloo1.github.io/dfri/",
            utc_clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
            client=client,
        )

    assert receipt["status"] == "RED"
    assert receipt["site"]["status"] == "RED"
    assert receipt["nowcast_freshness"]["status"] == "RED"
    assert any("error" in item for item in receipt["site"]["checks"])


def test_uptime_cli_writes_deferred_receipt_and_rejects_bad_required_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "uptime.json"
    expected = {
        "status": "GREEN",
        "site": {"status": "GREEN", "checks": []},
        "nowcast_freshness": {"status": "GREEN"},
        "automation": {"status": "GREEN"},
        "api": {
            "status": "DEFERRED",
            "required": False,
            "reason": "owner_deferred_until_programmatic_demand_or_unwieldy_m5_feeds",
            "checks": [],
        },
    }
    monkeypatch.setattr(uptime_module, "run_checks", lambda *args, **kwargs: expected)

    assert main(["--site-base", "https://example.test", "--output", str(output)]) == 0
    assert output.is_file()
    with pytest.raises(UptimeCheckError, match="requires --api-base"):
        main(
            [
                "--site-base",
                "https://example.test",
                "--require-api",
                "--output",
                str(output),
            ]
        )


def test_uptime_cli_preserves_red_receipt_before_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "uptime.json"
    expected = {"status": "RED"}
    monkeypatch.setattr(uptime_module, "run_checks", lambda *args, **kwargs: expected)

    with pytest.raises(UptimeCheckError, match="Required public"):
        main(["--site-base", "https://example.test", "--output", str(output)])
    assert output.read_text(encoding="utf-8").endswith("\n")
