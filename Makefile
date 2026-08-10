.PHONY: bootstrap privacy-check privacy-staged registries-check criticality-check supply-chain-contract vulnerability-scan supply-chain archive-round-trip lint typecheck test replay determinism verify live-smoke board-backfill board-snapshot board-targets treasury-mts mts-backtest mts-predict mts-grade census-archive context-history nyfed-history health spot-audit membership-verify filing-facts auto-abs card-trust backtest scoreboard-predict scoreboard-grade attribution quarterly-refresh recompute-check provenance-check api-openapi api site-quality publish-scoreboard publish

AS_OF ?= 2024-01-31
BOARD_START ?= 2015-01-01
BOARD_RELEASE ?= all
BOARD_ARGS ?=
BOARD_TARGET_START ?= 2015-01-01
BOARD_TARGET_ARGS ?=
MTS_START ?= 2017-12-31
MTS_ARGS ?=
MTS_BACKTEST_AS_OF ?= 2026-08-10T23:59:00+00:00
CENSUS_ARCHIVE_START ?= 2015-01-01
CENSUS_ARCHIVE_ARGS ?=
CONTEXT_START ?= 2015-01-01
CONTEXT_SOURCE ?= all
NYFED_START ?= 2015-01-01
FILING_ROLE ?= all
AUTO_ABS_TRUST ?= all
AUTO_ABS_ARGS ?=
CARD_TRUST ?= all
CARD_TRUST_ARGS ?=
BACKTEST_AS_OF ?= 2026-08-04T23:59:00+00:00
BACKTEST_OUTPUT ?= reports/m2_backtest.json
BACKTEST_MARKDOWN ?= reports/M2_BACKTEST.md
SCOREBOARD_ARGS ?=
PUBLISH_ARGS ?=
ATTRIBUTION_OUTPUT ?= reports/dfri_companies.json
ATTRIBUTION_REFRESH_ARGS ?=

bootstrap:
	uv sync --locked --all-groups
	npm ci --ignore-scripts

privacy-check:
	uv run python -m dfri.ops.privacy markdown

privacy-staged:
	uv run python -m dfri.ops.privacy excluded-staged

registries-check:
	uv run python tools/build_m5_registries.py --check

criticality-check:
	uv run python scripts/sync_assumption_criticality.py --check
	uv run python -m dfri.attribution.criticality --check

supply-chain-contract:
	uv run python -m dfri.ops.supply_chain

vulnerability-scan:
	uv run pip-audit --cache-dir .local/pip-audit-cache --local --skip-editable
	npm audit --audit-level=high

supply-chain: supply-chain-contract vulnerability-scan

archive-round-trip:
	uv run python -m dfri.ops.archive round-trip --archive .local/archive/dfri-ledger.tar.gz

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run mypy src

test:
	uv run pytest

replay:
	uv run python -m dfri.replay --as-of $(AS_OF) --output published/replay

determinism:
	uv run pytest --no-cov tests/integration/test_deterministic_replay.py

verify: privacy-check registries-check criticality-check supply-chain-contract lint typecheck test determinism

live-smoke:
	uv run python -m dfri.ingest.verify --output .local/evidence/source-verification.json

board-backfill:
	uv run python -m dfri.ingest.board_backfill --start $(BOARD_START) --release $(BOARD_RELEASE) $(BOARD_ARGS)

board-snapshot:
	uv run python -m dfri.ingest.board_snapshot --start $(BOARD_START) --release $(BOARD_RELEASE)

board-targets:
	uv run python -m dfri.ingest.board_targets --start $(BOARD_TARGET_START) $(BOARD_TARGET_ARGS)

treasury-mts:
	uv run python -m dfri.ingest.treasury_mts --start $(MTS_START) $(MTS_ARGS)

mts-backtest:
	uv run python -m dfri.mts_backtest --as-of $(MTS_BACKTEST_AS_OF) --output reports/mts_backtest.json

mts-predict:
	uv run python -m dfri.scoreboard mts-predict $(SCOREBOARD_ARGS)

mts-grade:
	uv run python -m dfri.scoreboard mts-grade $(SCOREBOARD_ARGS)

census-archive:
	uv run python -m dfri.ingest.census_archive --start $(CENSUS_ARCHIVE_START) $(CENSUS_ARCHIVE_ARGS)

context-history:
	uv run --env-file .env python -m dfri.ingest.context_history --start $(CONTEXT_START) --source $(CONTEXT_SOURCE)

nyfed-history:
	uv run python -m dfri.ingest.nyfed --start $(NYFED_START)

health:
	uv run python -m dfri.api.health

spot-audit:
	uv run --env-file .env python -m dfri.ingest.spot_audit

membership-verify:
	uv run python -m dfri.ingest.membership

filing-facts:
	uv run python -m dfri.ingest.filing_facts --role $(FILING_ROLE)

auto-abs:
	uv run python -m dfri.ingest.auto_abs --trust $(AUTO_ABS_TRUST) $(AUTO_ABS_ARGS)

card-trust:
	uv run python -m dfri.ingest.card_trust --trust $(CARD_TRUST) $(CARD_TRUST_ARGS)

backtest:
	uv run python -m dfri.backtest --as-of $(BACKTEST_AS_OF) --output $(BACKTEST_OUTPUT) --markdown $(BACKTEST_MARKDOWN)

scoreboard-predict:
	uv run python -m dfri.scoreboard predict $(SCOREBOARD_ARGS)

scoreboard-grade:
	uv run python -m dfri.scoreboard grade $(SCOREBOARD_ARGS)

attribution:
	uv run python -m dfri.attribution.pipeline --output $(ATTRIBUTION_OUTPUT)

quarterly-refresh:
	uv run python -m dfri.ops.quarterly_refresh $(ATTRIBUTION_REFRESH_ARGS)

recompute-check: attribution
	uv run python tools/recompute_check.py --published $(ATTRIBUTION_OUTPUT)

provenance-check:
	uv run python -m dfri.publish.link_check

api-openapi:
	uv run python -m dfri.api.openapi --output docs/openapi-v1.json

api:
	uv run python -m dfri.api.app

site-quality:
	uv run python -m dfri.publish.quality

publish-scoreboard: privacy-staged
	uv run python -m dfri.publish.site $(PUBLISH_ARGS)

publish: privacy-staged registries-check criticality-check supply-chain-contract
	uv run python -m dfri.api.openapi --check --output docs/openapi-v1.json
	uv run python -m dfri.publish.changelog
	uv run python -m dfri.seed.publication --output published/public --evidence .local/evidence/m4-publication.json
	uv run python -m dfri.api.benchmark --publication-root published/public --output .local/evidence/m4-api-latency.json
	npm run site-accessibility -- published/public .local/evidence/m4-axe.json
