"""Signed, optimistic, append-only writes to the dedicated public ledger branch."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from dfri.ops.repository_ledger import merge_repository_candidate, verify_repository_ledgers

REPOSITORY = "Cameloo1/dfri"
BRANCH = "ledger-state"
PREFIX = "state/ledgers/"
MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid signature { isValid } } }
}
"""


class RemoteLedgerError(RuntimeError):
    """The durable ledger could not be read or safely advanced."""


def api(endpoint: str, payload: dict[str, Any] | None = None) -> Any:
    """Use existing gh authentication; never expose CLI stderr or request contents."""
    command = ["gh", "api", "--hostname", "github.com", endpoint]
    if payload is not None:
        command += ["--method", "POST", "--input", "-"]
    try:
        result = subprocess.run(
            command,
            input=json.dumps(payload) if payload is not None else None,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RemoteLedgerError(
            "GitHub ledger request unavailable; retain candidate and retry"
        ) from None
    if result.returncode:
        raise RemoteLedgerError(
            "GitHub ledger request rejected; retain candidate and inspect rules"
        )
    try:
        response = json.loads(result.stdout)
    except ValueError:
        raise RemoteLedgerError("GitHub returned an invalid ledger response") from None
    if isinstance(response, dict) and response.get("errors"):
        raise RemoteLedgerError("GitHub ledger mutation rejected; candidate remains unaccepted")
    return response


def _content(path: str, sha: str) -> bytes:
    response = api(f"repos/{REPOSITORY}/contents/{PREFIX}{path}?ref={sha}")
    if response.get("encoding") != "base64" or response.get("type") != "file":
        raise RemoteLedgerError("Remote ledger member is not a regular encoded file")
    return base64.b64decode("".join(response["content"].split()), validate=True)


def fetch_state(output: Path) -> str:
    """Read a commit-pinned verified snapshot, never fall back to code main."""
    if output.exists() and (output.is_symlink() or not output.is_dir() or any(output.iterdir())):
        raise RemoteLedgerError("Ledger fetch destination must be empty")
    ref = api(f"repos/{REPOSITORY}/git/ref/heads/{BRANCH}")
    sha = str(ref["object"]["sha"])
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RemoteLedgerError("Invalid ledger commit identity")
    commit = api(f"repos/{REPOSITORY}/commits/{sha}")
    if commit["commit"]["verification"]["verified"] is not True:
        raise RemoteLedgerError("Ledger head lacks a verified signature")
    manifest_bytes = _content("MANIFEST.json", sha)
    manifest = json.loads(manifest_bytes)
    paths = [entry["path"] for table in manifest["tables"].values() for entry in table["files"]]
    if len(paths) != len(set(paths)) or any(
        not isinstance(path, str)
        or re.fullmatch(
            r"(predictions|grades|publication_records)/batch-[0-9a-f]{64}\.parquet", path
        )
        is None
        for path in paths
    ):
        raise RemoteLedgerError("Remote manifest contains a non-ledger or duplicate path")
    output.mkdir(parents=True, exist_ok=True)
    (output / "MANIFEST.json").write_bytes(manifest_bytes)
    for path in paths:
        destination = output / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_content(path, sha))
    verify_repository_ledgers(output)
    return sha


def _commit(sha: str, additions: list[dict[str, str]], message: str) -> str:
    response = api(
        "graphql",
        {
            "query": MUTATION,
            "variables": {
                "input": {
                    "branch": {"repositoryNameWithOwner": REPOSITORY, "branchName": BRANCH},
                    "expectedHeadOid": sha,
                    "message": {"headline": message},
                    "fileChanges": {"additions": additions, "deletions": []},
                }
            },
        },
    )
    commit = response["data"]["createCommitOnBranch"]["commit"]
    if not commit.get("signature") or commit["signature"]["isValid"] is not True:
        raise RemoteLedgerError("Ledger write is not verified; stop before Pages deployment")
    return str(commit["oid"])


def preflight(run_id: str, candidate: Path | None = None) -> dict[str, object]:
    """Prove the actual token can append a signed, zero-file-change commit before Pages."""
    with tempfile.TemporaryDirectory(prefix="dfri-ledger-preflight-") as temporary:
        root = Path(temporary) / "state"
        sha = fetch_state(root)
        before = verify_repository_ledgers(root).manifest_hash
        if candidate is not None:
            merge_repository_candidate(candidate, root)
        written = _commit(sha, [], f"ops: verify ledger writer ({run_id})")
        checked = Path(temporary) / "checked"
        head = fetch_state(checked)
        if head != written or verify_repository_ledgers(checked).manifest_hash != before:
            raise RemoteLedgerError(
                "Ledger changed during write preflight; retry before deployment"
            )
    return {"branch": BRANCH, "commit": written, "manifest_hash": before, "signed": True}


def promote(candidate: Path, run_id: str) -> dict[str, object]:
    """Validate a superset; retry uncertain writes by re-reading, never overwriting history."""
    candidate_receipt = verify_repository_ledgers(candidate)
    for attempt in range(3):
        with tempfile.TemporaryDirectory(prefix="dfri-ledger-promote-") as temporary:
            root = Path(temporary) / "state"
            sha = fetch_state(root)
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            merged = merge_repository_candidate(candidate, root)
            if not merged.added_files:
                return {"branch": BRANCH, "commit": sha, "added_files": 0, "signed": True}
            additions = [
                {
                    "path": PREFIX + path.relative_to(root).as_posix(),
                    "contents": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
                for path in sorted(root.rglob("*"))
                if path.is_file() and before.get(path.relative_to(root)) != path.read_bytes()
            ]
            try:
                written = _commit(
                    sha, additions, f"data: append immutable scoreboard ledger ({run_id})"
                )
            except RemoteLedgerError:
                if attempt == 2:
                    raise
                continue
            checked = Path(temporary) / "checked"
            head = fetch_state(checked)
            if (
                head != written
                or verify_repository_ledgers(checked).manifest_hash
                != candidate_receipt.manifest_hash
            ):
                raise RemoteLedgerError("Post-write ledger verification failed; preserve candidate")
            return {
                "branch": BRANCH,
                "commit": written,
                "added_files": merged.added_files,
                "signed": True,
            }
    raise RemoteLedgerError("Ledger promotion exhausted its bounded retries")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("preflight")
    check.add_argument("--run-id", required=True)
    check.add_argument("--candidate", type=Path)
    publish = commands.add_parser("promote")
    publish.add_argument("--candidate", type=Path, required=True)
    publish.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.command == "fetch":
        result: dict[str, object] = {"branch": BRANCH, "commit": fetch_state(args.output)}
    elif args.command == "preflight":
        result = preflight(args.run_id, args.candidate)
    else:
        result = promote(args.candidate, args.run_id)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
