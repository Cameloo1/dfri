"""Build and verify a deterministic, allowlisted archive of the public ledger."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Final, cast

from dfri.ops.repository_ledger import verify_repository_ledgers

ARCHIVE_SCHEMA_VERSION: Final = 1
MANIFEST_NAME: Final = "ARCHIVE_MANIFEST.json"
STATIC_FILES: Final = (
    "CITATION.cff",
    "LICENSE",
    ".zenodo.json",
    "src/dfri/publish/archive_registry_v1.json",
    "src/dfri/publish/changelog_v1.json",
)


class ArchiveError(RuntimeError):
    """The offsite archive candidate is incomplete, mutable, or unsafe to restore."""


@dataclass(frozen=True)
class ArchiveReceipt:
    archive: Path
    file_count: int
    payload_bytes: int
    manifest_hash: str
    ledger_manifest_hash: str


def create_archive(repository_root: Path, archive: Path) -> ArchiveReceipt:
    """Create a deterministic tar.gz containing only the public immutable record."""

    repository_root = repository_root.resolve()
    ledger_receipt = verify_repository_ledgers(repository_root / "state" / "ledgers")
    paths = sorted(
        [
            *(
                path
                for path in (repository_root / "state" / "ledgers").rglob("*")
                if path.is_file()
            ),
            *(repository_root / relative for relative in STATIC_FILES),
        ],
        key=lambda path: path.relative_to(repository_root).as_posix(),
    )
    missing = [path for path in paths if not path.is_file() or path.is_symlink()]
    if missing:
        raise ArchiveError(f"Archive source is missing or unsafe: {missing[0]}")
    payloads = [(path.relative_to(repository_root).as_posix(), path.read_bytes()) for path in paths]
    manifest = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "ledger_manifest_hash": ledger_receipt.manifest_hash,
        "files": [
            {
                "path": relative,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for relative, content in payloads
        ],
    }
    manifest_bytes = _canonical_json(manifest)
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_name(f".{archive.name}.tmp")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(mode="w", fileobj=compressed, format=tarfile.PAX_FORMAT) as tar:
                    _add_member(tar, MANIFEST_NAME, manifest_bytes)
                    for relative, content in payloads:
                        _add_member(tar, relative, content)
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return verify_archive(archive)


def verify_archive(archive: Path) -> ArchiveReceipt:
    """Verify archive members, hashes, canonical manifest, and ledger semantics."""

    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if not members or members[0].name != MANIFEST_NAME:
                raise ArchiveError("Archive manifest must be the first member")
            if any(not member.isfile() or not _safe_name(member.name) for member in members):
                raise ArchiveError("Archive contains a non-file or unsafe member")
            names = [member.name for member in members]
            if len(names) != len(set(names)):
                raise ArchiveError("Archive contains duplicate member paths")
            manifest_bytes = _member_bytes(tar, members[0])
            try:
                manifest = json.loads(manifest_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArchiveError("Archive manifest is not valid JSON") from exc
            _validate_manifest(manifest, manifest_bytes)
            entries = cast(list[dict[str, object]], manifest["files"])
            expected_names = [MANIFEST_NAME, *(cast(str, item["path"]) for item in entries)]
            if names != expected_names:
                raise ArchiveError("Archive member order does not match its manifest")
            payload_bytes = 0
            with tempfile.TemporaryDirectory(prefix="dfri-archive-verify-") as temporary:
                restored = Path(temporary)
                for member, entry in zip(members[1:], entries, strict=True):
                    content = _member_bytes(tar, member)
                    payload_bytes += len(content)
                    if (
                        len(content) != entry["bytes"]
                        or hashlib.sha256(content).hexdigest() != entry["sha256"]
                    ):
                        raise ArchiveError(f"Archive member checksum mismatch: {member.name}")
                    destination = restored / Path(member.name)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content)
                ledger = verify_repository_ledgers(restored / "state" / "ledgers")
                if ledger.manifest_hash != manifest["ledger_manifest_hash"]:
                    raise ArchiveError("Restored ledger manifest hash does not match archive")
    except (EOFError, OSError, tarfile.TarError) as exc:
        raise ArchiveError(f"Cannot read archive: {archive}") from exc
    return ArchiveReceipt(
        archive=archive,
        file_count=len(cast(list[object], manifest["files"])),
        payload_bytes=payload_bytes,
        manifest_hash=hashlib.sha256(manifest_bytes).hexdigest(),
        ledger_manifest_hash=cast(str, manifest["ledger_manifest_hash"]),
    )


def prove_round_trip(repository_root: Path, archive: Path) -> dict[str, object]:
    """Create twice, prove deterministic bytes, then verify a clean restore."""

    first = create_archive(repository_root, archive)
    with tempfile.TemporaryDirectory(prefix="dfri-archive-proof-") as temporary:
        second_path = Path(temporary) / archive.name
        second = create_archive(repository_root, second_path)
        if archive.read_bytes() != second_path.read_bytes():
            raise ArchiveError("Repeated archive creation is not byte-identical")
    return {
        **asdict(first),
        "archive": str(first.archive),
        "deterministic_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "second_manifest_hash": second.manifest_hash,
        "status": "PASS",
    }


def _validate_manifest(payload: object, raw: bytes) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        raise ArchiveError("Archive manifest schema_version must be 1")
    if set(payload) != {"schema_version", "ledger_manifest_hash", "files"}:
        raise ArchiveError("Archive manifest fields do not match the v1 contract")
    ledger_hash = payload["ledger_manifest_hash"]
    entries = payload["files"]
    if not isinstance(ledger_hash, str) or len(ledger_hash) != 64:
        raise ArchiveError("Archive ledger manifest hash is invalid")
    if not isinstance(entries, list) or not entries:
        raise ArchiveError("Archive manifest files must be a non-empty list")
    paths: list[str] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"path", "bytes", "sha256"}:
            raise ArchiveError("Archive manifest entry is invalid")
        path = raw_entry["path"]
        size = raw_entry["bytes"]
        digest = raw_entry["sha256"]
        if not isinstance(path, str) or not _safe_name(path):
            raise ArchiveError("Archive manifest path is unsafe")
        if not isinstance(size, int) or size < 0:
            raise ArchiveError("Archive manifest byte count is invalid")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ArchiveError("Archive manifest checksum is invalid")
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ArchiveError("Archive manifest paths must be unique and sorted")
    if raw != _canonical_json(payload):
        raise ArchiveError("Archive manifest is not canonically encoded")


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name


def _add_member(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(content))


def _member_bytes(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    handle = archive.extractfile(member)
    if handle is None:
        raise ArchiveError(f"Cannot read archive member: {member.name}")
    return handle.read()


def _canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--repository-root", type=Path, default=Path.cwd())
    create.add_argument("--archive", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    proof = subparsers.add_parser("round-trip")
    proof.add_argument("--repository-root", type=Path, default=Path.cwd())
    proof.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "create":
        payload: object = asdict(create_archive(args.repository_root, args.archive))
    elif args.command == "verify":
        payload = asdict(verify_archive(args.archive))
    else:
        payload = prove_round_trip(args.repository_root, args.archive)
    payload_dict = cast(dict[str, object], payload)
    serializable = {**payload_dict, "archive": str(payload_dict["archive"])}
    print(json.dumps(serializable, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
