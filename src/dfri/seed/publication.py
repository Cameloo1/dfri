"""Build a deterministic public-site verification snapshot from real published rows."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Final, cast

from dfri.lake.store import AppendOnlyParquetStore
from dfri.nowcast.bridge import BridgeForecast
from dfri.publish.ledger import PredictionLedger
from dfri.publish.quality import check_site, write_receipt
from dfri.publish.site import PublishReceipt, publish_scoreboard

SNAPSHOT_PATH: Final = Path(__file__).with_name("public_prediction_snapshot_v1.json")


class SeedPublicationError(RuntimeError):
    """The frozen public publication snapshot is invalid or non-deterministic."""


def publish_seed_snapshot(
    output: Path,
    *,
    evidence: Path | None = None,
    verify_determinism: bool = True,
    project_root: Path | None = None,
) -> PublishReceipt:
    """Build a disposable-ledger publication and optionally replay it byte for byte."""

    root = project_root or Path(__file__).resolve().parents[3]
    payload = _load_snapshot(SNAPSHOT_PATH)
    output.parent.mkdir(parents=True, exist_ok=True)
    work_parent = root / ".local" / "publish-work"
    work_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dfri-publish-", dir=work_parent) as temporary:
        working = Path(temporary)
        receipt = _build(payload, working / "first-ledger", output, root)
        if verify_determinism:
            replay = working / "replay"
            _build(payload, working / "second-ledger", replay, root)
            if _files(output) != _files(replay):
                raise SeedPublicationError("Frozen publication replay was not byte-identical")
        quality = check_site(output)
        if evidence is not None:
            write_receipt(evidence.with_name("m4-site-quality.json"), quality)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text(
                json.dumps(
                    {
                        "publication": {
                            **asdict(receipt),
                            "output_root": str(receipt.output_root),
                        },
                        "quality": asdict(quality),
                        "replay_byte_identical": verify_determinism,
                        "snapshot_id": payload["snapshot_id"],
                        "source_url": payload["source_url"],
                        "status": "PASS",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
    return receipt


def _build(
    payload: Mapping[str, object], ledger_root: Path, output: Path, project_root: Path
) -> PublishReceipt:
    store = AppendOnlyParquetStore(ledger_root)
    predictions = PredictionLedger(store)
    rows = cast(list[object], payload["rows"])
    for raw in rows:
        if not isinstance(raw, dict):
            raise SeedPublicationError("Frozen prediction row must be an object")
        row = cast(Mapping[str, object], raw)
        forecast = BridgeForecast(
            model_version=_text(row, "model_version"),
            target_series=_text(row, "target_series"),
            target_period=date.fromisoformat(_text(row, "target_period")),
            made_at=datetime.fromisoformat(_text(row, "made_at")),
            point=_number(row, "point"),
            low80=_number(row, "low80"),
            high80=_number(row, "high80"),
            low95=_number(row, "low95"),
            high95=_number(row, "high95"),
            training_observations=0,
            inputs_hash=_text(row, "inputs_hash"),
        )
        receipt = predictions.append(forecast)
        if receipt.record_id != _text(row, "prediction_id"):
            raise SeedPublicationError("Frozen prediction identity does not match its content")
    return publish_scoreboard(
        store,
        output,
        published_at=datetime.fromisoformat(cast(str, payload["published_at"])),
        data_vintage=datetime.fromisoformat(cast(str, payload["data_vintage"])),
        publication_mode="preview",
        project_root=project_root,
    )


def _load_snapshot(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedPublicationError(f"Unable to load frozen publication snapshot: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "v1":
        raise SeedPublicationError("Frozen publication snapshot schema must be v1")
    required = {
        "schema_version",
        "snapshot_id",
        "source_url",
        "captured_at",
        "published_at",
        "data_vintage",
        "rows",
    }
    if set(payload) != required:
        raise SeedPublicationError("Frozen publication snapshot fields do not match v1")
    if not isinstance(payload["rows"], list) or not payload["rows"]:
        raise SeedPublicationError("Frozen publication snapshot requires prediction rows")
    for timestamp in ("captured_at", "published_at", "data_vintage"):
        parsed = datetime.fromisoformat(_text(payload, timestamp))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise SeedPublicationError(f"Frozen snapshot {timestamp} must include a timezone")
    return cast(dict[str, object], payload)


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise SeedPublicationError(f"Frozen snapshot {key} must be a non-empty string")
    return value


def _number(row: Mapping[str, object], key: str) -> float:
    value = row.get(key)
    if not isinstance(value, int | float):
        raise SeedPublicationError(f"Frozen snapshot {key} must be numeric")
    return float(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("published/public"))
    parser.add_argument(
        "--evidence", type=Path, default=Path(".local/evidence/m4-publication.json")
    )
    parser.add_argument("--no-determinism-check", action="store_true")
    args = parser.parse_args(argv)
    receipt = publish_seed_snapshot(
        args.output,
        evidence=args.evidence,
        verify_determinism=not args.no_determinism_check,
    )
    print(
        json.dumps(
            {**asdict(receipt), "output_root": str(receipt.output_root), "status": "PASS"},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
