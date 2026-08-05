from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from dfri.lake.store import AppendOnlyParquetStore
from dfri.nowcast.bridge import BridgeForecast
from dfri.nowcast.targets import FirstPrintTarget
from dfri.publish.ledger import (
    GradeLedger,
    ImmutableGradeError,
    ImmutablePredictionError,
    ImmutablePublicationError,
    LedgerBusyError,
    LedgerError,
    PredictionLedger,
    PublicationLedger,
    grade_matured_predictions,
    prediction_record,
    verify_regrade_integrity,
)


def forecast(**changes: object) -> BridgeForecast:
    values: dict[str, object] = {
        "model_version": "bridge-ridge-v1",
        "target_series": "DELTA_DTCTLR.M",
        "target_period": date(2026, 6, 30),
        "made_at": datetime(2026, 6, 12, 21, 15, tzinfo=UTC),
        "point": 10_000.0,
        "low80": 8_000.0,
        "high80": 12_000.0,
        "low95": 6_000.0,
        "high95": 14_000.0,
        "training_observations": 137,
        "inputs_hash": "a" * 64,
    }
    values.update(changes)
    return BridgeForecast(**values)  # type: ignore[arg-type]


def target(**changes: object) -> FirstPrintTarget:
    release_at = datetime(2026, 8, 7, 19, 0, tzinfo=UTC)
    values: dict[str, object] = {
        "target_series": "DELTA_DTCTLR.M",
        "level_series": "DTCTLR.M",
        "target_period": date(2026, 6, 30),
        "value": 10_500.0,
        "unit": "Millions of U.S. Dollars",
        "release_at": release_at,
        "vintage_date": release_at.date(),
        "source_url": "https://www.federalreserve.gov/releases/g19/20260807/",
        "checksum": "b" * 64,
    }
    values.update(changes)
    return FirstPrintTarget(**values)  # type: ignore[arg-type]


def ledgers(tmp_path: Path) -> tuple[PredictionLedger, GradeLedger, AppendOnlyParquetStore]:
    store = AppendOnlyParquetStore(tmp_path)
    return PredictionLedger(store), GradeLedger(store), store


def test_prediction_id_is_stable_and_append_is_idempotent(tmp_path: Path) -> None:
    predictions, _grades, store = ledgers(tmp_path)

    first = predictions.append(forecast())
    second = predictions.append(forecast())

    assert first.record_id == second.record_id
    assert first.record_id.startswith("prd_")
    assert first.appended is True
    assert first.storage is not None
    assert second.appended is False
    assert second.storage is None
    assert store.read_table("predictions").height == 1
    assert predictions.read_all()[0].inputs_hash == "a" * 64

    later_retry = predictions.append(forecast(made_at=datetime(2026, 6, 12, 22, 15, tzinfo=UTC)))
    assert later_retry.record_id == first.record_id
    assert later_retry.appended is False
    assert predictions.read_all()[0].made_at == datetime(2026, 6, 12, 21, 15, tzinfo=UTC)


def test_prediction_retry_canonicalizes_cross_runner_numeric_noise(tmp_path: Path) -> None:
    predictions, _grades, store = ledgers(tmp_path)
    source = forecast(point=10_000.1234567894)
    canonical = prediction_record(source)
    legacy = replace(
        canonical,
        point=canonical.point + 2.0e-11,
        low80=canonical.low80 + 2.0e-11,
        high80=canonical.high80 + 2.0e-11,
        low95=canonical.low95 + 2.0e-11,
        high95=canonical.high95 + 2.0e-11,
    )
    store.append("predictions", [legacy.row()])

    retry = predictions.append(
        forecast(point=10_000.1234567894, made_at=datetime(2026, 6, 12, 22, 15, tzinfo=UTC))
    )

    assert canonical.point == round(source.point, 9)
    assert retry.appended is False
    assert predictions.read_all()[0] == legacy


def test_attempted_prediction_edit_is_rejected(tmp_path: Path) -> None:
    predictions, _grades, _store = ledgers(tmp_path)
    predictions.append(forecast())

    with pytest.raises(ImmutablePredictionError, match="different content"):
        predictions.append(forecast(point=10_001.0))


def test_prediction_contract_rejects_invalid_hash_intervals_and_period() -> None:
    with pytest.raises(LedgerError, match="hash"):
        prediction_record(forecast(inputs_hash="bad"))
    with pytest.raises(LedgerError, match="nested"):
        prediction_record(forecast(low80=11_000.0))
    with pytest.raises(LedgerError, match="month end"):
        prediction_record(forecast(target_period=date(2026, 6, 1)))
    with pytest.raises(LedgerError, match="timezone-aware"):
        prediction_record(forecast(made_at=datetime(2026, 6, 12, tzinfo=UTC).replace(tzinfo=None)))


