from __future__ import annotations

import re
from pathlib import Path

PINNED_ACTION = re.compile(
    r"^\s*(?:-\s*)?uses:\s+[^\s@]+@[0-9a-f]{40}(?:\s+#\s+v\S+)?\s*$", re.MULTILINE
)


def assert_all_actions_are_commit_pinned(workflow: str) -> None:
    uses_lines = [line for line in workflow.splitlines() if line.lstrip(" -").startswith("uses:")]
    assert uses_lines
    assert len(PINNED_ACTION.findall(workflow)) == len(uses_lines)


def test_m2_workflow_preserves_the_clock_and_pages_gates() -> None:
    root = Path(__file__).parents[2]
    workflow = (root / ".github" / "workflows" / "m2-scoreboard.yml").read_text()

    assert 'cron: "17 16 * * 1-5"' in workflow
    assert 'cron: "17 19 * * 1-5"' in workflow
    assert 'cron: "17 21 * * 1-5"' in workflow
    assert 'cron: "17 23 * * 1-5"' in workflow
    assert 'cron: "43 14 * * 1"' in workflow
    assert "cancel-in-progress: false" in workflow
    assert "github.event.repository.default_branch" in workflow
    assert "dfri.ops.state_bundle unpack" in workflow
    assert "dfri.ops.state_bundle pack" in workflow
    assert "No retained runtime cache is available" in workflow
    assert "dfri.ops.repository_ledger restore" in workflow
    assert "dfri.ops.repository_ledger snapshot" in workflow
    assert "dfri.ops.repository_ledger merge" in workflow
    assert "state/ledgers" in workflow
    assert "git diff --cached --name-status" in workflow
    assert 'git push origin "HEAD:${GITHUB_REF_NAME}"' in workflow
    assert "could not persist accepted ledger state to Git after three attempts" in workflow
    assert "dfri-m2-state-candidate" in workflow
    assert "Preserve deployment-accepted runtime state" in workflow
    assert "bootstrap_state" in workflow
    assert "steps.clock.outputs.publish == 'true'" in workflow
    assert "--publication-mode live" in workflow
    assert "--minimum-made-at" not in workflow
    assert "actions/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d # v6.0.0" in workflow
    assert (
        "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9 # v5.0.0"
        in workflow
    )
    assert "actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128 # v5.0.0" in workflow
    assert (
        workflow.count("actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1")
        == 4
    )
    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1" in workflow
    deploy = workflow.index("Deploy the accepted Pages artifact")
    checkout = workflow.index(
        "Check out current default branch for ledger promotion and receipt writer"
    )
    candidate_download = workflow.index("Download the candidate runtime state")
    accepted_state = workflow.index("Preserve deployment-accepted runtime state")
    repository_merge = workflow.index(
        "Verify and merge the deployment-accepted repository ledger candidate"
    )
    repository_commit = workflow.index("Commit newly appended public ledger batches to Git")
    receipt = workflow.index(
        "Write the deployment receipt and enforce the applicable four-hour SLA"
    )
    assert (
        deploy
        < checkout
        < candidate_download
        < accepted_state
        < repository_merge
        < repository_commit
        < receipt
    )
    assert "pages: write" in workflow and "id-token: write" in workflow
    assert "contents: write" in workflow
    assert "dfri.ops.deployment_receipt" in workflow
    assert "dfri.ops.quarterly_refresh" in workflow
    assert "attribution_refresh_appended" in workflow
    assert "options: [predict, grade, mts-predict, mts-grade, refresh, all]" in workflow
    assert "make MTS_START=2017-12-31 treasury-mts" in workflow
    assert "python -m dfri.mts_backtest" in workflow
    assert "python -m dfri.scoreboard mts-predict" in workflow
    assert "retention-days: 90" in workflow
    assert "FRED" not in workflow and "ALFRED" not in workflow
    assert_all_actions_are_commit_pinned(workflow)


def test_ci_uses_the_current_pinned_uv_contract() -> None:
    root = Path(__file__).parents[2]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text()

    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1" in workflow
    assert "fetch-depth: 0" in workflow
    assert "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b" in workflow
    assert 'version: "0.11.32"' in workflow
    assert "Scan complete Git history for secrets" in workflow
    assert 'GITLEAKS_VERSION: "8.30.1"' in workflow
    assert "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" in workflow
    assert '--log-opts="HEAD --remotes=origin --tags --full-history"' in workflow
    assert "--redact=100" in workflow
    assert "Block local paths and excluded documents" in workflow
    assert "dfri.ops.privacy markdown" in workflow
    assert "dfri.ops.privacy excluded-tracked" in workflow
    assert "make verify" in workflow
    assert "make publish" in workflow
    assert "Assert rendered publication rules" in workflow
    assert "make site-quality" in workflow
    assert "Verify published changelog history is append-only" in workflow
    assert "Start the M5 end-to-end pipeline timer" in workflow
    assert "elapsed < 1800" in workflow
    assert "m5-ci-performance.json" in workflow
    assert "publication-verification" in workflow
    assert "retention-days: 90" in workflow
    assert_all_actions_are_commit_pinned(workflow)


def test_m4_uptime_workflow_preserves_partial_api_and_owner_log_contract() -> None:
    root = Path(__file__).parents[2]
    workflow = (root / ".github" / "workflows" / "m4-uptime.yml").read_text()

    assert 'cron: "17 * * * *"' in workflow
    assert "cancel-in-progress: false" in workflow
    assert "DFRI_API_BASE_URL" in workflow
    assert "--require-api" in workflow
    assert "https://cameloo1.github.io/dfri/" in workflow
    assert "dfri.ops.uptime" in workflow
    assert "if: always()" in workflow
    assert "m4-uptime-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "retention-days: 90" in workflow
    assert_all_actions_are_commit_pinned(workflow)
