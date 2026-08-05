.PHONY: bootstrap lint typecheck test replay determinism verify live-smoke board-backfill board-snapshot board-targets census-archive context-history nyfed-history health spot-audit membership-verify filing-facts auto-abs card-trust backtest scoreboard-predict scoreboard-grade publish-scoreboard

AS_OF ?= 2024-01-31
BOARD_START ?= 2015-01-01
BOARD_RELEASE ?= all
BOARD_ARGS ?=
BOARD_TARGET_START ?= 2015-01-01
BOARD_TARGET_ARGS ?=
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

bootstrap:
	uv sync --locked --all-groups

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

verify: lint typecheck test determinism

live-smoke:
	uv run python -m dfri.ingest.verify --output .local/evidence/source-verification.json

board-backfill:
	uv run python -m dfri.ingest.board_backfill --start $(BOARD_START) --release $(BOARD_RELEASE) $(BOARD_ARGS)

board-snapshot:
	uv run python -m dfri.ingest.board_snapshot --start $(BOARD_START) --release $(BOARD_RELEASE)

board-targets:
	uv run python -m dfri.ingest.board_targets --start $(BOARD_TARGET_START) $(BOARD_TARGET_ARGS)

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

publish-scoreboard:
	uv run python -m dfri.publish.site $(PUBLISH_ARGS)
