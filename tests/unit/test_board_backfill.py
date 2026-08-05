from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import dfri.ingest.board_backfill as board_backfill
from dfri.ingest.board import BoardRelease
from dfri.ingest.board_backfill import (
    BoardBackfillError,
    BoardBackfillRunner,
    BoardBackfillSummary,
    build_parser,
)
from dfri.ingest.board_history import BoardHistoryReceipt, BoardHistoryValidation


class FakeClient:
    def __init__(self, dates: dict[BoardRelease, tuple[date, ...]]) -> None:
        self.dates = dates

    def discover_release_dates(self, release: BoardRelease) -> tuple[date, ...]:
        return self.dates[release]


class FakeIngestor:
    def __init__(self, root: Path, fail_once: set[tuple[BoardRelease, date]] | None = None) -> None:
        self.root = root
        self.fail_once = fail_once or set()
        self.calls: list[tuple[BoardRelease, date]] = []

    def fetch_and_ingest(self, release: BoardRelease, release_date: date) -> BoardHistoryReceipt:
        identity = (release, release_date)
        self.calls.append(identity)
        if identity in self.fail_once:
            self.fail_once.remove(identity)
            raise RuntimeError("source unavailable")
        batch = self.root / f"batch-{release}-{release_date.isoformat()}.parquet"
        batch.parent.mkdir(parents=True, exist_ok=True)
        batch.write_bytes(b"fixture")
        return BoardHistoryReceipt(
            release=release,
            release_date=release_date,
            source_url=(
                "https://www.federalreserve.gov/releases/"
                f"{release}/{release_date.strftime('%Y%m%d')}/"
            ),
            checksum="a" * 64,
            row_count=6 if release == "g19" else 12,
            already_present=False,
            batch_path=batch,
            content_hash="b" * 64,
        )


def runner(
    tmp_path: Path,
    client: FakeClient,
    ingestor: FakeIngestor,
) -> BoardBackfillRunner:
    return BoardBackfillRunner(
        client=client,  # type: ignore[arg-type]
        ingestor=ingestor,  # type: ignore[arg-type]
        state_path=tmp_path / "state" / "board.json",
        event_log_path=tmp_path / "evidence" / "board.jsonl",
        now=lambda: datetime(2026, 8, 4, tzinfo=UTC),
    )


