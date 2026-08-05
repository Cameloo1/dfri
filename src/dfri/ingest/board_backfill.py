"""Resumable Federal Reserve Board dated-release backfill command."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, Literal, Protocol, cast

from dfri.ingest.board import BoardRelease, FederalReserveBoardClient
from dfri.ingest.board_history import (
    BoardHistoryIngestor,
    validate_board_history,
)
from dfri.ingest.http import HttpTransport
from dfri.lake.store import AppendOnlyParquetStore

STATE_SCHEMA_VERSION: Final = 1
type ErrorMode = Literal["stop", "continue"]


class BoardBackfillError(RuntimeError):
    """The Board backfill state or a release attempt failed."""


class BoardReleaseReceipt(Protocol):
    @property
    def source_url(self) -> str: ...

    @property
    def checksum(self) -> str: ...

    @property
    def row_count(self) -> int: ...

    @property
    def already_present(self) -> bool: ...

    @property
    def batch_path(self) -> Path | None: ...

    @property
    def content_hash(self) -> str | None: ...


class BoardReleaseIngestor(Protocol):
    def fetch_and_ingest(
        self, release: BoardRelease, release_date: date
    ) -> BoardReleaseReceipt: ...


@dataclass(frozen=True)
class BoardBackfillSummary:
    planned: int
    attempted: int
    completed: int
    already_present: int
    checkpoint_skipped: int
    failed: int


class BoardBackfillRunner:
    """Run a bounded backfill with atomic checkpoints and append-only event receipts."""

    def __init__(
        self,
        *,
        client: FederalReserveBoardClient,
        ingestor: BoardReleaseIngestor,
        state_path: Path,
        event_log_path: Path,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._ingestor = ingestor
        self._state_path = state_path
        self._event_log_path = event_log_path
        self._now = now or (lambda: datetime.now(UTC))

    def run(
        self,
        *,
        releases: Sequence[BoardRelease],
        start: date,
        end: date,
        max_items: int | None = None,
        error_mode: ErrorMode = "stop",
        recheck_complete: bool = False,
    ) -> BoardBackfillSummary:
        if start > end:
            raise BoardBackfillError("Backfill start date must not follow end date")
        if max_items is not None and max_items < 1:
            raise BoardBackfillError("max_items must be at least one")
        if not releases or len(set(releases)) != len(releases):
            raise BoardBackfillError("Backfill releases must be non-empty and unique")

        state = self._load_state()
        items = _state_items(state)
        plan = self._build_plan(releases, start, end)
        attempted = completed = already_present = checkpoint_skipped = failed = 0
        for release, release_date in plan:
            key = _item_key(release, release_date)
            item = _state_item(state, key)
            if not recheck_complete and _checkpoint_is_live(item):
                checkpoint_skipped += 1
                self._append_event(
                    "CHECKPOINT_SKIPPED", release, release_date, {"batch_path": item["batch_path"]}
                )
                continue
            if max_items is not None and attempted >= max_items:
                break

            attempted += 1
            prior_attempts = item.get("attempts", 0)
            if not isinstance(prior_attempts, int) or isinstance(prior_attempts, bool):
                raise BoardBackfillError(f"Board backfill state item {key!r} has invalid attempts")
            attempts = prior_attempts + 1
            items[key] = {
                "status": "RUNNING",
                "attempts": attempts,
                "last_attempt_at": self._timestamp(),
            }
            self._save_state(state)
            self._append_event("RUNNING", release, release_date, {"attempt": attempts})
            try:
                receipt = self._ingestor.fetch_and_ingest(release, release_date)
            except Exception as exc:
                failed += 1
                safe_error = _safe_error(exc)
                items[key] = {
                    "status": "FAILED",
                    "attempts": attempts,
                    "last_attempt_at": self._timestamp(),
                    "error": safe_error,
                }
                self._save_state(state)
                self._append_event("FAILED", release, release_date, {"error": safe_error})
                if error_mode == "stop":
                    raise BoardBackfillError(
                        f"Board backfill stopped at {release}:{release_date.isoformat()}"
                    ) from exc
                continue

            completed += 1
            already_present += int(receipt.already_present)
            items[key] = _completed_item(receipt, attempts, self._timestamp())
            self._save_state(state)
            self._append_event(
                "ALREADY_PRESENT" if receipt.already_present else "COMPLETED",
                release,
                release_date,
                {
                    "checksum": receipt.checksum,
                    "row_count": receipt.row_count,
                    "content_hash": receipt.content_hash,
                },
            )

        return BoardBackfillSummary(
            planned=len(plan),
            attempted=attempted,
            completed=completed,
            already_present=already_present,
            checkpoint_skipped=checkpoint_skipped,
            failed=failed,
        )

    def _build_plan(
        self, releases: Sequence[BoardRelease], start: date, end: date
    ) -> tuple[tuple[BoardRelease, date], ...]:
        plan: list[tuple[BoardRelease, date]] = []
        for release in releases:
            discovered = self._client.discover_release_dates(release)
            selected = [item for item in discovered if start <= item <= end]
            if not selected:
                raise BoardBackfillError(
                    f"Board index returned no {release.upper()} releases in the requested window"
                )
            plan.extend((release, item) for item in selected)
        return tuple(plan)

    def _load_state(self) -> dict[str, object]:
        if not self._state_path.exists():
            return {"schema_version": STATE_SCHEMA_VERSION, "items": {}}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BoardBackfillError(f"Cannot read backfill state: {self._state_path}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
            raise BoardBackfillError("Board backfill state has an unsupported schema")
        if not isinstance(raw.get("items"), dict):
            raise BoardBackfillError("Board backfill state has invalid items")
        return cast(dict[str, object], raw)

    def _save_state(self, state: dict[str, object]) -> None:
        state["updated_at"] = self._timestamp()
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._state_path.name}.", suffix=".tmp", dir=self._state_path.parent
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            temporary_path.write_text(
                json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            temporary_path.replace(self._state_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _append_event(
        self,
        status: str,
        release: BoardRelease,
        release_date: date,
        details: dict[str, object],
    ) -> None:
        event = {
            "at": self._timestamp(),
            "status": status,
            "release": release,
            "release_date": release_date.isoformat(),
            **details,
        }
        self._event_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._event_log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _timestamp(self) -> str:
        current = self._now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise BoardBackfillError("Backfill clock must return timezone-aware datetimes")
        return current.astimezone(UTC).isoformat()


def _state_item(state: dict[str, object], key: str) -> dict[str, object]:
    items = _state_items(state)
    raw = items.get(key, {})
    if not isinstance(raw, dict):
        raise BoardBackfillError(f"Board backfill state item {key!r} is invalid")
    return cast(dict[str, object], raw)


def _state_items(state: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], state["items"])


def _checkpoint_is_live(item: dict[str, object]) -> bool:
    batch_path = item.get("batch_path")
    return (
        item.get("status") == "COMPLETE"
        and isinstance(batch_path, str)
        and Path(batch_path).is_file()
    )


def _completed_item(
    receipt: BoardReleaseReceipt, attempts: int, completed_at: str
) -> dict[str, object]:
    return {
        "status": "COMPLETE",
        "attempts": attempts,
        "completed_at": completed_at,
        "source_url": receipt.source_url,
        "checksum": receipt.checksum,
        "row_count": receipt.row_count,
        "batch_path": str(receipt.batch_path.resolve()) if receipt.batch_path else None,
        "content_hash": receipt.content_hash,
    }


def _item_key(release: BoardRelease, release_date: date) -> str:
    return f"{release}:{release_date.isoformat()}"


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", choices=("g19", "h8", "all"), default="all")
    parser.add_argument("--start", type=_parse_date, default=date(2015, 1, 1))
    parser.add_argument("--end", type=_parse_date, default=datetime.now(UTC).date())
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--state", type=Path, default=Path(".local/state/board-backfill.json"))
    parser.add_argument(
        "--event-log", type=Path, default=Path(".local/evidence/board-backfill.jsonl")
    )
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--on-error", choices=("stop", "continue"), default="stop")
    parser.add_argument("--recheck-complete", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    releases: tuple[BoardRelease, ...] = (
        ("g19", "h8") if args.release == "all" else (cast(BoardRelease, args.release),)
    )
    with HttpTransport(min_interval_seconds=0.5) as transport:
        client = FederalReserveBoardClient(transport)
        ingestor = BoardHistoryIngestor(AppendOnlyParquetStore(args.lake_root), client)
        summary = BoardBackfillRunner(
            client=client,
            ingestor=ingestor,
            state_path=args.state,
            event_log_path=args.event_log,
        ).run(
            releases=releases,
            start=args.start,
            end=args.end,
            max_items=args.max_items,
            error_mode=args.on_error,
            recheck_complete=args.recheck_complete,
        )
        output: dict[str, object] = {"backfill": asdict(summary)}
        if args.max_items is None and summary.failed == 0 and not args.skip_validation:
            expected_dates = {
                release: tuple(
                    item
                    for item in client.discover_release_dates(release)
                    if args.start <= item <= args.end
                )
                for release in releases
            }
            validation = validate_board_history(
                AppendOnlyParquetStore(args.lake_root), expected_dates
            )
            output["validation"] = asdict(validation)
    print(json.dumps(output, sort_keys=True, default=str))
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
