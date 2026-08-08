# Repository-backed ledger durability report

**Status:** PASS

**Verified:** 2026-08-08

**Public main:** `9b9d0153e449cbbdfdc01671404463294b48580f`

## Result

Predictions, first-print grades, and first-publication records now live in
[`state/ledgers/`](state/ledgers/) on the default branch. Git history is the durable authority.
GitHub Actions artifacts remain a 90-day redundant cache for faster public-source restoration, but
the clock and full public ledger recover without any artifact or connected state service.

The migration preserved every accepted batch byte, record ID, and original timestamp. The hosted
scheduled-equivalent proof restored Git state, found no new prediction or grade, deployed Pages,
verified the accepted candidate, and skipped the repository commit because the candidate was
identical.

## Migrated baseline

The migration source was the deployment-accepted state from
[Actions run 31280013811](https://github.com/Cameloo1/dfri/actions/runs/31280013811).
Its complete runtime-bundle manifest hash was
`73a5617f190c990de5a7632d72ce5b4de539af273eeafb0b2d4dbc78c4e059f7`.

| Ledger | Parquet batches | Rows | Record identity |
|---|---:|---:|---|
| Predictions | 6 | 6 | Six stable `prd_` IDs |
| Grades | 2 | 2 | Both bound to existing prediction IDs |
| Publication records | 2 | 6 | Exactly one record per prediction |
| **Total** | **10** | **14** | No duplicate or orphan IDs |

The canonical repository manifest is
[`state/ledgers/MANIFEST.json`](state/ledgers/MANIFEST.json). It binds every relative path to:

- the canonical row hash used in its content-addressed filename;
- the exact Parquet SHA-256 and byte length;
- its row count; and
- its prediction IDs.

Repository manifest SHA-256:
`3eadf24738b1c329c0d9f2760ad4a88b037ca869a8637cdd58dfb9dd67e7fcf3`.

The deterministic inventory hash over all ten relative paths and exact file SHA-256 values is
`a1a9f98eef2331dc6bb14ffbb8a5ded5bdbe27a75a014954c376cc3089d3abb0` before and after migration.
The source and repository inventories compared byte-for-byte equal.

All six original prediction timestamps remain unchanged:

- four predictions at `2026-08-05T02:24:45.950514+00:00`; and
- two predictions at `2026-08-07T23:50:56.278575+00:00`.

Both grade records retain `graded_at=2026-08-07T19:00:00+00:00`. Every publication record retains
its original `published_at`; byte identity makes timestamp rewriting impossible without a manifest
and Git-history change.

## Persistence and workflow boundary

The scheduled workflow now follows this order:

1. Optionally restore the newest integrity-checked Actions cache.
2. Restore and verify `state/ledgers/` as the authority.
3. Reconstruct disposable public-source inputs when the cache is absent.
4. Run the idempotent prediction and grading jobs.
5. Build and deploy the complete Pages artifact.
6. Verify the deployment-accepted candidate against the repository ledger.
7. Permit only new `batch-<canonical-hash>.parquet` files and the corresponding manifest update.
8. Commit those appends to the default branch without force, then write the deployment receipt.

Existing batch edits, deletions, orphan grades, missing publication records, noncanonical filenames,
unmanaged staged paths, byte disagreements, and a cache ahead of Git all fail closed. A direct push
retries three times against concurrent code-only changes and never force-pushes.

## Proofs

### 1. Byte identity

The accepted artifact and repository snapshots contain the same ten relative paths and exact file
SHA-256 values. Decoding all Parquet batches reproduced 6 prediction rows, 2 grade rows, and 6
publication rows with the same IDs and timestamps. The inventory hash remained
`a1a9f98eef2331dc6bb14ffbb8a5ded5bdbe27a75a014954c376cc3089d3abb0`.

### 2. Immutability and no-op behavior

Tests attempt to alter existing candidate bytes, delete an existing batch, introduce a cache-only
batch, change an existing prediction under the same ID, and supply a noncanonical manifest. Each
path is rejected. A candidate identical to the repository reports `added_files=0` and leaves file
bytes and modification times unchanged.

The full local suite passed 400 tests at 85.03% coverage. Ruff lint, strict mypy over 61 source
files, privacy checks, registry drift checks, and staged-exclusion checks also passed. Public PR CI
passed for [PR 17](https://github.com/Cameloo1/dfri/pull/17) and the checkout-order correction in
[PR 18](https://github.com/Cameloo1/dfri/pull/18).

### 3. Hosted scheduled-equivalent no-op

[Actions run 31281435353](https://github.com/Cameloo1/dfri/actions/runs/31281435353) completed green
on the hosted runner:

- Git restore: `added_files=0`, `file_count=10`, `row_count=14`, manifest `3eadf247...`;
- prediction job: 2 already present, 0 appended;
- grading job: 0 appended, 4 not matured, stored-grade integrity verified;
- repository candidate merge: `added_files=0`, `file_count=10`, `row_count=14`, same manifest;
- repository commit step: skipped as the required no-op; and
- Pages deployment, accepted-cache preservation, and deployment receipt: PASS.

This manual run is durability evidence only and does not count as a scheduled M2 weekly cycle.

### 4. Fresh-clone recovery without an artifact

A depth-one clone of public `main` at `9b9d0153e449cbbdfdc01671404463294b48580f` began without a
`.local` directory. Running only the repository restore command created all ten runtime batches and
14 rows with manifest `3eadf247...`. The restored inventory hash was `a1a9f98...`, identical to the
pre-migration accepted baseline. No Actions artifact, database, API, or other connected state
service was read.

### 5. Failure and recovery validation

The first hosted proof,
[run 31281158285](https://github.com/Cameloo1/dfri/actions/runs/31281158285), exposed that checkout
cleaned the downloaded candidate directory. Git restore, clock execution, site build, and Pages
deployment had passed; no ledger row was appended or changed. PR 18 moved checkout before download
and added an enforced ordering assertion. The identical rerun then passed end to end.

## Recovery rule

If all Actions artifacts expire, clone `main`, verify `state/ledgers/MANIFEST.json`, restore the
repository ledgers, and rerun the public-source backfills. Never bootstrap a second ledger. If a
Pages-accepted candidate is ahead of Git because its commit failed, the next run stops; review and
merge that exact retained candidate before resuming the clock.

## Owner action

None. Item 1 is complete. The repository contains the durable ledger and the artifact-free recovery
path is proven.
