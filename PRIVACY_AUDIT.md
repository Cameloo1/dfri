# DFRI Privacy and Exposure Audit

Audit date: 2026-08-05

Repository: `Cameloo1/dfri`

Scope: every commit reachable from current public branches and tags, plus every GitHub pull-request
head ref available at audit time.

## Findings

| Area | Finding | Required owner action |
|---|---|---|
| Secrets and credentials | No secret was detected. The scanner and manual pass found only empty environment-template variables, process-environment lookups, and synthetic test values. | None; no rotation required. |
| Environment files | No populated environment file is present. `.env.example` contains variable names with blank values. | None. |
| Email addresses | Committed file content contains only the intentional operational contact. Five GitHub merge commits carry provider-generated system and user-noreply metadata; no personal contact mailbox appears in file content or commit metadata. | None. |
| Local environment identifiers | No committed text contains an absolute user-profile path, workstation name, or known local username. | None. |
| Excluded control documents | Neither excluded control document occurs in any of the 19 unique public commits or seven pull-request head refs. A local-only source backup ref contained the private source history but was never a GitHub ref. | None. |
| Forward controls | HEAD lacked explicit ignore entries, a blocking secret-history scan, a Markdown path lint, and a staged-control-document gate. | Remediated in this change. |

## Evidence and method

- Gitleaks `8.30.1` was obtained from its official GitHub release and its archive digest was
  verified before execution.
- The public-ref scan covered 19 unique commits. Fourteen content-bearing commits were scanned;
  the remaining five are merge commits whose trees are represented by their parents.
- Gitleaks scanned complete diffs for public branches, tags, and pull-request heads with full
  redaction enabled and reported no leaks.
- An independent history pass inspected every text blob for common token formats, credential
  assignments, environment-secret names, email addresses, user-profile paths, and workstation
  identifiers. Matches were reviewed without copying candidate values into this report.
- Every public commit tree was enumerated directly to prove that the two excluded control
  documents are absent. Current GitHub pull-request refs added no unique commits beyond the same
  audited set.

Scanner reports and temporary binaries remain outside the repository and are not publication
artifacts.

## Actions taken

- Removed the local-only backup ref from the filtered publication worktree. The private source
  repository remains intact; the removed ref was only an accidental mirror-push risk.
- Added explicit ignore rules for both control documents, generated company output, drafts, and
  private working directories.
- Added a repository privacy guard that reports only repo-relative location and rule names. It
  blocks absolute user or workstation paths in tracked Markdown and blocks either control
  document when tracked or staged, depending on the invoked gate.
- Added the Markdown guard to local verification and the staged-document guard to publication
  commands.
- Added blocking CI steps for a checksum-pinned full-history Gitleaks scan, Markdown privacy lint,
  and tracked control-document rejection.
- Added unit and workflow-contract tests for all new controls.
- Added the public privacy posture to the README.

## History decision and immutable records

No public-history rewrite was performed. No secret, personal mailbox, local path, or excluded
control document was present in public history, so rewriting would add risk without removing an
exposure. Immutable prediction records and their timestamps were not changed.

## Owner action

None. No exposed credential was found, so no key rotation is required.
