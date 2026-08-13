"""Fail closed when direct dependencies or workflow actions are not exactly pinned."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path
from typing import Final, cast

EXACT_PYTHON: Final = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+$")
REMOTE_ACTION: Final = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?!\./)(?P<action>[^@\s]+)@(?P<revision>[^\s#]+)(?:\s+#.*)?$"
)
FULL_SHA: Final = re.compile(r"^[0-9a-f]{40}$")


class SupplyChainError(RuntimeError):
    """A dependency can move without a reviewed lock or immutable revision."""


def verify_supply_chain(root: Path) -> dict[str, object]:
    """Verify exact direct constraints and lock-backed install surfaces."""

    pyproject_path = root / "pyproject.toml"
    uv_lock = root / "uv.lock"
    package_path = root / "package.json"
    package_lock = root / "package-lock.json"
    for path in (pyproject_path, uv_lock, package_path, package_lock):
        if not path.is_file():
            raise SupplyChainError(f"Missing required dependency contract: {path.name}")

    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = cast(dict[str, object], pyproject.get("project"))
    build = cast(dict[str, object], pyproject.get("build-system"))
    groups = cast(dict[str, object], pyproject.get("dependency-groups"))
    python_dependencies = [
        *cast(list[str], project.get("dependencies")),
        *cast(list[str], build.get("requires")),
        *cast(list[str], groups.get("dev")),
    ]
    floating_python = [item for item in python_dependencies if EXACT_PYTHON.fullmatch(item) is None]
    if floating_python:
        raise SupplyChainError(
            "Direct Python dependencies must use exact == pins: " + ", ".join(floating_python)
        )

    package = _json_object(package_path)
    node_dependencies = {
        **_string_map(package.get("dependencies"), "dependencies"),
        **_string_map(package.get("devDependencies"), "devDependencies"),
    }
    floating_node = [
        f"{name}@{version}"
        for name, version in sorted(node_dependencies.items())
        if _floating_node_version(version)
    ]
    if floating_node:
        raise SupplyChainError(
            "Direct Node dependencies must use exact versions: " + ", ".join(floating_node)
        )
    lock = _json_object(package_lock)
    if lock.get("lockfileVersion") not in {2, 3}:
        raise SupplyChainError("package-lock.json must use npm lockfile version 2 or 3")
    root_package = _json_object_value(cast(dict[str, object], lock.get("packages")), "")
    root_node = {
        **_string_map(root_package.get("dependencies"), "locked dependencies"),
        **_string_map(root_package.get("devDependencies"), "locked devDependencies"),
    }
    if node_dependencies != root_node:
        raise SupplyChainError("package.json dependency pins do not match package-lock.json")

    action_count = 0
    for workflow in sorted((root / ".github" / "workflows").glob("*.yml")):
        for line_number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            match = REMOTE_ACTION.match(line)
            if match is None:
                if line.lstrip().startswith("uses:") and "./" not in line:
                    raise SupplyChainError(
                        f"Unparseable remote action reference: {workflow.name}:{line_number}"
                    )
                continue
            action_count += 1
            revision = match.group("revision")
            if FULL_SHA.fullmatch(revision) is None:
                raise SupplyChainError(
                    f"Workflow action is not pinned to a full commit SHA: "
                    f"{workflow.name}:{line_number}"
                )
    if action_count == 0:
        raise SupplyChainError("No remote GitHub Actions were found to verify")

    lock_text = uv_lock.read_text(encoding="utf-8")
    missing_from_uv = [
        dependency.split("==", 1)[0].split("[", 1)[0].lower().replace("_", "-")
        for dependency in cast(list[str], project.get("dependencies"))
        + cast(list[str], groups.get("dev"))
        if f'name = "{dependency.split("==", 1)[0].split("[", 1)[0].lower().replace("_", "-")}"'
        not in lock_text
    ]
    if missing_from_uv:
        raise SupplyChainError(
            "Direct Python dependencies are absent from uv.lock: " + ", ".join(missing_from_uv)
        )
    return {
        "schema_version": "v1",
        "status": "PASS",
        "python_direct_pins": len(python_dependencies),
        "node_direct_pins": len(node_dependencies),
        "github_action_pins": action_count,
        "locks": ["uv.lock", "package-lock.json"],
    }


def _floating_node_version(value: str) -> bool:
    prefixes = ("^", "~", ">", "<", "*", "workspace:", "file:", "git+")
    return value.startswith(prefixes) or " || " in value


def _json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SupplyChainError(f"Cannot load {path.name}") from exc
    if not isinstance(payload, dict):
        raise SupplyChainError(f"{path.name} must contain a JSON object")
    return cast(dict[str, object], payload)


def _json_object_value(mapping: dict[str, object], key: str) -> dict[str, object]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise SupplyChainError(f"package-lock.json is missing packages[{key!r}]")
    return cast(dict[str, object], value)


def _string_map(value: object, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise SupplyChainError(f"{label} must map dependency names to versions")
    return cast(dict[str, str], value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(verify_supply_chain(args.root.resolve()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
