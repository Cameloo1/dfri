"""Create and restore integrity-checked M2 runtime-state bundles."""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Final, cast

BUNDLE_SCHEMA_VERSION: Final = 1
MANIFEST_NAME: Final = "MANIFEST.json"
ALLOWED_PATTERNS: Final = (
    "lake/raw/raw_observations/batch-*.parquet",
    "lake/curated/predictions/batch-*.parquet",
    "lake/curated/grades/batch-*.parquet",
    "lake/curated/publication_records/batch-*.parquet",
    "lake/curated/attribution_refreshes/batch-*.parquet",
    "state/board-backfill.json",
    "state/board-targets-v1.json",
    "evidence/scoreboard_jobs/*.json",
    "evidence/job_status/*.json",
    "evidence/quarterly_refresh/*.json",
)


class StateBundleError(RuntimeError):
    """Runtime state cannot be transferred without violating its allowlist or integrity."""


@dataclass(frozen=True)
class StateBundleReceipt:
    bundle: Path
    file_count: int
    payload_bytes: int
    manifest_hash: str


def pack_state_bundle(root: Path, bundle: Path) -> StateBundleReceipt:
    """Pack only public-source M2 state into deterministic, checksummed tar-gzip bytes."""

    if not root.is_dir():
        raise StateBundleError(f"State root does not exist: {root}")
    paths = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and _is_allowed(path.relative_to(root).as_posix())
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not paths:
        raise StateBundleError("State root contains no allowlisted M2 files")
    files: list[tuple[str, bytes]] = []
    manifest_files: list[dict[str, object]] = []
    for path in paths:
        if path.is_symlink():
            raise StateBundleError(f"State bundle refuses symlink: {path}")
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        files.append((relative, content))
        manifest_files.append(
            {
                "path": relative,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "files": manifest_files,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    bundle.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{bundle.name}.", suffix=".tmp", dir=bundle.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(
                    mode="w", fileobj=compressed, format=tarfile.USTAR_FORMAT
                ) as archive:
                    _add_bytes(archive, MANIFEST_NAME, manifest_bytes)
                    for relative, content in files:
                        _add_bytes(archive, relative, content)
        temporary.replace(bundle)
    finally:
        temporary.unlink(missing_ok=True)
    return StateBundleReceipt(
        bundle=bundle,
        file_count=len(files),
        payload_bytes=sum(len(content) for _, content in files),
        manifest_hash=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def unpack_state_bundle(bundle: Path, root: Path) -> StateBundleReceipt:
    """Verify every member before atomically restoring into an empty state root."""

    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise StateBundleError(f"Refusing to restore over non-empty state root: {root}")
    try:
        with tarfile.open(bundle, mode="r:gz") as archive:
            members = archive.getmembers()
            by_name = {member.name: member for member in members}
            if len(by_name) != len(members):
                raise StateBundleError("State bundle contains duplicate member names")
            manifest_member = by_name.pop(MANIFEST_NAME, None)
            if manifest_member is None or not manifest_member.isfile():
                raise StateBundleError("State bundle manifest is missing")
            manifest_bytes = _member_bytes(archive, manifest_member)
            manifest = _parse_manifest(manifest_bytes)
            expected = cast(list[dict[str, object]], manifest["files"])
            expected_names = [cast(str, item["path"]) for item in expected]
            if expected_names != sorted(expected_names) or len(expected_names) != len(
                set(expected_names)
            ):
                raise StateBundleError("State bundle manifest paths are not unique and sorted")
            if set(by_name) != set(expected_names):
                raise StateBundleError("State bundle members do not match its manifest")
            verified: list[tuple[str, bytes]] = []
            for item in expected:
                relative = cast(str, item["path"])
                if not _is_allowed(relative):
                    raise StateBundleError(f"State bundle path is not allowlisted: {relative}")
                member = by_name[relative]
                if not member.isfile():
                    raise StateBundleError(f"State bundle member is not a regular file: {relative}")
                content = _member_bytes(archive, member)
                if len(content) != item["bytes"]:
                    raise StateBundleError(f"State bundle size mismatch: {relative}")
                if hashlib.sha256(content).hexdigest() != item["sha256"]:
                    raise StateBundleError(f"State bundle checksum mismatch: {relative}")
                verified.append((relative, content))
    except (EOFError, OSError, tarfile.TarError) as exc:
        if isinstance(exc, StateBundleError):
            raise
        raise StateBundleError(f"Cannot read state bundle: {bundle}") from exc

    staging = root.with_name(f".{root.name}.restore-{uuid.uuid4().hex}")
    try:
        for relative, content in verified:
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        if root.exists():
            root.rmdir()
        staging.replace(root)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return StateBundleReceipt(
        bundle=bundle,
        file_count=len(verified),
        payload_bytes=sum(len(content) for _, content in verified),
        manifest_hash=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def _parse_manifest(content: bytes) -> dict[str, object]:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateBundleError("State bundle manifest is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise StateBundleError("State bundle schema version is unsupported")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise StateBundleError("State bundle manifest has no files")
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "bytes", "sha256"}
            or not isinstance(item["path"], str)
            or not isinstance(item["bytes"], int)
            or isinstance(item["bytes"], bool)
            or item["bytes"] < 0
            or not isinstance(item["sha256"], str)
            or len(item["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in item["sha256"])
        ):
            raise StateBundleError("State bundle manifest file entry is invalid")
    return cast(dict[str, object], payload)


def _member_bytes(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise StateBundleError(f"State bundle member cannot be read: {member.name}")
    return extracted.read()


def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    archive.addfile(info, io.BytesIO(content))


def _is_allowed(relative: str) -> bool:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        return False
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in ALLOWED_PATTERNS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    pack = subparsers.add_parser("pack")
    pack.add_argument("--root", type=Path, default=Path(".local"))
    pack.add_argument("--bundle", type=Path, default=Path(".local/runtime-state.tar.gz"))
    unpack = subparsers.add_parser("unpack")
    unpack.add_argument("--root", type=Path, default=Path(".local"))
    unpack.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    receipt = (
        pack_state_bundle(args.root, args.bundle)
        if args.command == "pack"
        else unpack_state_bundle(args.bundle, args.root)
    )
    print(json.dumps({**asdict(receipt), "bundle": str(receipt.bundle)}, sort_keys=True))


if __name__ == "__main__":
    main()
