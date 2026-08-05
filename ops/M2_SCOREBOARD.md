# M2 scoreboard clock operations

This runbook covers the active M2 GitHub Actions clock in `Cameloo1/dfri` and the public scoreboard
at https://cameloo1.github.io/dfri/. Q-001 is resolved. Do not count local, manual, or backdated
runs as live-cycle evidence.

## Activation status

The public baseline and first fix both passed push-triggered CI. GitHub Pages uses the custom
workflow source, HTTPS is enforced, and the `github-pages` environment accepts only `main`.
Workflow `327469010` is active with both schedules below.

Manual bootstrap run https://github.com/Cameloo1/dfri/actions/runs/30968841429 deployed four
predictions and zero grades. All 15 public manifest entries and four immutable permalinks passed
HTTP, byte-length, and SHA-256 checks. Candidate artifact `8915896515` and accepted-state artifact
`8915902293` are byte-identical and restore 1,029 allowlisted files with no private paths. Receipt
artifact `8915902998` records a 367,826-second `FAIL`, as required: this manual recovery run used
the July 31 H.8 release outside the live four-hour window. Pages and accepted-state promotion
completed before that receipt failure, so the state is recoverable. Never enable
`bootstrap_state` again. The next acceptance evidence must come from genuine scheduled runs.

## Current workflow contract

The default-branch workflow has two UTC schedules:

- Weekdays at 21:17 UTC: refresh dated Board/Census inputs, grade any newly matured first-print
  G.19 predictions, and publish only when a grade was appended.
- Weekdays at 23:17 UTC: check for a new dated H.8 release, append predictions only when input
  identity changes, and publish only when a prediction was appended. The weekday check covers the
  ordinary Friday release plus Board holiday shifts to Monday or Tuesday; non-release days are
  idempotent no-ops.

The hours follow the Board's current 3:00 p.m. Eastern G.19 and 4:15 p.m. Eastern H.8 calendars,
leave margin around both release windows, and avoid GitHub's documented top-of-hour congestion.
GitHub documents that scheduled runs use the default branch, use POSIX cron in UTC, and may be
delayed under load:
https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
The live Board calendar is https://www.federalreserve.gov/newsevents/calendar.htm.

Only one clock run can execute at a time. A later run queues instead of cancelling the current
one. All jobs are idempotent; a no-change run preserves state but does not mutate or redeploy the
public artifact. Manual dispatch supports `predict`, `grade`, or `all`; `force_publish` is a
recovery gate and must not be used to manufacture live-cycle evidence.

## State and recovery

GitHub-hosted runners are disposable. Each build restores the newest deployment-accepted
`dfri-m2-state` artifact, verifies its internal SHA-256 manifest and strict allowlist, and performs
the run. A no-change run uploads its refreshed state directly under that accepted name. A changed
run first uploads `dfri-m2-state-candidate`; only after GitHub Pages accepts the deployment does the
deploy job copy it to the accepted `dfri-m2-state` name. A failed Pages deployment therefore cannot
become the next clock state, while a successful deployment remains recoverable even if its later
four-hour SLA check fails. Both artifact classes expire after 90 days. The bundle contains only:

- public-source `raw_observations` Parquet batches;
- append-only prediction, grade, and first-publication ledgers;
- Board backfill checkpoints; and
- scoreboard job receipts.

Private SEC loan-level files, API keys, environment files, caches, locks, and unrelated local
evidence cannot enter the bundle. Restore refuses a non-empty destination. If the clock has ever
published and no valid prior state artifact can be restored, cancel the run and recover from a
reviewed bundle before continuing; never bootstrap a second ledger over an existing public
scoreboard. If Pages succeeds but promotion of its candidate state fails, preserve the candidate
artifact, confirm the public feed manifest, and manually promote that exact reviewed bundle before
the clock runs again.

## Deployment and evidence

The publisher records each prediction's first live `published_at`, `data_vintage`, and methodology
in a separate append-only ledger. Later site builds reuse those values. The deploy job uses the
official GitHub Pages artifact flow and the `github-pages` environment. Current GitHub guidance
requires `pages: write`, `id-token: write`, an explicit build dependency, and the Pages deployment
environment:
https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages

After Pages accepts the artifact, the workflow writes a deployment receipt containing the source
release timestamp, publication/deployment timestamps, page URL, workflow URL, commit, append
counts, and release-to-public latency. More than 14,400 seconds fails the job but the receipt is
still uploaded. Preserve the two successful weekly workflow URLs, state-artifact IDs, Pages URLs,
and receipt artifacts in the eventual `MILESTONE_REPORTS/M2.md`.

## Activation checklist

1. **PASS:** Configure Q-001's repository and push the reviewed default branch.
2. **PASS:** Enable GitHub Pages with GitHub Actions as its source and protect the `github-pages`
   environment to the default branch.
3. **PASS:** Confirm Actions can read prior run artifacts and write Pages deployments. No paid secret is
   required for the M2 Board/Census path.
4. **PASS:** The one permitted manual `all` dispatch used `bootstrap_state=true` and
   `force_publish=false`; its candidate, deployment-accepted state, feed, permalinks, manifest, and
   receipt were inspected. Never enable `bootstrap_state` again.
5. **PASS:** Confirm both weekday schedules remain enabled on the default branch.
6. **PENDING:** Start the two-cycle clock only from the first genuinely scheduled Friday
   prediction deployment.

Pause/retry rules: cancel before deploy if source validation fails; retry the same workflow after a
transient network failure; never skip a failed parser/data-quality gate; never delete/edit ledger
rows; and use manual `force_publish` only to redeploy an already accepted immutable artifact.
