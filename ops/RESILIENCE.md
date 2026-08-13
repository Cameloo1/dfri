# Scheduled publication resilience

This runbook governs the G.19, H.8, Treasury MTS, quarterly attribution, status-refresh, and public
uptime lanes. A failed or manually replayed run never receives scheduled-cycle credit. Preserve the
first failure receipt and workflow URL before retrying.

## Read state first

1. Open `v1/status.json` on the public site and identify the exact job, last success, expected next
   run, release deadline, and missed flags.
2. Open the linked GitHub Actions run and the deduplicated automation incident. Download its
   receipts before the 90-day artifact cache expires; the prediction, grade, and publication
   ledgers themselves remain authoritative in `state/ledgers/`.
3. Verify the default-branch workflow revision and the public source release independently. Do not
   infer a release from a cron expression.
4. Pause publication if an attempted recovery would change an existing ledger row, consume a new
   credential, alter source terms, or publish data that did not pass the Vintage Guard.

## Scheduled job did not fire

- Inspect repository Actions status, workflow registration, default branch, cron expression,
  concurrency queue, billing/Actions availability, and the next time in `v1/status.json`.
- If the workflow fired late but used only data available at its actual `made_at`, let it finish and
  preserve the late timestamp. If it never fired, dispatch the same job only to restore service;
  label the run manual and do not count it as the missed live cycle.
- A successful retry must append no conflicting row, deploy Pages before ledger promotion, record
  its run URL, and close or update the incident with the recovery evidence.
- Abort if the only proposed recovery backdates `made_at`, fabricates a release, or rewrites a
  prediction. Restart the live-cycle count when the milestone rules require it.

## Source unreachable

- Preserve the URL, response class/status, retrieval time, retry count, and source contract. Retry
  only through the bounded client backoff already registered for that source.
- If a registered independent fallback is permitted and available, activate it through the
  assumption registry, widen the affected band, append a `source_fallback` changelog event, and
  publish the visible fallback note. Never substitute an unregistered source in the workflow.
- Skip only the affected append when no release is verifiably available. Keep the last accepted
  public state and status banner; do not republish an invented zero or stale value as current.

## Release format changed

- Save the smallest legally redistributable failing response behind the ignored local evidence
  boundary. Compare title, units, identifiers, headers, table shape, and first-print markers with
  the pinned registry and last accepted fixture.
- Pause ingestion before any append. Update the parser only with a real dated fixture and a test
  proving both the old accepted shape and the new source shape. Reverify the live endpoint, units,
  terms URL, and point-in-time boundary before retrying.
- Abort and leave a `BLOCKED` record if the first print can no longer be distinguished from revised
  data or if the new access path changes redistribution terms.

## Primary and fallback both unavailable

- Treat this as an explicit structural outage. Keep the last accepted ledger and public estimate,
  mark the assumption/status stale, and open or update the automation incident.
- Do not chain to a third source unless it has already passed legality, identity, unit,
  independence, and sensitivity review and is registered as a fallback.
- Recovery requires one permitted registered source, a successful deterministic recompute, visible
  source-state disclosure, and a no-mutation proof for all prior predictions and publications.

## Status-only publication recovery

The hourly uptime workflow mirrors every file named by the accepted public `manifest.json`, checks
each byte against its SHA-256, updates only `v1/status.json`, `status/banner.html`, and the manifest,
then deploys that candidate. If any other byte moves, abort. Re-running the status refresh is safe;
it reconstructs success evidence from the prior public status snapshot and does not touch a model,
feed row, prediction, grade, methodology, or publication timestamp.
