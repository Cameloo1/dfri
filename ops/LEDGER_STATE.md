# Dedicated ledger state

The owner approved an in-repository ledger branch and narrow policy change on 2026-09-08.
`ledger-state` is the authority for the three public immutable tables under `state/ledgers/`.
Code stays on protected main. This does not change models, predictions, grades or publication
metadata. Main's historical ledger snapshot is retained; it must never silently replace live state.

## Verified migration

The accepted Pages artifact from run
[34157669998](https://github.com/Cameloo1/dfri/actions/runs/34157669998) and the live feeds matched
all eight predictions, two grades and eight first-publication records. The missing two MTS
predictions and their publication records were appended as three files in signed commit
[`6e6936664c8d92a8b287591b8c2e994fcad624c0`](https://github.com/Cameloo1/dfri/commit/6e6936664c8d92a8b287591b8c2e994fcad624c0).
All ten pre-existing batch files remain byte-identical. Candidate manifest SHA-256:
`d19e659cd06f3ff4d3255571252d8627b93e46ea10cd926d27a877b136d503d0`.

Ruleset 20615350 still protects main exactly as before, and excludes only `refs/heads/ledger-state`.
Ruleset 22517407 applies to that branch, requires verified signatures and prohibits deletion and
force pushes. It has no bypass actors. An initially added linear-history rule was removed because
the preserved repository ancestry contains merge commits; only single-parent appends are produced
by the new writer. No existing history was rewritten.

## Writer and deployment contract

The `dfri.ops.github_ledger` commands use existing `gh` authentication. Repository and branch are
fixed constants, not workflow-dispatch input. Errors never print request payloads, CLI stderr or
tokens. Fetches pin one commit, verify its signature, allowlist manifest paths, and validate all
Parquet bytes, row hashes and ledger relationships. Fetch failure is a hard stop, not a seed fallback.

Before Pages, `preflight` validates the candidate as a byte-preserving superset of current state
and creates a **signed empty commit** using the actual workflow token. It changes no ledger file
or published record. It then re-reads the signed head and proves the manifest unchanged. Rejected
permissions/signing block deployment. These commits are operational write checks, not predictions
or live-cycle evidence. No-change clock runs that do not deploy do not perform this write check.

After Pages acceptance, `promote` revalidates current state and submits only new batch files and
the updated manifest through GitHub's `createCommitOnBranch` API, with an expected parent SHA and
no deletions. Re-running an accepted candidate performs no write. An uncertain write response
causes a bounded re-read/reconciliation; it never authorizes rewriting old records. The state
commit and preflight receipt are retained with deployment evidence. Rules can still change between
preflight and promotion: that race fails closed and requires the same interrupted-promotion recovery.

GitHub signing reference: https://docs.github.com/en/graphql/reference/commits#createcommitonbranch

## Recovery and archival

From a fresh clone with locked dependencies:

```sh
uv run python -m dfri.ops.github_ledger fetch --output .local/ledger-source
uv run python -m dfri.ops.repository_ledger restore \
  --repository-root .local/ledger-source --runtime-root .local/lake/curated
```

Retain the fetched commit SHA. No artifact, raw-source cache or database is needed to recover the
three ledgers. An interrupted fetch uses a new empty destination; inspect/discard only the
incomplete disposable fetch, never an accepted state directory. A cache ahead of Git remains a
hard stop: verify accepted Pages bytes and explicitly reconcile the candidate. The live workflow
fetches state before running either prediction clock. Archive runs separately fetch the signed
state, merge the immutable superset into their disposable source checkout, then package and
round-trip verify that snapshot. The archive receipt records the fetched state commit.

For an offline archive, use a verified `ledger-state` checkout's ledger files and the intended
code revision's citation/license/changelog. A code-only checkout's archive is historical, not latest.

## Pause, retry and rollback

Keep candidates and the last accepted site on any failure. Do not reset, force push or delete
ledger history. The existing publication concurrency group serializes normal writers. An unexpected
concurrent state advance is reviewed, not overwritten. Re-run unchanged inputs only after resolving
the source, rules or transport failure; a manual recovery never counts as a scheduled cycle.

The private operational evidence retains the original ruleset and its unchanged effective-main
comparison. If rollback is required, pause the writer before restoring rules, preserve both branch
histories, and keep last accepted Pages content. Returning code to its previous workflow without
also preserving its ability to read current ledger state is not a safe rollback. Settings changes,
new credentials and additional policy exceptions still require owner approval.

Local and hosted verification/deployment are distinct. The signed recovery seed above is verified;
deployment and genuine scheduled-cycle evidence must be recorded separately, never inferred from
this runbook or an active cron registration.
