from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from dfri.ops import status_refresh
from dfri.ops.job_status import build_status_report, record_success
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
    refreshed_manifest = json.loads(after["manifest.json"])
    refreshed_paths = [entry["path"] for entry in refreshed_manifest["files"]]
    assert refreshed_paths == sorted(refreshed_paths)
    status = json.loads(after["v1/status.json"])
    assert status["publication_mode"] == "live"
    assert b"Automation stale" in after["status/banner.html"]


def test_status_refresh_merges_verified_clock_history_for_release_sla(tmp_path: Path) -> None:
    source = build_publication(tmp_path / "source")
    as_of = datetime(2026, 9, 29, 20, 44, tzinfo=UTC)
    public_receipts = tmp_path / "public-receipts"
    record_success(
        public_receipts,
        job_id="h8-predict",
        succeeded_at=datetime(2026, 9, 29, 2, 36, tzinfo=UTC),
        workflow_run_url="https://github.com/Cameloo1/dfri/actions/runs/2",
    )
    public_status = build_status_report(
        as_of=as_of,
        receipt_directory=public_receipts,
        publication_mode="live",
    )
    assert (
        next(job for job in public_status["jobs"] if job["job_id"] == "h8-predict")[
            "missed_expected_release"
        ]
        is True
    )
    status_path = source / "v1" / "status.json"
    status_path.write_text(json.dumps(public_status), encoding="utf-8")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "v1/status.json":
            content = status_path.read_bytes()
            entry["bytes"] = len(content)
            entry["sha256"] = hashlib.sha256(content).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    scheduled_receipts = tmp_path / "scheduled-receipts"
    record_success(
        scheduled_receipts,
        job_id="h8-predict",
        succeeded_at=datetime(2026, 9, 25, 22, 17, tzinfo=UTC),
        workflow_run_url="https://github.com/Cameloo1/dfri/actions/runs/1",
    )
    # Keep the public latest receipt in the recovered artifact too, as the real runtime bundle does.
    record_success(
        scheduled_receipts,
        job_id="h8-predict",
        succeeded_at=datetime(2026, 9, 29, 2, 36, tzinfo=UTC),
        workflow_run_url="https://github.com/Cameloo1/dfri/actions/runs/2",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        relative = unquote(request.url.path.split("/dfri/", 1)[1])
        path = source / relative
        return (
            httpx.Response(200, content=path.read_bytes())
            if path.is_file()
            else httpx.Response(404)
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        refresh_public_status(
            "https://example.test/dfri/",
            tmp_path / "refreshed",
            as_of=as_of,
            job_receipt_directory=scheduled_receipts,
            client=client,
        )

    refreshed = json.loads((tmp_path / "refreshed" / "v1" / "status.json").read_text())
    h8 = next(job for job in refreshed["jobs"] if job["job_id"] == "h8-predict")
    assert h8["last_successful_run"] == "2026-09-29T02:36:00+00:00"
    assert h8["missed_expected_release"] is False
    assert h8["release_check_status"] == "OBSERVED_ON_TIME"


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
