from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dfri.lake.store import AppendOnlyParquetStore
from dfri.ops import github_ledger as remote
from dfri.ops.repository_ledger import RepositoryLedgerError, snapshot_repository_ledgers
from dfri.publish.ledger import PredictionLedger, PublicationLedger

SOURCE = Path(__file__).resolve().parents[2] / "state" / "ledgers"


class FakeGitHub:
    def __init__(self) -> None:
        self.files = {
            p.relative_to(SOURCE).as_posix(): p.read_bytes()
            for p in SOURCE.rglob("*")
            if p.is_file()
        }
        self.head = "a" * 40
        self.signed = True
        self.calls: list[dict[str, Any]] = []
        self.lose_response = False
        self.reject = False

    def api(self, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        if endpoint == "graphql":
            assert payload is not None
            request = payload["variables"]["input"]
            self.calls.append(request)
            assert request["branch"] == {
                "repositoryNameWithOwner": remote.REPOSITORY,
                "branchName": "ledger-state",
            }
            assert request["expectedHeadOid"] == self.head
            assert request["fileChanges"]["deletions"] == []
            if self.reject:
                raise remote.RemoteLedgerError("rejected")
            for item in request["fileChanges"]["additions"]:
                assert item["path"].startswith(remote.PREFIX)
                self.files[item["path"][len(remote.PREFIX) :]] = base64.b64decode(item["contents"])
            self.head = f"{len(self.calls):040x}"
            if self.lose_response:
                self.lose_response = False
                raise remote.RemoteLedgerError("uncertain transport after commit")
            return {
                "data": {
                    "createCommitOnBranch": {
                        "commit": {"oid": self.head, "signature": {"isValid": self.signed}}
                    }
                }
            }
        if "/git/ref/" in endpoint:
            assert endpoint.endswith("heads/ledger-state")
            return {"object": {"sha": self.head}}
        if "/commits/" in endpoint:
            return {"commit": {"verification": {"verified": self.signed}}}
        path, query = endpoint.split("/contents/", 1)[1].split("?ref=")
        assert query == self.head
        return {
            "encoding": "base64",
            "type": "file",
            "content": base64.b64encode(self.files[path[len(remote.PREFIX) :]]).decode(),
        }


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(remote, "api", fake.api)
    return fake


def new_candidate(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    shutil.copytree(SOURCE, runtime)
    store = AppendOnlyParquetStore(runtime)
    ledger = PredictionLedger(store)
    original = ledger.read_all()[0]
    appended = ledger.append(replace(original, inputs_hash="e" * 64))
    publication = PublicationLedger(store).read_all()[0]
    store.append(
        "publication_records", [replace(publication, prediction_id=appended.record_id).row()]
    )
    candidate = tmp_path / "candidate"
    snapshot_repository_ledgers(runtime, candidate)
    return candidate


def test_fetch_is_pinned_signed_and_byte_identical(github: FakeGitHub, tmp_path: Path) -> None:
    assert remote.fetch_state(tmp_path / "snapshot") == github.head
    for path, content in github.files.items():
        assert (tmp_path / "snapshot" / path).read_bytes() == content
    with pytest.raises(remote.RemoteLedgerError, match="empty"):
        remote.fetch_state(tmp_path / "snapshot")
    github.signed = False
    with pytest.raises(remote.RemoteLedgerError, match="signature"):
        remote.fetch_state(tmp_path / "unsigned")


@pytest.mark.parametrize("path", ["../../outside", "predictions/not-a-batch", "MANIFEST.json"])
def test_fetch_rejects_unmanaged_paths(github: FakeGitHub, tmp_path: Path, path: str) -> None:
    manifest = json.loads(github.files["MANIFEST.json"])
    manifest["tables"]["predictions"]["files"][0]["path"] = path
    github.files["MANIFEST.json"] = json.dumps(manifest).encode()
    with pytest.raises(remote.RemoteLedgerError, match="non-ledger"):
        remote.fetch_state(tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


def test_preflight_exercises_writer_without_editing_records(github: FakeGitHub) -> None:
    before = dict(github.files)
    receipt = remote.preflight("test-run")
    assert receipt["signed"] is True
    assert github.calls[0]["fileChanges"] == {"additions": [], "deletions": []}
    assert github.files == before


def test_preflight_fails_closed_when_policy_rejects_writer(github: FakeGitHub) -> None:
    github.reject = True
    with pytest.raises(remote.RemoteLedgerError, match="rejected"):
        remote.preflight("denied")


@pytest.mark.parametrize("lost_response", [False, True])
def test_promotion_append_retry_noop(
    github: FakeGitHub, tmp_path: Path, lost_response: bool
) -> None:
    candidate = new_candidate(tmp_path)
    before = dict(github.files)
    github.lose_response = lost_response
    remote.promote(candidate, "test")
    assert len(github.calls) == 1
    for path, content in before.items():
        if path != "MANIFEST.json":
            assert github.files[path] == content
    assert remote.promote(candidate, "retry")["added_files"] == 0
    assert len(github.calls) == 1


def test_rejection_bounded_and_candidate_untouched(github: FakeGitHub, tmp_path: Path) -> None:
    candidate = new_candidate(tmp_path)
    before = dict(github.files)
    github.reject = True
    with pytest.raises(remote.RemoteLedgerError):
        remote.promote(candidate, "reject")
    assert len(github.calls) == 3 and github.files == before


def test_mutable_candidate_never_reaches_remote(github: FakeGitHub, tmp_path: Path) -> None:
    candidate = new_candidate(tmp_path)
    path = candidate / "MANIFEST.json"
    manifest = json.loads(path.read_bytes())
    manifest["schema_version"] = 999
    path.write_text(json.dumps(manifest))
    with pytest.raises(RepositoryLedgerError):
        remote.promote(candidate, "bad")
    assert not github.calls


@pytest.mark.parametrize("response", ["not-json", '{"errors":[{"message":"private"}]}'])
def test_api_redacts_failures(monkeypatch: pytest.MonkeyPatch, response: str) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, response, "secret")
    )
    with pytest.raises(remote.RemoteLedgerError) as error:
        remote.api("graphql", {"private": "payload"})
    assert "private" not in str(error.value) and "secret" not in str(error.value)


def test_workflow_boundaries() -> None:
    root = SOURCE.parents[1]
    workflow = (root / ".github/workflows/m2-scoreboard.yml").read_text()
    assert "github_ledger fetch" in workflow
    assert workflow.index("github_ledger preflight") < workflow.index(
        "name: Deploy the accepted Pages artifact"
    )
    assert workflow.index("name: Deploy the accepted Pages artifact") < workflow.index(
        "github_ledger promote"
    )
    assert 'git push origin "HEAD:${GITHUB_REF_NAME}"' not in workflow
    assert "--repository-root .local/ledger-source" in workflow
    assert "all) grade_commands=(grade mts-grade)" in workflow
    assert "grade_appended=$(( grade_appended + this_grade_appended ))" in workflow
    archive = (root / ".github/workflows/archive.yml").read_text()
    assert "github_ledger fetch" in archive and "repository_ledger merge" in archive


def test_cli_and_candidate_preflight(
    github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    candidate = new_candidate(tmp_path)
    commands = [
        ["fetch", "--output", str(tmp_path / "fetched")],
        ["preflight", "--candidate", str(candidate), "--run-id", "cli"],
        ["promote", "--candidate", str(candidate), "--run-id", "cli"],
    ]
    for command in commands:
        monkeypatch.setattr(sys, "argv", ["github-ledger", *command])
        remote.main()
        assert json.loads(capsys.readouterr().out)["branch"] == "ledger-state"


@pytest.mark.parametrize("failure", [OSError("private"), subprocess.TimeoutExpired("private", 90)])
def test_transport_exceptions_are_safe(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(remote.RemoteLedgerError, match="unavailable") as error:
        remote.api("endpoint")
    assert "private" not in str(error.value)


def test_cli_failure_output_is_not_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "private", "secret")
    )
    with pytest.raises(remote.RemoteLedgerError, match="rejected") as error:
        remote.api("endpoint")
    assert "secret" not in str(error.value)


def test_unsigned_mutation_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        remote,
        "api",
        lambda *a, **k: {
            "data": {"createCommitOnBranch": {"commit": {"oid": "b" * 40, "signature": None}}}
        },
    )
    with pytest.raises(remote.RemoteLedgerError, match="not verified"):
        remote._commit("a" * 40, [], "test")


def test_invalid_head_is_rejected(github: FakeGitHub, tmp_path: Path) -> None:
    github.head = "../main"
    with pytest.raises(remote.RemoteLedgerError, match="identity"):
        remote.fetch_state(tmp_path / "bad")


def test_api_success_decodes_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, '{"ok":true}', "")
    )
    assert remote.api("endpoint") == {"ok": True}
