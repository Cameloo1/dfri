@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "UV_CACHE_DIR=%CD%\.uv-cache"
set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=verify"
set "DFRI_PYTEST_ROOT=%CD%\.local\pytest-run-%RANDOM%-%RANDOM%"
if "%AS_OF%"=="" set "AS_OF=2024-01-31"
if "%BOARD_START%"=="" set "BOARD_START=2015-01-01"
if "%BOARD_RELEASE%"=="" set "BOARD_RELEASE=all"
if "%BOARD_TARGET_START%"=="" set "BOARD_TARGET_START=2015-01-01"
if "%CENSUS_ARCHIVE_START%"=="" set "CENSUS_ARCHIVE_START=2015-01-01"
if "%CONTEXT_START%"=="" set "CONTEXT_START=2015-01-01"
if "%CONTEXT_SOURCE%"=="" set "CONTEXT_SOURCE=all"
if "%NYFED_START%"=="" set "NYFED_START=2015-01-01"
if "%FILING_ROLE%"=="" set "FILING_ROLE=all"
if "%AUTO_ABS_TRUST%"=="" set "AUTO_ABS_TRUST=all"
if "%CARD_TRUST%"=="" set "CARD_TRUST=all"
if "%BACKTEST_AS_OF%"=="" set "BACKTEST_AS_OF=2026-08-04T23:59:00+00:00"
if "%BACKTEST_OUTPUT%"=="" set "BACKTEST_OUTPUT=reports\m2_backtest.json"
if "%BACKTEST_MARKDOWN%"=="" set "BACKTEST_MARKDOWN=reports\M2_BACKTEST.md"
if "%ATTRIBUTION_OUTPUT%"=="" set "ATTRIBUTION_OUTPUT=reports\dfri_companies.json"