def test_matured_prediction_grades_once_against_first_print(tmp_path: Path) -> None:
    predictions, grades, _store = ledgers(tmp_path)
    receipt = predictions.append(forecast())
    first_print = target()

    before = grade_matured_predictions(
        predictions,
        grades,
        (first_print,),
        as_of=first_print.release_at - timedelta(seconds=1),
    )
    after = grade_matured_predictions(
        predictions,
        grades,
        (first_print,),
        as_of=first_print.release_at,
    )
    repeat = grade_matured_predictions(
        predictions,
        grades,
        (first_print,),
        as_of=first_print.release_at + timedelta(days=1),
    )

    assert before.not_matured == 1
    assert after.appended == 1
    assert repeat == replace(repeat, attempted=0, appended=0, already_present=0, not_matured=0)
    grade = grades.read_all()[0]
    assert grade.prediction_id == receipt.record_id
    assert grade.actual_first_print == 10_500.0
    assert grade.abs_error == 500.0
    assert grade.graded_at == first_print.release_at
    verify_regrade_integrity(predictions, grades, (first_print,))


def test_grade_edit_and_invalid_grade_boundaries_are_rejected(tmp_path: Path) -> None:
    predictions, grades, _store = ledgers(tmp_path)
    predictions.append(forecast())
    prediction = predictions.read_all()[0]
    grades.append(prediction, target())

    with pytest.raises(ImmutableGradeError, match="different evidence"):
        grades.append(prediction, target(value=10_600.0))
    with pytest.raises(LedgerError, match="series"):
        grades.append(prediction, target(target_series="DELTA_DTCTLN.M"))
    with pytest.raises(LedgerError, match="periods"):
        grades.append(prediction, target(target_period=date(2026, 7, 31)))
    with pytest.raises(LedgerError, match="not made before"):
        grades.append(
            replace(prediction, made_at=target().release_at),
            target(),
        )


def test_first_publication_metadata_is_append_only(tmp_path: Path) -> None:
    predictions, _grades, store = ledgers(tmp_path)
    predictions.append(forecast())
    prediction = predictions.read_all()[0]
    publications = PublicationLedger(store)
    first_at = datetime(2026, 6, 12, 22, 0, tzinfo=UTC)
    first_vintage = datetime(2026, 6, 12, 21, 15, tzinfo=UTC)

    first = publications.append(
        prediction,
        published_at=first_at,
        data_vintage=first_vintage,
        methodology_version="1.0.0",
    )
    retry = publications.append(
        prediction,
        published_at=first_at + timedelta(days=1),
        data_vintage=first_vintage + timedelta(days=1),
        methodology_version="2.0.0",
    )

    assert first.appended is True
    assert retry.appended is False
    assert publications.read_all()[0].published_at == first_at
    assert publications.read_all()[0].data_vintage == first_vintage
    assert publications.read_all()[0].methodology_version == "1.0.0"
    with pytest.raises(ImmutablePublicationError, match="predates"):
        publications.append(
            prediction,
            published_at=prediction.made_at - timedelta(seconds=1),
            data_vintage=prediction.made_at - timedelta(hours=1),
            methodology_version="1.0.0",
        )


def test_regrade_integrity_detects_missing_prediction_or_target(tmp_path: Path) -> None:
    predictions, grades, store = ledgers(tmp_path)
    predictions.append(forecast())
    prediction = predictions.read_all()[0]
    grades.append(prediction, target())

    with pytest.raises(LedgerError, match="no first-print"):
        verify_regrade_integrity(predictions, grades, ())
    orphan_store = AppendOnlyParquetStore(tmp_path / "orphan")
    orphan_store.append("grades", [grades.read_all()[0].row()])
    with pytest.raises(LedgerError, match="no prediction"):
        verify_regrade_integrity(
            PredictionLedger(orphan_store), GradeLedger(orphan_store), (target(),)
        )
    assert store.read_table("grades").height == 1


def test_duplicate_targets_naive_as_of_and_busy_lock_are_explicit(tmp_path: Path) -> None:
    predictions, grades, store = ledgers(tmp_path)
    predictions.append(forecast())
    with pytest.raises(LedgerError, match="timezone-aware"):
        grade_matured_predictions(
            predictions,
            grades,
            (target(),),
            as_of=datetime(2026, 8, 7, tzinfo=UTC).replace(tzinfo=None),
        )
    with pytest.raises(LedgerError, match="duplicate"):
        grade_matured_predictions(
            predictions, grades, (target(), target()), as_of=target().release_at
        )

    lock = store.root / "_locks" / "predictions.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held", encoding="ascii")
    busy = PredictionLedger(store, lock_timeout=0)
    with pytest.raises(LedgerBusyError, match="inspect and recover"):
        busy.append(forecast(made_at=datetime(2026, 6, 19, 21, 15, tzinfo=UTC)))
