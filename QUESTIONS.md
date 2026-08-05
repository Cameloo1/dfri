# DFRI Review Questions

Non-blocking questions accumulate here for the Day-14 review. None currently block M0 execution.

| ID | Date | Area | Question | Build impact | Current action |
|---|---|---|---|---|---|
| Q-001 | 2026-08-04 | GitHub/public hosting | **RESOLVED:** Which Camelon Systems GitHub repository and public hostname should receive DFRI? | The canonical public destinations are `Cameloo1/dfri` and `https://cameloo1.github.io/dfri/`. | Keep the full local history in the primary workspace and publish only inspected filtered-lane commits. The active clock and manual bootstrap are verified; do not count the bootstrap as a live cycle. |
| Q-002 | 2026-08-05 | Public API hosting | **RESOLVED by D-010:** Is a public FastAPI runtime warranted for v1? | No. Versioned Pages feeds satisfy current access needs, so every API-specific M4 criterion is deferred rather than passed. | Keep the tested implementation dormant. Revisit when an external party requests API access or M5 feed size becomes unwieldy; then deploy from CI to `api.dfri.camelon.app`, Cloudflare Workers first and Fly.io auto-stop only if runtime compatibility requires it. |
