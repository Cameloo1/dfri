"""Content-addressed, append-only Parquet storage with atomic writes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from dfri.lake.schemas import schema_for, table_from_rows


class AppendOnlyViolationError(RuntimeError):
    """Raised when an operation would overwrite append-only state."""


@dataclass(frozen=True)
class WriteReceipt:
    table_name: str
    path: Path
    content_hash: str
    row_count: int
    already_present: bool


def _json_value(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def canonical_rows(rows: Sequence[Mapping[str, object]]) -> bytes:
    """Return a stable encoding used for batch identity, independent of row order."""

    normalized = [{key: _json_value(value) for key, value in sorted(row.items())} for row in rows]
    normalized.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def stable_table(table: pa.Table) -> pa.Table:
    """Sort a table by every column that Arrow can order."""

    if table.num_rows < 2:
        return table
    sortable = [
        (field.name, "ascending") for field in table.schema if not pa.types.is_list(field.type)
    ]
    indices = pa.compute.sort_indices(table, sort_keys=sortable)
    return pa.compute.take(table, indices)


def write_deterministic_parquet(path: Path, table: pa.Table) -> None:
    """Atomically write stable Parquet bytes without wall-clock metadata."""

    path.parent.mkdir(parents=True, exist_ok=True)
    stable = stable_table(table).replace_schema_metadata(None)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        pq.write_table(
            stable,
            temporary_path,
            compression="zstd",
            compression_level=9,
            use_dictionary=False,
            write_statistics=True,
            data_page_version="1.0",
            version="2.6",
        )
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


class AppendOnlyParquetStore:
    """Store immutable, content-addressed batches beneath one lake layer."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def append(self, table_name: str, rows: Sequence[Mapping[str, object]]) -> WriteReceipt:
        table = table_from_rows(table_name, rows)
        content_hash = hashlib.sha256(canonical_rows(rows)).hexdigest()
        destination = self.root / table_name / f"batch-{content_hash}.parquet"
        if destination.exists():
            existing = pq.read_table(destination, schema=schema_for(table_name))
            if existing.num_rows != table.num_rows:
                raise AppendOnlyViolationError(
                    f"Content-address collision for {table_name}: {destination.name}"
                )
            return WriteReceipt(
                table_name, destination, content_hash, table.num_rows, already_present=True
            )
        write_deterministic_parquet(destination, table)
        return WriteReceipt(
            table_name, destination, content_hash, table.num_rows, already_present=False
        )

    def read_table(self, table_name: str) -> pl.DataFrame:
        schema_for(table_name)
        paths = sorted((self.root / table_name).glob("batch-*.parquet"))
        if not paths:
            empty = pl.from_arrow(pa.Table.from_pylist([], schema=schema_for(table_name)))
            if not isinstance(empty, pl.DataFrame):
                raise TypeError("Arrow table unexpectedly converted to a Polars series")
            return empty
        frames = [pl.read_parquet(path) for path in paths]
        return pl.concat(frames, how="vertical")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
