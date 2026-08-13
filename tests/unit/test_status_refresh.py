from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from dfri.ops import status_refresh
from dfri.ops.status_refresh import main, refresh_public_status

from .test_api_app import build_publication


def test_status_refresh_changes_only_status_documents_and_manifest(tmp_path: Path) -> None:
    source = build_publication(tmp_path / "source")
    before = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }

    def handler(request: httpx.Request) -> httpx.Response:
        relative = unquote(request.url.path.split("/dfri/", 1)[1])
        path = source / relative
        return (
            httpx.Response(200, content=path.read_bytes())
            if path.is_file()
            else httpx.Response(404)
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        receipt = refresh_public_status(
            "https://example.test/dfri/",
            tmp_path / "refreshed",
            as_of=datetime(2026, 8, 12, 23, tzinfo=UTC),
            client=client,
        )
    output = tmp_path / "refreshed"
    after = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }

    assert receipt["status"] == "PASS"
    assert receipt["overall_status"] == "STALE"
    assert set(before) == set(after)
    assert {path for path in before if before[path] != after[path]} == {
        "manifest.json",
        "status/banner.html",
        "v1/status.json",
    }
    status = json.loads(after["v1/status.json"])
    assert status["publication_mode"] == "live"
    assert b"Automation stale" in after["status/banner.html"]


def test_status_refresh_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = {"status": "PASS", "overall_status": "CURRENT"}
    monkeypatch.setattr(status_refresh, "refresh_public_status", lambda *_args, **_kwargs: expected)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "status-refresh",
            "--site-base",
            "https://example.test/dfri/",
            "--output-root",
            str(tmp_path / "output"),
            "--as-of",
            "2026-08-10T23:30:00Z",
        ],
    )

    assert main() == 0
    assert json.loads(capsys.readouterr().out) == expected
