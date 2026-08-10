# Resilience and distribution report

Status date: 2026-08-10

## Implemented

- `/v1/status.json` reports each registered publication lane's last successful run, next expected
  run, run SLA, applicable release SLA, and missed-run/release state. Content-addressed success
  receipts are preserved in the allowlisted runtime bundle; the hourly public observer rebuilds
  only the status documents after verifying the accepted Pages manifest.
- Every rendered page embeds the no-JavaScript status strip and preserves the existing visible
  source-fallback notice. The independent hourly observer checks the accepted site after its
  status-only deployment.
- Both scheduled workflows open or update a GitHub issue on failure with the repository-scoped
  `GITHUB_TOKEN`. No notification account or paid service is introduced. Recovery procedures are
  in `ops/RESILIENCE.md`.
- All direct Python, build, Node, and remote GitHub Action dependencies are exact and lock-backed.
  CI installs from `uv.lock` and `package-lock.json`, then runs blocking `pip-audit` and `npm audit`
  checks. The first audit found four advisories; the affected PyArrow, pypdf, and pytest pins were
  upgraded to fixed versions before this report was accepted.
- `make archive-round-trip` builds the allowlisted ledger archive twice, checks every member hash,
  restores it in a clean temporary directory, verifies repository-ledger semantics, and requires
  byte-identical archive output. GitHub Releases have a workflow candidate that attaches the same
  verified archive using only the repository token.
- `/v1/events.json` and `/events.xml` publish stable events for predictions, grades, restatements,
  explicit source-fallback activations, and methodology/publication changes.
- Every stable page has canonical Open Graph and Twitter-card metadata. Generic, company, and
  prediction pages receive deterministic 1200 by 630 static preview PNGs generated without a
  runtime network call. A citation block is rendered only after a verified archive DOI exists.

## Verification

- `make.cmd verify`: 517 tests passed; coverage 85.14%; deterministic replay passed.
- `make.cmd vulnerability-scan`: zero known Python or Node vulnerabilities after pinned upgrades.
- `make.cmd archive-round-trip`: PASS; two archive builds were byte-identical and the clean restore
  reproduced the committed ledger manifest.
- `make.cmd publish`: PASS; 61 pages passed JavaScript-disabled, semantic, keyboard, mobile-layout,
  and automated accessibility checks with zero failures.
- `make.cmd site-quality`: PASS; heaviest page 86,451 bytes, estimated 4G load 582.255 ms, minimum
  contrast 5.736:1, 50 company pages and 61 pages total.
- A disposable clone of commit `07ecff9` with no runtime state completed locked bootstrap,
  `make.cmd verify`, `make.cmd archive-round-trip`, `make.cmd publish`, and
  `make.cmd site-quality` with the same passing results. Windows Application Control required the
  clone to reuse the policy-allowed parent Python executable; the clone's dependencies were still
  synchronized from its own exact lock and all product/runtime state came only from the clone.

Generated publications, vulnerability caches, browser evidence, archive tarballs, and local test
workspaces remain ignored development artifacts and are not public source files.

## Blocked external proof

A real offsite Zenodo deposit and DOI remain `BLOCKED — CREDENTIAL/POLICY`. Zenodo requires either
a connected GitHub account or a Zenodo account and deposit token. Neither is authorized or
available. The deterministic package, `.zenodo.json`, `CITATION.cff`, release-asset workflow, DOI
registry, and verification procedure are ready; the registry contains null DOI fields and the site
does not render a placeholder citation. See `DEVIATIONS.md` D-015 and `ops/ARCHIVE.md`.

## Publication state

This report verifies a local candidate. Commit, push, workflow registration, Pages deployment,
failure-issue behavior on a real runner, offsite deposit, DOI resolution, and live-site behavior are
separate states and are not claimed here.
