"""Append-only prediction and first-print grade ledgers for the public scoreboard."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from math import isfinite
from pathlib import Path
from typing import Final, Protocol, cast

from dfri.lake.store import AppendOnlyParquetStore, WriteReceipt
from dfri.nowcast.targets import FirstPrintTarget

PREDICTION_STATUS: Final = "PENDING_FIRST_PRINT"
PREDICTION_ID_PREFIX: Final = "prd_"
PREDICTION_DECIMAL_PLACES: Final = 9
LOCK_POLL_SECONDS: Final = 0.05


class LedgerError(RuntimeError):
    """Prediction or grade state violates the append-only scoreboard contract."""


class ImmutablePredictionError(LedgerError):
    """An existing prediction ID was presented with different content."""


class ImmutableGradeError(LedgerError):
    """An existing grade was presented with different first-print evidence."""


class ImmutablePublicationError(LedgerError):
    """Stored first-publication metadata is inconsistent with prediction state."""


class LedgerBusyError(LedgerError):
    """Another writer currently holds the ledger's atomic write gate."""


class ForecastLike(Protocol):
    @property
    def model_version(self) -> str: ...

    @property
    def target_series(self) -> str: ...

    @property
    def target_period(self) -> date: ...

    @property
    def made_at(self) -> datetime: ...

    @property
    def point(self) -> float: ...

    @property
    def low80(self) -> float: ...

    @property
    def high80(self) -> float: ...

    @property
    def low95(self) -> float: ...

    @property
    def high95(self) -> float: ...

    @property
    def inputs_hash(self) -> str: ...


@dataclass(frozen=True)
class PredictionRecord:
    prediction_id: str
    made_at: datetime
    model_version: str
    inputs_hash: str
    target_series: str
    target_period: date
    point: float
    low80: float
    high80: float
    low95: float
    high95: float
    status: str = PREDICTION_STATUS

    def row(self) -> dict[str, object]:
        return {
            "prediction_id": self.prediction_id,
            "made_at": self.made_at,
            "model_version": self.model_version,
            "inputs_hash": self.inputs_hash,
            "target_series": self.target_series,
            "target_period": self.target_period,
            "point": self.point,
            "low80": self.low80,
            "high80": self.high80,
            "low95": self.low95,
            "high95": self.high95,
            "status": self.status,
        }


@dataclass(frozen=True)
class GradeRecord:
    prediction_id: str
    actual_first_print: float
    vintage_url: str
    abs_error: float
    graded_at: datetime

    def row(self) -> dict[str, object]:
        return {
            "prediction_id": self.prediction_id,
            "actual_first_print": self.actual_first_print,
            "vintage_url": self.vintage_url,
            "abs_error": self.abs_error,
            "graded_at": self.graded_at,
        }


@dataclass(frozen=True)
class PublicationRecord:
    prediction_id: str
    published_at: datetime
    data_vintage: datetime
    methodology_version: str

    def row(self) -> dict[str, object]:
        return {
            "prediction_id": self.prediction_id,
            "published_at": self.published_at,
            "data_vintage": self.data_vintage,
            "methodology_version": self.methodology_version,
        }


@dataclass(frozen=True)
class LedgerReceipt:
    record_id: str
    appended: bool
    storage: WriteReceipt | None


@dataclass(frozen=True)
class GradingResult:
    attempted: int
    appended: int
    already_present: int
    not_matured: int


class PredictionLedger:
    def __init__(self, store: AppendOnlyParquetStore, *, lock_timeout: float = 5.0) -> None:
        self._store = store
        self._lock_timeout = lock_timeout

    def append(self, forecast: ForecastLike) -> LedgerReceipt:
        record = prediction_record(forecast)
        with _exclusive_lock(self._store.root, "predictions", self._lock_timeout):
            existing = {item.prediction_id: item for item in self.read_all()}
            current = existing.get(record.prediction_id)
            if current is not None:
                retry_record = replace(record, made_at=current.made_at)
                if _canonical_prediction_numbers(current) != retry_record:
                    raise ImmutablePredictionError(
                        f"Prediction {record.prediction_id} already exists with different content"
                    )
                return LedgerReceipt(record.prediction_id, appended=False, storage=None)
            receipt = self._store.append("predictions", [record.row()])
            return LedgerReceipt(record.prediction_id, appended=True, storage=receipt)

    def read_all(self) -> tuple[PredictionRecord, ...]:
        frame = self._store.read_table("predictions")
        records = tuple(_prediction_from_row(row) for row in frame.iter_rows(named=True))
        _reject_duplicate_ids(records, "prediction")
        return tuple(sorted(records, key=lambda item: (item.made_at, item.prediction_id)))


