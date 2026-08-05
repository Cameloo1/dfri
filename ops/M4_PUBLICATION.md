# M4 publication operation

This runbook covers deterministic static publication, the read-only API process, public uptime
receipts, and recovery without rewriting accepted outputs. Runtime files and receipts remain under
ignored `published/` and `.local/` paths.

## Cold publication gate

Run the complete public build from a clean dependency state:

```sh
make bootstrap
make verify
make publish
```

On Windows, use `make.cmd bootstrap`, `make.cmd verify`, and `make.cmd publish`. The publish target:

1. rejects drift in `docs/openapi-v1.json`;
2. validates the public changelog registry;
3. builds the complete site twice from the frozen first-public prediction snapshot and requires
   byte-identical output;
4. checks page weight, estimated 4G load, contrast, server-rendered content, and provenance shape;
5. measures 100 local API requests and requires p95 below 300 ms; and
6. runs axe 4.12.1 plus a JavaScript-disabled render across every generated page.

Evidence is written to `.local/evidence/m4-*.json`. CI uploads those ignored receipts for 90 days.
The frozen snapshot is a verification input copied from the public feed; it never counts as a new
scheduled prediction or grade.

## Read-only API

Serve one atomically promoted publication directory:

```sh
uv run python -m dfri.api.app --publication-root published/public --host 127.0.0.1 --port 8000
```

The process has only the nine committed `GET` routes. A missing or invalid publication returns a
503 `BLOCKED` response. There are no mutation or authentication routes in v1. Owner deviation
D-010 defers public operation and every API-specific M4 criterion; the implementation remains a
tested dormant capability, not a live service. The versioned Pages feeds are the v1 access surface.

## Pause, retry, skip, and abort

- **Pause:** disable the relevant scheduled workflow. Do not delete accepted state or publication
  artifacts. Record why and the exact run URL in `DEVIATIONS.md` when the pause changes an AC.
- **Retry:** rerun the same commit and inputs. Prediction, grade, publication, and changelog
  identities make unchanged retries idempotent. A different candidate must use a new version or
  append-only record.
- **Skip:** only no-op release-calendar checks may skip. A due ingest, publish, or monitoring failure
  cannot be marked successful or replaced with a hand-built receipt.
- **Abort:** stop before promotion when validation fails. Preserve the failed CI log and ignored
  receipt; never copy a partial staging directory into the public root.

## Recovery matrix

| Failure | Inspect | Recovery | Proof |
|---|---|---|---|
| Ingest fails before append | job receipt, source response, checkpoint | correct the source-specific issue and rerun the same boundary | unchanged accepted rows plus an appended success receipt |
| Build fails | CI log and `.local/evidence` | fix the deterministic input or renderer and rerun | prior publication bytes remain unchanged |
| Promotion fails | staging/backup paths and process error | retry the atomic promotion after the filesystem issue is resolved | last-good directory is restored; candidate is not visible |
| Deployment fails | Pages run and candidate state artifact | rerun deployment for the same candidate | accepted state updates only after public manifest verification |
| Uptime/freshness fails | `m4-uptime-*` Actions artifact | repair the public site/API or source clock, then dispatch the monitor | a later green receipt; the failed receipt remains readable |
| Bad public release | changelog and last accepted artifact | redeploy the last accepted artifact, then append a correction/restatement | old rows remain; correction has a new changelog entry |

The publisher refuses an output directory without its own `manifest.json`, removes stale managed
paths through full-directory replacement, restores the last-good directory when promotion raises,
and deletes incomplete staging directories. Tests inject build and promotion failures and verify a
byte-identical retry.

## Uptime and freshness

`.github/workflows/m4-uptime.yml` runs hourly and can be dispatched manually. It checks every stable
site/feed URL, nowcast publication age, and—once configured—every public API surface. Each attempt
uploads an owner-readable JSON receipt. Until an approved revisit trigger occurs, the receipt
reports the API as `DEFERRED` and can be green when the required Pages and freshness checks pass.
Once `DFRI_API_BASE_URL` exists, API checks become required automatically and any failure is red.
