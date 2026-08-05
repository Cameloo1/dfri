"""Generate the committed deterministic OpenAPI contract."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from dfri.api.app import create_app


class OpenApiDriftError(RuntimeError):
    """The committed OpenAPI contract differs from the generated application contract."""


def render_openapi() -> bytes:
    return (json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n").encode()


def write_openapi(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(render_openapi())
    temporary.replace(path)
    return path


def check_openapi(path: Path) -> Path:
    try:
        committed = path.read_bytes()
    except OSError as exc:
        raise OpenApiDriftError(f"Committed OpenAPI contract is unavailable: {path}") from exc
    if committed != render_openapi():
        raise OpenApiDriftError(
            f"Committed OpenAPI contract is stale; regenerate {path} with api-openapi"
        )
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/openapi-v1.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    path = check_openapi(args.output) if args.check else write_openapi(args.output)
    print(json.dumps({"output": str(path), "status": "PASS"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