if /I "%TARGET%"=="bootstrap" (
  uv sync --locked --all-groups
  if errorlevel 1 exit /b 1
  npm.cmd ci --ignore-scripts
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="privacy-check" (
  uv run python -m dfri.ops.privacy markdown
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="privacy-staged" (
  uv run python -m dfri.ops.privacy excluded-staged
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="lint" (
  uv run ruff check src tests
  if errorlevel 1 exit /b 1
  uv run ruff format --check src tests
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="typecheck" (
  uv run mypy src
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="test" (
  if not exist "%DFRI_PYTEST_ROOT%" mkdir "%DFRI_PYTEST_ROOT%"
  if errorlevel 1 exit /b 1
  uv run pytest --basetemp="%DFRI_PYTEST_ROOT%\base" -o cache_dir="%DFRI_PYTEST_ROOT%\cache"
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="replay" (
  uv run python -m dfri.replay --as-of %AS_OF% --output published\replay
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="determinism" (
  if not exist "%DFRI_PYTEST_ROOT%" mkdir "%DFRI_PYTEST_ROOT%"
  if errorlevel 1 exit /b 1
  uv run pytest --basetemp="%DFRI_PYTEST_ROOT%\base" -o cache_dir="%DFRI_PYTEST_ROOT%\cache" --no-cov tests\integration\test_deterministic_replay.py
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="verify" (
  uv run python -m dfri.ops.privacy markdown
  if errorlevel 1 exit /b 1
  uv run ruff check src tests
  if errorlevel 1 exit /b 1
  uv run ruff format --check src tests
  if errorlevel 1 exit /b 1
  uv run mypy src
  if errorlevel 1 exit /b 1
  if not exist "%DFRI_PYTEST_ROOT%" mkdir "%DFRI_PYTEST_ROOT%"
  if errorlevel 1 exit /b 1
  uv run pytest --basetemp="%DFRI_PYTEST_ROOT%\base" -o cache_dir="%DFRI_PYTEST_ROOT%\cache"
  if errorlevel 1 exit /b 1
  uv run pytest --basetemp="%DFRI_PYTEST_ROOT%\base-determinism" -o cache_dir="%DFRI_PYTEST_ROOT%\cache-determinism" --no-cov tests\integration\test_deterministic_replay.py
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="live-smoke" (
  uv run python -m dfri.ingest.verify --output .local\evidence\source-verification.json
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="board-backfill" (
  uv run python -m dfri.ingest.board_backfill --start %BOARD_START% --release %BOARD_RELEASE% %BOARD_ARGS%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="board-snapshot" (
  uv run python -m dfri.ingest.board_snapshot --start %BOARD_START% --release %BOARD_RELEASE%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="board-targets" (
  uv run python -m dfri.ingest.board_targets --start %BOARD_TARGET_START% %BOARD_TARGET_ARGS%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="census-archive" (
  uv run python -m dfri.ingest.census_archive --start %CENSUS_ARCHIVE_START% %CENSUS_ARCHIVE_ARGS%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="context-history" (
  uv run --env-file .env python -m dfri.ingest.context_history --start %CONTEXT_START% --source %CONTEXT_SOURCE%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="nyfed-history" (
  uv run python -m dfri.ingest.nyfed --start %NYFED_START%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="health" (
  uv run python -m dfri.api.health
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="spot-audit" (
  uv run --env-file .env python -m dfri.ingest.spot_audit
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="membership-verify" (
  uv run python -m dfri.ingest.membership
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="filing-facts" (
  uv run python -m dfri.ingest.filing_facts --role %FILING_ROLE%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="auto-abs" (
  uv run python -m dfri.ingest.auto_abs --trust %AUTO_ABS_TRUST% %AUTO_ABS_ARGS%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="card-trust" (
  uv run python -m dfri.ingest.card_trust --trust %CARD_TRUST% %CARD_TRUST_ARGS%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="backtest" (
  uv run python -m dfri.backtest --as-of %BACKTEST_AS_OF% --output "%BACKTEST_OUTPUT%" --markdown "%BACKTEST_MARKDOWN%"
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="scoreboard-predict" (
  uv run python -m dfri.scoreboard predict %SCOREBOARD_ARGS%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="scoreboard-grade" (
  uv run python -m dfri.scoreboard grade %SCOREBOARD_ARGS%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="attribution" (
  uv run python -m dfri.attribution.pipeline --output "%ATTRIBUTION_OUTPUT%"
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="recompute-check" (
  uv run python -m dfri.attribution.pipeline --output "%ATTRIBUTION_OUTPUT%"
  if errorlevel 1 exit /b 1
  uv run python tools\recompute_check.py --published "%ATTRIBUTION_OUTPUT%"
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="provenance-check" (
  uv run python -m dfri.publish.link_check
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="api-openapi" (
  uv run python -m dfri.api.openapi --output docs\openapi-v1.json
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="api" (
  uv run python -m dfri.api.app
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="site-quality" (
  uv run python -m dfri.publish.quality
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="publish-scoreboard" (
  uv run python -m dfri.ops.privacy excluded-staged
  if errorlevel 1 exit /b 1
  uv run python -m dfri.publish.site %PUBLISH_ARGS%
  exit /b !ERRORLEVEL!
)

if /I "%TARGET%"=="publish" (
  uv run python -m dfri.ops.privacy excluded-staged
  if errorlevel 1 exit /b 1
  uv run python -m dfri.api.openapi --check --output docs\openapi-v1.json
  if errorlevel 1 exit /b 1
  uv run python -m dfri.publish.changelog
  if errorlevel 1 exit /b 1
  uv run python -m dfri.seed.publication --output published\public --evidence .local\evidence\m4-publication.json
  if errorlevel 1 exit /b 1
  uv run python -m dfri.api.benchmark --publication-root published\public --output .local\evidence\m4-api-latency.json
  if errorlevel 1 exit /b 1
  npm.cmd run site-accessibility -- published\public .local\evidence\m4-axe.json
  exit /b !ERRORLEVEL!
)

echo Unknown target: %TARGET% 1>&2
exit /b 2
