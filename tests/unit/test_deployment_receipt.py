from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dfri.ops.deployment_receipt import DeploymentReceiptError, main, write_deployment_receipt

RELEASED = datetime(2026, 8, 7, 20, 15, tzinfo=UTC)
PUBLISHED = RELEASED + timedelta(hours=1)
SHA = "a" * 40


def write(output: Path, deployed_at: datetime) -> object:
    return write_deployment_receipt(
        output,
        mode="predict",
        source_release_at=RELEASED,
        published_at=PUBLISHED,
        deployed_at=deployed_at,
        page_url="https://example.com/dfri/",
        workflow_url="https://github.com/camelon/dfri/actions/runs/1",
        commit_sha=SHA,
        prediction_appended=2,
        grade_appended=0,
    )


def test_deployment_receipt_records_a_passing_release_sla(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    receipt = write(output, RELEASED + timedelta(hours=3, minutes=59))
    payload = json.loads(output.read_text())

    assert receipt.sla_status == "PASS"
    assert payload["latency_seconds"] == 14_340
    assert payload["prediction_appended"] == 2
    assert payload["source_release_at"] == RELEASED.isoformat()


def test_deployment_receipt_persists_a_failed_sla_for_evidence(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    receipt = write(output, RELEASED + timedelta(hours=4, seconds=1))

    assert receipt.sla_status == "FAIL"
    assert json.loads(output.read_text())["latency_seconds"] == 14_401


@pytest.mark.parametrize(
    ("page_url", "commit_sha", "published_at", "message"),
    [
        ("http://example.com", SHA, PUBLISHED, "HTTPS"),
        ("https://example.com", "bad", PUBLISHED, "SHA-1"),
        ("https://example.com", SHA, RELEASED - timedelta(seconds=1), "out of order"),
    ],
)
def test_deployment_receipt_rejects_invalid_evidence(
    tmp_path: Path,
    page_url: str,
    commit_sha: str,
    published_at: datetime,
    message: str,
) -> None:
    with pytest.raises(DeploymentReceiptError, match=message):
        write_deployment_receipt(
            tmp_path / "receipt.json",
            mode="grade",
            source_release_at=RELEASED,
            published_at=published_at,
            deployed_at=RELEASED + timedelta(hours=1),
            page_url=page_url,
            workflow_url="https://github.com/camelon/dfri/actions/runs/1",
            commit_sha=commit_sha,
            prediction_appended=0,
            grade_appended=1,
        )


def test_deployment_receipt_rejects_invalid_operational_boundaries(tmp_path: Path) -> None:
    valid = {
        "output": tmp_path / "receipt.json",
        "mode": "predict",
        "source_release_at": RELEASED,
        "published_at": PUBLISHED,
        "deployed_at": RELEASED + timedelta(hours=2),
        "page_url": "https://example.com/dfri/",
        "workflow_url": "https://github.com/camelon/dfri/actions/runs/1",
        "commit_sha": SHA,
        "prediction_appended": 1,
        "grade_appended": 0,
    }
    with pytest.raises(DeploymentReceiptError, match="mode"):
        write_deployment_receipt(**{**valid, "mode": "replace"})
    with pytest.raises(DeploymentReceiptError, match="timezone-aware"):
        write_deployment_receipt(**{**valid, "source_release_at": RELEASED.replace(tzinfo=None)})
    with pytest.raises(DeploymentReceiptError, match="HTTPS"):
        write_deployment_receipt(**{**valid, "workflow_url": "not-a-url"})
    with pytest.raises(DeploymentReceiptError, match="negative"):
        write_deployment_receipt(**{**valid, "prediction_appended": -1})


def test_deployment_receipt_cli_reports_pass_and_persists_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "pass.json"
    common = [
        "dfri-deployment-receipt",
        "--mode",
        "predict",
        "--source-release-at",
        RELEASED.isoformat(),
        "--published-at",
        PUBLISHED.isoformat(),
        "--page-url",
        "https://example.com/dfri/",
        "--workflow-url",
        "https://github.com/camelon/dfri/actions/runs/1",
        "--commit-sha",
        SHA,
        "--prediction-appended",
        "1",
        "--grade-appended",
        "0",
    ]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            *common,
            "--output",
            str(output),
            "--deployed-at",
            (RELEASED + timedelta(hours=2)).isoformat(),
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["sla_status"] == "PASS"

    failed = tmp_path / "failed.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            *common,
            "--output",
            str(failed),
            "--deployed-at",
            (RELEASED + timedelta(hours=5)).isoformat(),
        ],
    )
    with pytest.raises(SystemExit, match="1"):
        main()
    assert json.loads(failed.read_text())["sla_status"] == "FAIL"
