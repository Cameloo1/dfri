"""Mirror an accepted static publication and refresh only its operational status files."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast
from urllib.parse import urljoin

import httpx

from dfri.ops.job_status import build_status_report, record_success, render_status_banner

STATUS_PATHS: Final = frozenset({"v1/status.json", "status/banner.html"})


class StatusRefreshError(RuntimeError):
    """The accepted public tree cannot be mirrored or refreshed without changing content."""


def refresh_public_status(
    site_base: str,
    output_root: Path,
    *,
    as_of: datetime,
    client: httpx.Client | None = None,
) -> dict[str, object]:
    """Download and verify the accepted tree, then update only status and manifest bytes."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise StatusRefreshError("as_of must be timezone-aware")
    if output_root.exists() and (not output_root.is_dir() or any(output_root.iterdir())):
        raise StatusRefreshError(f"Output root must be empty: {output_root}")
    base = site_base.rstrip("/") + "/"
    staging = output_root.with_name(f".{output_root.name}.staging-{uuid.uuid4().hex}")
    owned = client is None
    session = client or httpx.Client(timeout=30.0, follow_redirects=False)
    try:
        manifest_response = session.get(urljoin(base, "manifest.json"))
        manifest_response.raise_for_status()
        manifest = manifest_response.json()
        entries = _manifest_entries(manifest)
        for entry in entries:
            relative = cast(str, entry["path"])
            response = session.get(urljoin(base, relative))
            response.raise_for_status()
            content = response.content
            if len(content) != entry["bytes"] or _hash(content) != entry["sha256"]:
                raise StatusRefreshError(f"Public manifest mismatch: {relative}")
            destination = staging / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        current_status_path = staging / "v1" / "status.json"
        if not current_status_path.is_file():
            raise StatusRefreshError("Accepted publication has no v1/status.json")
        current_status = json.loads(current_status_path.read_text(encoding="utf-8"))
        receipts = staging / ".status-receipts"
        _restore_receipts(current_status, receipts)
        refreshed = build_status_report(
            as_of=as_of.astimezone(UTC),
            receipt_directory=receipts,
            publication_mode="live",
        )
        current_status_path.write_text(
            json.dumps(refreshed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        banner = staging / "status" / "banner.html"
        banner.parent.mkdir(parents=True, exist_ok=True)
        banner.write_bytes(render_status_banner(refreshed))
        shutil.rmtree(receipts, ignore_errors=True)
        _write_manifest(staging, manifest)
        _verify_local_tree(staging)
        output_root.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(output_root)
    except (
        StatusRefreshError,
        httpx.HTTPError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, StatusRefreshError):
            raise
        raise StatusRefreshError("Cannot mirror the accepted public publication") from exc
    finally:
        if owned:
            session.close()
    return {
        "schema_version": "v1",
        "status": "PASS",
        "refreshed_at": as_of.astimezone(UTC).isoformat(),
        "overall_status": refreshed["overall_status"],
        "file_count": len(
            _manifest_entries(json.loads((output_root / "manifest.json").read_text()))
        ),
        "changed_paths": sorted([*STATUS_PATHS, "manifest.json"]),
    }


def _restore_receipts(status: object, directory: Path) -> None:
    if not isinstance(status, dict) or status.get("schema_version") != "v1":
        raise StatusRefreshError("Public status schema is not v1")
    jobs = status.get("jobs")
    if not isinstance(jobs, list):
        raise StatusRefreshError("Public status jobs are invalid")
    for raw in jobs:
        if not isinstance(raw, dict):
            raise StatusRefreshError("Public status job is invalid")
        succeeded_at = raw.get("last_successful_run")
        run_url = raw.get("last_successful_run_url")
        if succeeded_at is None and run_url is None:
            continue
        if not isinstance(succeeded_at, str) or not isinstance(run_url, str):
            raise StatusRefreshError("Public status success evidence is incomplete")
        record_success(
            directory,
            job_id=cast(str, raw["job_id"]),
            succeeded_at=datetime.fromisoformat(succeeded_at.replace("Z", "+00:00")),
            workflow_run_url=run_url,
        )


def _manifest_entries(manifest: object) -> list[dict[str, object]]:
    if not isinstance(manifest, dict):
        raise StatusRefreshError("Public manifest must be an object")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise StatusRefreshError("Public manifest files must be a list")
    output: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            raise StatusRefreshError("Public manifest entry is invalid")
        if not isinstance(entry["path"], str) or Path(entry["path"]).is_absolute():
            raise StatusRefreshError("Public manifest path is unsafe")
        if ".." in Path(entry["path"]).parts:
            raise StatusRefreshError("Public manifest path escapes the publication")
        if not isinstance(entry["bytes"], int) or not isinstance(entry["sha256"], str):
            raise StatusRefreshError("Public manifest checksum fields are invalid")
        output.append(cast(dict[str, object], entry))
    paths = [cast(str, item["path"]) for item in output]
    if paths != sorted(paths):
        raise StatusRefreshError("Public manifest entries must be sorted")
    return output


def _write_manifest(root: Path, prior: dict[str, object]) -> None:
    files = sorted(
        path for path in root.rglob("*") if path.is_file() and path.name != "manifest.json"
    )
    manifest = {key: value for key, value in prior.items() if key != "files"}
    manifest["files"] = [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _hash(path.read_bytes()),
        }
        for path in files
    ]
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _verify_local_tree(root: Path) -> None:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entries = _manifest_entries(manifest)
    actual = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )
    if actual != [cast(str, item["path"]) for item in entries]:
        raise StatusRefreshError("Refreshed public tree differs from its manifest")
    for entry in entries:
        content = (root / Path(cast(str, entry["path"]))).read_bytes()
        if len(content) != entry["bytes"] or _hash(content) != entry["sha256"]:
            raise StatusRefreshError(f"Refreshed manifest mismatch: {entry['path']}")


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-base", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()
    payload = refresh_public_status(
        args.site_base,
        args.output_root,
        as_of=datetime.fromisoformat(args.as_of.replace("Z", "+00:00")),
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
