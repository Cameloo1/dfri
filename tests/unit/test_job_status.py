from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dfri.ops.job_status import (
    JobStatusError,
    build_status_report,
    load_success_receipts,
    main,
    record_success,
    render_status_banner,
)


def test_content_addressed_success_receipts_are_idempotent_and_tamper_evident(
    tmp_path: Path,
) -> None:
    succeeded_at = datetime(2026, 8, 10, 23, 17, tzinfo=UTC)
    first = record_success(
        tmp_path,
        job_id="h8-predict",
        succeeded_at=succeeded_at,
        workflow_run_url="https://github.com/Cameloo1/dfri/actions/runs/1",
    )
    second = record_success(
        tmp_path,
        job_id="h8-predict",
        succeeded_at=succeeded_at,
        workflow_run_url="https://github.com/Cameloo1/dfri/actions/runs/1",
    )

    assert first == second
    assert len(load_success_receipts(tmp_path)) == 1
    payload = json.loads(first.read_text(encoding="utf-8"))
    payload["succeeded_at"] = "2026-08-11T00:00:00+00:00"
    first.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(JobStatusError, match="content-addressed"):
        load_success_receipts(tmp_path)


def test_live_status_reports_missed_runs_and_preview_does_not_claim_live_health(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 8, 10, 23, 30, tzinfo=UTC)
    record_success(
        tmp_path,
        job_id="h8-predict",
        succeeded_at=datetime(2026, 8, 7, 23, 17, tzinfo=UTC),
        workflow_run_url="https://github.com/Cameloo1/dfri/actions/runs/1",
    )
    record_success(
        tmp_path,
        job_id="h8-predict",
        succeeded_at=datetime(2026, 8, 10, 23, 17, tzinfo=UTC),
        workflow_run_url="https://github.com/Cameloo1/dfri/actions/runs/2",
    )

    live = build_status_report(as_of=as_of, receipt_directory=tmp_path, publication_mode="live")
    preview = build_status_report(
        as_of=as_of, receipt_directory=tmp_path, publication_mode="preview"
    )
    by_id = {item["job_id"]: item for item in live["jobs"]}

    assert live["schema_version"] == "v1"
    assert live["overall_status"] == "STALE"
    assert by_id["h8-predict"]["status"] == "CURRENT"
    assert by_id["mts-predict"]["missed_expected_run"] is True
    assert by_id["mts-predict"]["expected_next_run"] == "2026-08-11T16:17:00+00:00"
    assert preview["overall_status"] == "UNKNOWN"
    assert all(item["missed_expected_run"] is False for item in preview["jobs"])


def test_mts_release_sla_requires_a_receipt_in_the_correct_window(tmp_path: Path) -> None:
    # The pinned July 2026 MTS first print is due 2026-08-12 at 18:00 UTC.
    as_of = datetime(2026, 8, 12, 22, 1, tzinfo=UTC)
    record_success(
        tmp_path,
        job_id="mts-predict",
        succeeded_at=datetime(2026, 8, 12, 16, 17, tzinfo=UTC),
        workflow_run_url="https://github.com/Cameloo1/dfri/actions/runs/3",
    )
    report = build_status_report(as_of=as_of, receipt_directory=tmp_path, publication_mode="live")
    by_id = {item["job_id"]: item for item in report["jobs"]}

    assert by_id["mts-predict"]["missed_expected_release"] is False
    assert by_id["mts-grade"]["missed_expected_release"] is True
    assert by_id["mts-grade"]["release_check_status"] == "MISSED"


def test_receipt_rejects_unknown_job_and_non_github_url(tmp_path: Path) -> None:
    with pytest.raises(JobStatusError, match="Unknown"):
        record_success(
            tmp_path,
            job_id="mystery",
            succeeded_at=datetime(2026, 8, 10, tzinfo=UTC),
            workflow_run_url="https://github.com/Cameloo1/dfri/actions/runs/4",
        )
    with pytest.raises(JobStatusError, match="GitHub"):
        record_success(
            tmp_path,
            job_id="h8-predict",
            succeeded_at=datetime(2026, 8, 10, tzinfo=UTC),
            workflow_run_url="https://example.com/run/4",
        )


def test_status_banner_covers_current_stale_unknown_and_rejects_bad_input(tmp_path: Path) -> None:
    current = {
        "overall_status": "CURRENT",
        "generated_at": "2026-08-10T23:30:00+00:00",
        "jobs": [],
    }
    stale = {
        **current,
        "overall_status": "STALE",
        "jobs": [{"label": "H.8 prediction", "missed_expected_run": True}],
    }
    unknown = {**current, "overall_status": "UNKNOWN"}

    assert b"Automation current" in render_status_banner(current)
    assert b"SLA exceeded: H.8 prediction" in render_status_banner(stale)
    assert b"status incomplete" in render_status_banner(unknown)
    with pytest.raises(JobStatusError, match="contract"):
        render_status_banner({"overall_status": "BROKEN"})
    with pytest.raises(JobStatusError, match="jobs"):
        render_status_banner({**current, "jobs": "invalid"})


def test_status_cli_records_and_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "receipts"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-status",
            "record",
            "--directory",
            str(directory),
            "--job-id",
            "h8-predict",
            "--succeeded-at",
            "2026-08-10T23:17:00Z",
            "--workflow-run-url",
            "https://github.com/Cameloo1/dfri/actions/runs/9",
        ],
    )
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"

    output = tmp_path / "status.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-status",
            "report",
            "--directory",
            str(directory),
            "--as-of",
            "2026-08-10T23:30:00Z",
            "--publication-mode",
            "live",
            "--output",
            str(output),
        ],
    )
    assert main() == 0
    assert output.is_file()
    assert json.loads(capsys.readouterr().out)["schema_version"] == "v1"


def test_status_rejects_naive_future_and_malformed_receipts(tmp_path: Path) -> None:
    with pytest.raises(JobStatusError, match="timezone-aware"):
        record_success(
            tmp_path,
            job_id="h8-predict",
            succeeded_at=datetime(2026, 8, 10),  # noqa: DTZ001 - exercises naive-time rejection
            workflow_run_url="https://github.com/Cameloo1/dfri/actions/runs/1",
        )
    receipt = record_success(
        tmp_path,
        job_id="h8-predict",
        succeeded_at=datetime(2026, 8, 11, tzinfo=UTC),
        workflow_run_url="https://github.com/Cameloo1/dfri/actions/runs/2",
    )
    with pytest.raises(JobStatusError, match="follow"):
        build_status_report(
            as_of=datetime(2026, 8, 10, tzinfo=UTC),
            receipt_directory=tmp_path,
            publication_mode="live",
        )
    receipt.write_text("not-json", encoding="utf-8")
    with pytest.raises(JobStatusError, match="Cannot read"):
        load_success_receipts(tmp_path)
