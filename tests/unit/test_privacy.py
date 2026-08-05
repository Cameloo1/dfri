from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import dfri.ops.privacy as privacy
from dfri.ops.privacy import (
    PrivacyGuardError,
    forbidden_public_paths,
    markdown_findings,
    scan_markdown_text,
    staged_excluded_findings,
    tracked_excluded_findings,
)


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "DFRI test")
    git(tmp_path, "config", "user.email", "ops@camelon.app")
    return tmp_path


def test_markdown_scanner_reports_locations_without_echoing_values() -> None:
    text = "\n".join(
        (
            "Windows C:/Users/alice/project",
            "Linux /home/bob/project",
            "WSL /mnt/c/Users/carol/project",
            "Host DESKTOP-ABC123",
        )
    )

    findings = scan_markdown_text("notes.md", text)

    assert {item.rule for item in findings} == {
        "windows-user-profile",
        "unix-user-profile",
        "wsl-user-profile",
        "workstation-name",
    }
    assert {item.line for item in findings} == {1, 2, 3, 4}
    assert all("alice" not in item.display() for item in findings)


def test_markdown_scanner_accepts_repo_relative_paths_and_urls() -> None:
    findings = scan_markdown_text(
        "README.md",
        "Use docs/setup.md and https://example.com/Users/guide without a local profile.",
    )

    assert findings == ()


def test_forbidden_control_documents_match_at_any_depth() -> None:
    findings = forbidden_public_paths(
        ("README.md", "DFRI_BUILD_SPEC.md", "nested\\DFRI_AGENT_KICKOFF_PROMPT.md")
    )

    assert [item.path for item in findings] == [
        "DFRI_BUILD_SPEC.md",
        "nested/DFRI_AGENT_KICKOFF_PROMPT.md",
    ]


def test_git_backed_markdown_and_excluded_checks(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / "README.md").write_text("Safe repo-relative documentation.\n", encoding="utf-8")
    (root / "nested").mkdir()
    excluded = root / "nested" / "DFRI_BUILD_SPEC.md"
    excluded.write_text("control\n", encoding="utf-8")
    git(root, "add", "README.md", "nested/DFRI_BUILD_SPEC.md")

    assert markdown_findings(root) == ()
    assert tracked_excluded_findings(root)[0].path == "nested/DFRI_BUILD_SPEC.md"
    assert staged_excluded_findings(root)[0].path == "nested/DFRI_BUILD_SPEC.md"


def test_main_fails_closed_and_passes_clean_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    markdown = root / "README.md"
    markdown.write_text("Local path /Users/example/project\n", encoding="utf-8")
    git(root, "add", "README.md")

    assert privacy.main(("markdown", "--root", str(root))) == 1
    assert "README.md:1: unix-user-profile" in capsys.readouterr().out
    markdown.write_text("Only repo-relative paths.\n", encoding="utf-8")
    assert privacy.main(("markdown", "--root", str(root))) == 0
    assert "PASS" in capsys.readouterr().out


def test_git_inventory_failure_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise OSError("git unavailable")

    monkeypatch.setattr(privacy.subprocess, "run", fail_git)
    with pytest.raises(PrivacyGuardError, match="Unable to inspect Git inventory"):
        markdown_findings(tmp_path)
    assert privacy.main(("markdown", "--root", str(tmp_path))) == 2


def test_repository_contract_wires_privacy_guards() -> None:
    root = Path(__file__).parents[2]
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    windows_make = (root / "make.cmd").read_text(encoding="utf-8")

    for excluded in privacy.FORBIDDEN_PUBLIC_BASENAMES:
        assert excluded in ignore
    assert "verify: privacy-check" in makefile
    assert "publish-scoreboard: privacy-staged" in makefile
    assert "publish: privacy-staged" in makefile
    assert 'if /I "%TARGET%"=="verify"' in windows_make
    assert 'if /I "%TARGET%"=="publish-scoreboard"' in windows_make
    assert 'if /I "%TARGET%"=="publish"' in windows_make
    assert windows_make.count("dfri.ops.privacy excluded-staged") >= 3
    assert "uv run pytest" not in windows_make
    assert windows_make.count("uv run python -m pytest") == 4
