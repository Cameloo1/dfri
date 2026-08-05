"""CLI and stable report writer for the M3 attribution engine."""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

from dfri.attribution.engine import DEFAULT_DRAWS, DEFAULT_SEED, run_attribution
from dfri.attribution.registry import load_attribution_bundle


def write_attribution_report(
    output: Path,
    *,
    draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
) -> Path:
    result = run_attribution(load_attribution_bundle(), draws=draws, seed=seed)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(result.payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/dfri_companies.json"))
    parser.add_argument("--draws", type=int, default=DEFAULT_DRAWS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = write_attribution_report(args.output, draws=args.draws, seed=args.seed)
    print(json.dumps({"status": "PASS", "output": str(path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
