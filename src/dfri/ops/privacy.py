"""Fail closed on public-repository privacy boundary violations."""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

FORBIDDEN_PUBLIC_BASENAMES: Final = frozenset(
    {"DFRI_AGENT_KICKOFF_PROMPT.md", "DFRI_BUILD_SPEC.md"}
)
ABSOLUTE_PATH_RULES: Final = (
    (
        "windows-user-profile",
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:[\\/](?:Users|Documents and Settings)[\\/])"
            r"[^\\/\s:]+"
        ),
    ),
    (
        "unix-user-profile",
        re.compile(r"(?<![A-Za-z0-9_])/(?:home|Users)/[A-Za-z0-9._-]+(?:/|\b)"),
    ),
    (
        "wsl-user-profile",
        re.compile(r"(?i)(?<![A-Za-z0-9_])/mnt/[a-z]/Users/[A-Za-z0-9._-]+(?:/|\b)"),
    ),
    (
        "workstation-name",
        re.compile(r"(?i)\b(?:DESKTOP-[A-Z0-9-]+|[A-Z0-9]+-PC)\b"),
    ),
)


class PrivacyGuardError(RuntimeError):
    """Git inventory could not be inspected safely."""


@dataclass(frozen=True, order=True)
class PrivacyFinding:
    path: str
    line: int | None
    rule: str

    def display(self) -> str:
        location = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{location}: {self.rule}"


def scan_markdown_text(path: str, text: str) -> tuple[PrivacyFinding, ...]:
    """Return only locations and rule names, never matching workstation text."""

    findings: list[PrivacyFinding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in ABSOLUTE_PATH_RULES:
            if pattern.search(line):
                findings.append(PrivacyFinding(path=path, line=line_number, rule=rule))
    return tuple(findings)


def forbidden_public_paths(paths: Iterable[str]) -> tuple[PrivacyFinding, ...]:
    findings = {
        PrivacyFinding(path=normalized, line=None, rule="excluded-control-document")
        for item in paths
        if (normalized := item.replace("\\", "/"))
        and PurePosixPath(normalized).name in FORBIDDEN_PUBLIC_BASENAMES
    }
    return tuple(sorted(findings))


def markdown_findings(root: Path) -> tuple[PrivacyFinding, ...]:
    resolved_root = root.resolve()
    findings: list[PrivacyFinding] = []
    for relative in _git_paths(resolved_root, "ls-files", "--", "*.md"):
        path = (resolved_root / relative).resolve()
        if not path.is_relative_to(resolved_root):
            raise PrivacyGuardError(f"Tracked Markdown escapes repository root: {relative}")
        findings.extend(scan_markdown_text(relative, path.read_text(encoding="utf-8")))
    return tuple(sorted(findings))


def tracked_excluded_findings(root: Path) -> tuple[PrivacyFinding, ...]:
    return forbidden_public_paths(_git_paths(root.resolve(), "ls-files"))


def staged_excluded_findings(root: Path) -> tuple[PrivacyFinding, ...]:
    return forbidden_public_paths(
        _git_paths(root.resolve(), "diff", "--cached", "--name-only", "--diff-filter=ACMR", "--")
    )


def _git_paths(root: Path, *arguments: str) -> tuple[str, ...]:
    if not arguments:
        raise PrivacyGuardError("Unable to inspect Git inventory without a command")
    command, *command_arguments = arguments
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), command, "-z", *command_arguments],
            check=True,
            stdout=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PrivacyGuardError(f"Unable to inspect Git inventory for {arguments[0]}") from exc
    return tuple(item for item in completed.stdout.decode("utf-8").split("\0") if item)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("markdown", "excluded-tracked", "excluded-staged"))
    parser.add_argument("--root", type=Path, default=Path())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.mode == "markdown":
            findings = markdown_findings(args.root)
        elif args.mode == "excluded-tracked":
            findings = tracked_excluded_findings(args.root)
        else:
            findings = staged_excluded_findings(args.root)
    except PrivacyGuardError as exc:
        print(f"privacy guard: BLOCKED: {exc}")
        return 2
    if findings:
        print("privacy guard: FAIL")
        for finding in findings:
            print(finding.display())
        return 1
    print(f"privacy guard: PASS ({args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
