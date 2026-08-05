from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

import dfri.ingest.spot_audit as spot_audit
from dfri.ingest.board import RELEASE_URLS
from dfri.ingest.spot_audit import (
    SpotAuditError,
    audit_observations,
    fetch_live_observations,
    write_receipt,
)
from dfri.lake.store import AppendOnlyParquetStore

NOW = datetime(2026, 8, 4, 16, 0, tzinfo=UTC)
GROUPS = {
    "BOARD_G19": ("FEDERAL_RESERVE_BOARD", RELEASE_URLS["g19"]),
    "BOARD_H8": ("FEDERAL_RESERVE_BOARD", RELEASE_URLS["h8"]),
    "BEA": ("BEA", "https://apps.bea.gov/api/data/?redacted"),
    "CENSUS": ("CENSUS", "https://api.census.gov/data/timeseries/eits/marts?redacted"),
    "NYFED": (
        "NYFED",
        "https://www.newyorkfed.org/medialibrary/interactives/householdcredit/"
        "data/xls/HHD_C_Report_2026Q1",
    ),
}


def observation_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for group_index, (group, (source, source_url)) in enumerate(GROUPS.items(), start=1):
        checksum = f"{group_index:x}" * 64
        for month in range(1, 6):
            rows.append(
                {
                    "source": source,
                    "series_id": f"{group}:SERIES",
                    "obs_period": date(2026, month, 1),
                    "value": float(group_index * 100 + month),
                    "unit": "Units",
                    "source_url": source_url,
                    "checksum": checksum,
                }
            )
    return rows


def frame(rows: list[dict[str, object]] | None = None) -> pl.DataFrame:
    return pl.DataFrame(
        rows if rows is not None else observation_rows(),
        schema={
            "source": pl.String,
            "series_id": pl.String,
            "obs_period": pl.Date,
            "value": pl.Float64,
            "unit": pl.String,
            "source_url": pl.String,
            "checksum": pl.String,
        },
    )


def raw_rows() -> list[dict[str, object]]:
    return [
        {
            **row,
            "release_date": NOW,
            "vintage_date": NOW.date(),
            "ingested_at": NOW,
        }
        for row in observation_rows()
    ]


def test_audit_is_deterministic_passes_20_rows_and_covers_every_group() -> None:
    stored = frame()
    first = audit_observations(stored, stored.clone(), audited_at=NOW, seed="fixed")
    repeated = audit_observations(stored, stored.clone(), audited_at=NOW, seed="fixed")

    assert first == repeated
    assert first.status == "PASS"
    assert first.audited_rows == first.passed_rows == 20
    assert first.failed_rows == 0
    assert set(first.rows_by_group) == set(GROUPS)
    assert all(count >= 1 for count in first.rows_by_group.values())
    assert len(first.evidence_hash) == 64


def test_audit_fails_when_a_source_identical_value_differs() -> None:
    stored_rows = observation_rows()
    live_rows = [dict(row) for row in stored_rows]
    live_rows[0]["value"] = float(live_rows[0]["value"]) + 1.0

    receipt = audit_observations(
        frame(stored_rows),
        frame(live_rows),
        audited_at=NOW,
        seed="fixed",
        sample_size=len(stored_rows),
    )

    assert receipt.status == "FAIL"
    assert receipt.failed_rows == 1
    assert {sample.status for sample in receipt.samples} == {"PASS", "FAIL"}


def test_changed_live_source_identity_is_blocked_not_called_fabricated() -> None:
    stored_rows = observation_rows()
    live_rows = [dict(row) for row in stored_rows]
    for row in live_rows:
        if row["source"] == "BEA":
            row["checksum"] = "f" * 64

    receipt = audit_observations(frame(stored_rows), frame(live_rows), audited_at=NOW, seed="fixed")

    assert receipt.status == "BLOCKED"
    assert receipt.blocked_groups == ("BEA",)
    assert receipt.failed_rows == 0
    assert receipt.audited_rows == 20
    assert "BEA" not in receipt.rows_by_group