def test_backfill_checkpoints_completed_batches_and_skips_them(tmp_path: Path) -> None:
    dates = (date(2015, 1, 8), date(2015, 2, 6))
    client = FakeClient({"g19": dates, "h8": ()})
    ingestor = FakeIngestor(tmp_path / "lake")
    backfill = runner(tmp_path, client, ingestor)

    first = backfill.run(releases=("g19",), start=date(2015, 1, 1), end=date(2015, 2, 28))
    second = backfill.run(releases=("g19",), start=date(2015, 1, 1), end=date(2015, 2, 28))

    assert first.planned == 2
    assert first.attempted == 2
    assert first.completed == 2
    assert second.attempted == 0
    assert second.checkpoint_skipped == 2
    assert ingestor.calls == [("g19", dates[0]), ("g19", dates[1])]

    state = json.loads((tmp_path / "state" / "board.json").read_text(encoding="utf-8"))
    assert state["items"]["g19:2015-01-08"]["status"] == "COMPLETE"
    events = [
        json.loads(line)
        for line in (tmp_path / "evidence" / "board.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["status"] for event in events][-2:] == [
        "CHECKPOINT_SKIPPED",
        "CHECKPOINT_SKIPPED",
    ]


def test_failed_item_is_checkpointed_and_retried_without_repeating_completed(
    tmp_path: Path,
) -> None:
    dates = (date(2015, 1, 8), date(2015, 2, 6))
    client = FakeClient({"g19": dates, "h8": ()})
    ingestor = FakeIngestor(tmp_path / "lake", {("g19", dates[1])})
    backfill = runner(tmp_path, client, ingestor)

    with pytest.raises(BoardBackfillError, match="stopped at"):
        backfill.run(releases=("g19",), start=date(2015, 1, 1), end=date(2015, 2, 28))

    state = json.loads((tmp_path / "state" / "board.json").read_text(encoding="utf-8"))
    assert state["items"]["g19:2015-02-06"]["status"] == "FAILED"

    recovered = backfill.run(releases=("g19",), start=date(2015, 1, 1), end=date(2015, 2, 28))
    assert recovered.completed == 1
    assert recovered.checkpoint_skipped == 1
    assert ingestor.calls.count(("g19", dates[0])) == 1
    assert ingestor.calls.count(("g19", dates[1])) == 2


def test_max_items_bounds_new_network_attempts(tmp_path: Path) -> None:
    dates = (date(2015, 1, 8), date(2015, 2, 6))
    client = FakeClient({"g19": dates, "h8": ()})
    ingestor = FakeIngestor(tmp_path / "lake")

    summary = runner(tmp_path, client, ingestor).run(
        releases=("g19",),
        start=date(2015, 1, 1),
        end=date(2015, 2, 28),
        max_items=1,
    )

    assert summary.planned == 2
    assert summary.attempted == 1
    assert len(ingestor.calls) == 1


def test_corrupt_state_and_invalid_windows_fail_closed(tmp_path: Path) -> None:
    client = FakeClient({"g19": (date(2015, 1, 8),), "h8": ()})
    ingestor = FakeIngestor(tmp_path / "lake")
    backfill = runner(tmp_path, client, ingestor)
    state_path = tmp_path / "state" / "board.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(BoardBackfillError, match="Cannot read"):
        backfill.run(releases=("g19",), start=date(2015, 1, 1), end=date(2015, 2, 1))
    state_path.unlink()
    with pytest.raises(BoardBackfillError, match="start date"):
        backfill.run(releases=("g19",), start=date(2015, 2, 1), end=date(2015, 1, 1))
    with pytest.raises(BoardBackfillError, match="max_items"):
        backfill.run(
            releases=("g19",),
            start=date(2015, 1, 1),
            end=date(2015, 2, 1),
            max_items=0,
        )
    with pytest.raises(BoardBackfillError, match="non-empty and unique"):
        backfill.run(
            releases=("g19", "g19"),
            start=date(2015, 1, 1),
            end=date(2015, 2, 1),
        )
    with pytest.raises(BoardBackfillError, match="no H8"):
        backfill.run(releases=("h8",), start=date(2015, 1, 1), end=date(2015, 2, 1))


def test_cli_defaults_to_full_resumable_backfill() -> None:
    args = build_parser().parse_args([])

    assert args.release == "all"
    assert args.start == date(2015, 1, 1)
    assert args.on_error == "stop"
    assert args.max_items is None

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--start", "not-a-date"])


def test_cli_runs_full_plan_and_reports_validation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    class FakeTransport:
        def __enter__(self) -> FakeTransport:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class CliClient:
        def discover_release_dates(self, release: BoardRelease) -> tuple[date, ...]:
            assert release in ("g19", "h8")
            return (date(2015, 1, 8),)

    class CliRunner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, **_kwargs: object) -> BoardBackfillSummary:
            return BoardBackfillSummary(
                planned=2,
                attempted=2,
                completed=2,
                already_present=0,
                checkpoint_skipped=0,
                failed=0,
            )

    validation = BoardHistoryValidation(
        total_rows=18,
        release_pages=2,
        pages_by_release={"g19": 1, "h8": 1},
        raw_rows_by_series={"DTCTL.M": 1},
        first_print_rows_by_series={"DTCTL.M": 1},
        earliest_release=date(2015, 1, 8),
        latest_release=date(2015, 1, 8),
    )
    client = CliClient()
    monkeypatch.setattr(board_backfill, "HttpTransport", lambda **_kwargs: FakeTransport())
    monkeypatch.setattr(board_backfill, "FederalReserveBoardClient", lambda _transport: client)
    monkeypatch.setattr(board_backfill, "AppendOnlyParquetStore", lambda path: path)
    monkeypatch.setattr(board_backfill, "BoardHistoryIngestor", lambda *_args: object())
    monkeypatch.setattr(board_backfill, "BoardBackfillRunner", CliRunner)
    monkeypatch.setattr(board_backfill, "validate_board_history", lambda *_args: validation)

    result = board_backfill.main(
        [
            "--release",
            "all",
            "--start",
            "2015-01-01",
            "--end",
            "2015-01-31",
            "--lake-root",
            str(tmp_path / "lake"),
            "--state",
            str(tmp_path / "state.json"),
            "--event-log",
            str(tmp_path / "events.jsonl"),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["backfill"]["completed"] == 2
    assert output["validation"]["release_pages"] == 2