class GradeLedger:
    def __init__(self, store: AppendOnlyParquetStore, *, lock_timeout: float = 5.0) -> None:
        self._store = store
        self._lock_timeout = lock_timeout

    def append(self, prediction: PredictionRecord, target: FirstPrintTarget) -> LedgerReceipt:
        record = grade_record(prediction, target)
        with _exclusive_lock(self._store.root, "grades", self._lock_timeout):
            existing = {item.prediction_id: item for item in self.read_all()}
            current = existing.get(record.prediction_id)
            if current is not None:
                if current != record:
                    raise ImmutableGradeError(
                        f"Grade {record.prediction_id} already exists with different evidence"
                    )
                return LedgerReceipt(record.prediction_id, appended=False, storage=None)
            receipt = self._store.append("grades", [record.row()])
            return LedgerReceipt(record.prediction_id, appended=True, storage=receipt)

    def read_all(self) -> tuple[GradeRecord, ...]:
        frame = self._store.read_table("grades")
        records = tuple(_grade_from_row(row) for row in frame.iter_rows(named=True))
        _reject_duplicate_ids(records, "grade")
        return tuple(sorted(records, key=lambda item: (item.graded_at, item.prediction_id)))


class PublicationLedger:
    """Append each prediction's first live-publication metadata exactly once."""

    def __init__(self, store: AppendOnlyParquetStore, *, lock_timeout: float = 5.0) -> None:
        self._store = store
        self._lock_timeout = lock_timeout

    def append(
        self,
        prediction: PredictionRecord,
        *,
        published_at: datetime,
        data_vintage: datetime,
        methodology_version: str,
    ) -> LedgerReceipt:
        receipt = self.append_many(
            (prediction,),
            published_at=published_at,
            data_vintage=data_vintage,
            methodology_version=methodology_version,
        )
        return LedgerReceipt(
            prediction.prediction_id,
            appended=receipt is not None,
            storage=receipt,
        )

    def append_many(
        self,
        predictions: Sequence[PredictionRecord],
        *,
        published_at: datetime,
        data_vintage: datetime,
        methodology_version: str,
    ) -> WriteReceipt | None:
        records = tuple(
            publication_record(
                prediction,
                published_at=published_at,
                data_vintage=data_vintage,
                methodology_version=methodology_version,
            )
            for prediction in predictions
        )
        if len({record.prediction_id for record in records}) != len(records):
            raise LedgerError("Publication batch contains duplicate prediction IDs")
        with _exclusive_lock(self._store.root, "publication-records", self._lock_timeout):
            existing = {item.prediction_id: item for item in self.read_all()}
            missing = [record for record in records if record.prediction_id not in existing]
            if not missing:
                return None
            return self._store.append("publication_records", [record.row() for record in missing])

    def read_all(self) -> tuple[PublicationRecord, ...]:
        frame = self._store.read_table("publication_records")
        records = tuple(_publication_from_row(row) for row in frame.iter_rows(named=True))
        _reject_duplicate_ids(records, "publication")
        return tuple(sorted(records, key=lambda item: (item.published_at, item.prediction_id)))