def test_insufficient_source_identical_candidates_is_blocked() -> None:
    five_rows = observation_rows()[:5]
    receipt = audit_observations(
        frame(five_rows),
        frame(five_rows),
        audited_at=NOW,
        seed="fixed",
        expected_groups=("BOARD_G19",),
    )

    assert receipt.status == "BLOCKED"
    assert receipt.audited_rows == 5
    assert receipt.requested_rows == 20


def test_duplicate_and_malformed_frames_fail_closed() -> None:
    rows = observation_rows()
    with pytest.raises(SpotAuditError, match="duplicate"):
        audit_observations(frame([*rows, dict(rows[0])]), frame(rows), audited_at=NOW, seed="fixed")
    with pytest.raises(SpotAuditError, match="missing columns"):
        audit_observations(
            pl.DataFrame(),
            frame(rows),
            audited_at=NOW,
            seed="fixed",
            expected_groups=("BEA",),
        )
    credential_rows = [dict(row) for row in rows]
    credential_rows[10]["source_url"] = "https://api.census.gov/data?key=secret"
    with pytest.raises(SpotAuditError, match="credential-bearing"):
        audit_observations(frame(credential_rows), frame(rows), audited_at=NOW, seed="fixed")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"seed": ""}, "seed"),
        ({"sample_size": 0}, "positive"),
        ({"expected_groups": ()}, "groups"),
        ({"expected_groups": ("BEA", "BEA")}, "groups"),
        ({"audited_at": NOW.replace(tzinfo=None)}, "timezone-aware"),
    ],
)
def test_audit_arguments_fail_closed(kwargs: dict[str, object], message: str) -> None:
    arguments: dict[str, object] = {"audited_at": NOW, "seed": "fixed"}
    arguments.update(kwargs)
    with pytest.raises(SpotAuditError, match=message):
        audit_observations(frame(), frame(), **arguments)  # type: ignore[arg-type]


def test_live_fetch_requires_keys_before_creating_a_workspace(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    with pytest.raises(SpotAuditError, match="BEA_API_KEY"):
        fetch_live_observations(bea_api_key="", census_api_key="", work_root=work_root)
    assert not work_root.exists()


def test_receipt_write_is_atomic_and_serializes_iso_dates(tmp_path: Path) -> None:
    receipt = audit_observations(frame(), frame(), audited_at=NOW, seed="fixed")
    output = tmp_path / "evidence" / "spot-audit.json"

    write_receipt(receipt, output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["status"] == "PASS"
    assert payload["audited_at"] == "2026-08-04T16:00:00+00:00"
    assert payload["samples"][0]["obs_period"].startswith("2026-")
    assert not list(output.parent.glob("*.tmp"))


def test_cli_uses_environment_keys_without_printing_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store = AppendOnlyParquetStore(tmp_path / "lake")
    store.append("raw_observations", raw_rows())
    live = frame()
    calls: list[tuple[str, str]] = []

    def fake_fetch(**kwargs: object) -> pl.DataFrame:
        calls.append((str(kwargs["bea_api_key"]), str(kwargs["census_api_key"])))
        return live

    monkeypatch.setattr(spot_audit, "fetch_live_observations", fake_fetch)
    monkeypatch.setenv("BEA_API_KEY", "bea-secret")
    monkeypatch.setenv("CENSUS_API_KEY", "census-secret")
    output = tmp_path / "receipt.json"

    result = spot_audit.main(
        ["--lake-root", str(store.root), "--work-root", str(tmp_path), "--output", str(output)]
    )
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)

    assert result == 0
    assert payload["status"] == "PASS"
    assert calls == [("bea-secret", "census-secret")]
    assert "bea-secret" not in stdout
    assert "census-secret" not in stdout
    assert "bea-secret" not in output.read_text(encoding="utf-8")
    assert "census-secret" not in output.read_text(encoding="utf-8")
    assert "samples" not in payload
    assert output.exists()


def test_receipt_dataclass_is_immutable() -> None:
    receipt = audit_observations(frame(), frame(), audited_at=NOW, seed="fixed")
    with pytest.raises(AttributeError):
        replace(receipt, status="FAIL").status = "PASS"  # type: ignore[misc]