def prediction_record(forecast: ForecastLike) -> PredictionRecord:
    identity = {
        "model_version": forecast.model_version,
        "inputs_hash": forecast.inputs_hash,
        "target_series": forecast.target_series,
        "target_period": forecast.target_period.isoformat(),
    }
    prediction_id = (
        PREDICTION_ID_PREFIX
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    raw_record = PredictionRecord(
        prediction_id=prediction_id,
        made_at=forecast.made_at,
        model_version=forecast.model_version,
        inputs_hash=forecast.inputs_hash,
        target_series=forecast.target_series,
        target_period=forecast.target_period,
        point=forecast.point,
        low80=forecast.low80,
        high80=forecast.high80,
        low95=forecast.low95,
        high95=forecast.high95,
    )
    _validate_prediction(raw_record)
    record = _canonical_prediction_numbers(raw_record)
    _validate_prediction(record)
    return record


def _canonical_prediction_numbers(record: PredictionRecord) -> PredictionRecord:
    """Remove irrelevant cross-runner BLAS noise at a sub-cent output boundary."""

    return replace(
        record,
        point=round(record.point, PREDICTION_DECIMAL_PLACES),
        low80=round(record.low80, PREDICTION_DECIMAL_PLACES),
        high80=round(record.high80, PREDICTION_DECIMAL_PLACES),
        low95=round(record.low95, PREDICTION_DECIMAL_PLACES),
        high95=round(record.high95, PREDICTION_DECIMAL_PLACES),
    )


def grade_record(prediction: PredictionRecord, target: FirstPrintTarget) -> GradeRecord:
    _validate_prediction(prediction)
    if prediction.target_series != target.target_series:
        raise LedgerError("Prediction and grade target series do not match")
    if prediction.target_period != target.target_period:
        raise LedgerError("Prediction and grade target periods do not match")
    if prediction.made_at >= target.release_at:
        raise LedgerError("Prediction was not made before the first-print release")
    if not isfinite(target.value):
        raise LedgerError("First-print grade value is non-finite")
    record = GradeRecord(
        prediction_id=prediction.prediction_id,
        actual_first_print=target.value,
        vintage_url=target.source_url,
        abs_error=abs(prediction.point - target.value),
        graded_at=target.release_at,
    )
    _validate_grade(record)
    return record


def publication_record(
    prediction: PredictionRecord,
    *,
    published_at: datetime,
    data_vintage: datetime,
    methodology_version: str,
) -> PublicationRecord:
    _validate_prediction(prediction)
    record = PublicationRecord(
        prediction_id=prediction.prediction_id,
        published_at=published_at,
        data_vintage=data_vintage,
        methodology_version=methodology_version,
    )
    _validate_publication(record, prediction)
    return record


def grade_matured_predictions(
    prediction_ledger: PredictionLedger,
    grade_ledger: GradeLedger,
    targets: Sequence[FirstPrintTarget],
    *,
    as_of: datetime,
) -> GradingResult:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise LedgerError("Grading as-of timestamp must be timezone-aware")
    target_by_key: dict[tuple[str, date], FirstPrintTarget] = {}
    for target in targets:
        key = (target.target_series, target.target_period)
        if key in target_by_key:
            raise LedgerError("Grading target set contains duplicate first prints")
        target_by_key[key] = target
    existing = {item.prediction_id for item in grade_ledger.read_all()}
    attempted = appended = already_present = not_matured = 0
    for prediction in prediction_ledger.read_all():
        if prediction.prediction_id in existing:
            continue
        matured_target = target_by_key.get((prediction.target_series, prediction.target_period))
        if matured_target is None or matured_target.release_at > as_of:
            not_matured += 1
            continue
        attempted += 1
        receipt = grade_ledger.append(prediction, matured_target)
        if receipt.appended:
            appended += 1
        else:
            already_present += 1
    return GradingResult(attempted, appended, already_present, not_matured)


def verify_regrade_integrity(
    prediction_ledger: PredictionLedger,
    grade_ledger: GradeLedger,
    targets: Sequence[FirstPrintTarget],
) -> None:
    predictions = {item.prediction_id: item for item in prediction_ledger.read_all()}
    targets_by_key = {(item.target_series, item.target_period): item for item in targets}
    for stored in grade_ledger.read_all():
        prediction = predictions.get(stored.prediction_id)
        if prediction is None:
            raise LedgerError(f"Stored grade has no prediction: {stored.prediction_id}")
        target = targets_by_key.get((prediction.target_series, prediction.target_period))
        if target is None:
            raise LedgerError(f"Stored grade has no first-print target: {stored.prediction_id}")
        expected = grade_record(prediction, target)
        if expected != stored:
            raise ImmutableGradeError(
                f"Stored grade does not match raw first print: {stored.prediction_id}"
            )


def _validate_prediction(record: PredictionRecord) -> None:
    if not record.prediction_id.startswith(PREDICTION_ID_PREFIX) or not _is_sha256(
        record.prediction_id.removeprefix(PREDICTION_ID_PREFIX)
    ):
        raise LedgerError("Prediction ID is not a stable SHA-256 identity")
    if record.made_at.tzinfo is None or record.made_at.utcoffset() is None:
        raise LedgerError("Prediction timestamp must be timezone-aware")
    if record.target_period != _month_end(record.target_period):
        raise LedgerError("Prediction target period must be a month end")
    if not record.model_version or not record.target_series:
        raise LedgerError("Prediction model and target series are required")
    if not _is_sha256(record.inputs_hash):
        raise LedgerError("Prediction input hash is not lowercase SHA-256")
    values = (record.point, record.low80, record.high80, record.low95, record.high95)
    if not all(isfinite(item) for item in values):
        raise LedgerError("Prediction contains a non-finite value")
    if not record.low95 <= record.low80 <= record.point <= record.high80 <= record.high95:
        raise LedgerError("Prediction intervals are not properly nested")
    if record.status != PREDICTION_STATUS:
        raise LedgerError("Prediction status is not the immutable initial status")


def _validate_grade(record: GradeRecord) -> None:
    if not record.prediction_id.startswith(PREDICTION_ID_PREFIX):
        raise LedgerError("Grade prediction ID is invalid")
    if not all(isfinite(item) for item in (record.actual_first_print, record.abs_error)):
        raise LedgerError("Grade contains a non-finite value")
    if record.abs_error < 0:
        raise LedgerError("Grade absolute error cannot be negative")
    if not record.vintage_url.startswith("https://www.federalreserve.gov/releases/g19/"):
        raise LedgerError("Grade vintage is not a Board G.19 release URL")
    if record.graded_at.tzinfo is None or record.graded_at.utcoffset() is None:
        raise LedgerError("Grade timestamp must be timezone-aware")


def _validate_publication(record: PublicationRecord, prediction: PredictionRecord | None) -> None:
    if not record.prediction_id.startswith(PREDICTION_ID_PREFIX):
        raise ImmutablePublicationError("Publication prediction ID is invalid")
    for value, label in (
        (record.published_at, "Publication timestamp"),
        (record.data_vintage, "Publication data vintage"),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ImmutablePublicationError(f"{label} must be timezone-aware")
    if record.data_vintage > record.published_at:
        raise ImmutablePublicationError("Publication data vintage is after publication")
    if prediction is not None and record.published_at < prediction.made_at:
        raise ImmutablePublicationError("Publication predates its prediction")
    if not record.methodology_version:
        raise ImmutablePublicationError("Publication methodology version is required")


def _prediction_from_row(row: dict[str, object]) -> PredictionRecord:
    record = PredictionRecord(
        prediction_id=str(row["prediction_id"]),
        made_at=cast(datetime, row["made_at"]),
        model_version=str(row["model_version"]),
        inputs_hash=str(row["inputs_hash"]),
        target_series=str(row["target_series"]),
        target_period=cast(date, row["target_period"]),
        point=float(cast(float, row["point"])),
        low80=float(cast(float, row["low80"])),
        high80=float(cast(float, row["high80"])),
        low95=float(cast(float, row["low95"])),
        high95=float(cast(float, row["high95"])),
        status=str(row["status"]),
    )
    _validate_prediction(record)
    return record


def _grade_from_row(row: dict[str, object]) -> GradeRecord:
    record = GradeRecord(
        prediction_id=str(row["prediction_id"]),
        actual_first_print=float(cast(float, row["actual_first_print"])),
        vintage_url=str(row["vintage_url"]),
        abs_error=float(cast(float, row["abs_error"])),
        graded_at=cast(datetime, row["graded_at"]),
    )
    _validate_grade(record)
    return record


def _publication_from_row(row: dict[str, object]) -> PublicationRecord:
    record = PublicationRecord(
        prediction_id=str(row["prediction_id"]),
        published_at=cast(datetime, row["published_at"]),
        data_vintage=cast(datetime, row["data_vintage"]),
        methodology_version=str(row["methodology_version"]),
    )
    _validate_publication(record, None)
    return record


def _reject_duplicate_ids(
    records: Sequence[PredictionRecord | GradeRecord | PublicationRecord], kind: str
) -> None:
    ids = [item.prediction_id for item in records]
    if len(ids) != len(set(ids)):
        raise LedgerError(f"Stored {kind} ledger contains duplicate IDs")


@contextmanager
def _exclusive_lock(root: Path, name: str, timeout: float) -> Iterator[None]:
    if not isfinite(timeout) or timeout < 0:
        raise LedgerError("Ledger lock timeout must be finite and non-negative")
    lock_dir = root / "_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise LedgerBusyError(
                    f"Ledger {name} is busy; inspect and recover {lock_path} before retry"
                ) from None
            time.sleep(LOCK_POLL_SECONDS)
    try:
        os.write(descriptor, token.encode("ascii"))
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if lock_path.read_text(encoding="ascii") == token:
                lock_path.unlink()
        except FileNotFoundError:
            pass


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _month_end(period: date) -> date:
    year = period.year + int(period.month == 12)
    month = 1 if period.month == 12 else period.month + 1
    return date(year, month, 1) - date.resolution
