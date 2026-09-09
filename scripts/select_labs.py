#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS_DIR = ROOT / "experiments"

FULL_MATRIX_INPUTS = {
    "scripts/run_lab.py",
    "schema/experiment.schema.json",
    "schema/empirical-evidence.schema.json",
}

SMOKE_INPUTS = {
    ".github/workflows/lab.yml",
    "scripts/select_labs.py",
}


@dataclass(frozen=True)
class Lab:
    name: str
    manifest: str
    result: str
    dependency_roots: tuple[str, ...]


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def repo_relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def normalize_root(path: str) -> str:
    return Path(path).as_posix().rstrip("/") + "/"


def discover_labs() -> list[Lab]:
    labs: list[Lab] = []

    for manifest_path in sorted(EXPERIMENTS_DIR.rglob("*.yaml")):
        manifest = load_yaml(manifest_path)
        if not isinstance(manifest, dict):
            continue

        experiment_id = manifest.get("id")
        runner = manifest.get("runner")
        if not isinstance(experiment_id, str) or not isinstance(runner, dict):
            continue

        manifest_rel = repo_relative(manifest_path)
        dependency_roots: set[str] = set()

        build_context = runner.get("build_context")
        if isinstance(build_context, str):
            dependency_roots.add(normalize_root(build_context))

        compose_file = runner.get("compose_file")
        if isinstance(compose_file, str):
            dependency_roots.add(normalize_root(str(Path(compose_file).parent)))

        name = experiment_id.removeprefix("experiment.")
        labs.append(
            Lab(
                name=name,
                manifest=manifest_rel,
                result=f"{experiment_id}.json",
                dependency_roots=tuple(sorted(dependency_roots)),
            )
        )

    return labs


def git_changed_files(base: str, head: str) -> list[str]:
    if not base or set(base) == {"0"}:
        command = ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", head]
    else:
        command = ["git", "diff", "--name-only", "--diff-filter=ACMRD", base, head]

    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        raise SystemExit(completed.returncode)

    return sorted({line.strip() for line in completed.stdout.splitlines() if line.strip()})


def path_is_under(path: str, root: str) -> bool:
    return path == root.rstrip("/") or path.startswith(root)


def select_labs(labs: list[Lab], changed_files: list[str]) -> list[Lab]:
    changed = set(changed_files)
    if changed & FULL_MATRIX_INPUTS:
        return labs

    selected: dict[str, Lab] = {}
    for lab in labs:
        if lab.manifest in changed:
            selected[lab.manifest] = lab
            continue

        if any(
            path_is_under(changed_path, dependency_root)
            for changed_path in changed_files
            for dependency_root in lab.dependency_roots
        ):
            selected[lab.manifest] = lab

    if changed & SMOKE_INPUTS and labs:
        smoke_lab = labs[0]
        selected.setdefault(smoke_lab.manifest, smoke_lab)

    return [selected[key] for key in sorted(selected)]


def matrix_json(labs: list[Lab]) -> str:
    include = [
        {"name": lab.name, "manifest": lab.manifest, "result": lab.result}
        for lab in labs
    ]
    return json.dumps({"include": include}, separators=(",", ":"), sort_keys=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select empirical labs affected by a Git diff."
    )
    parser.add_argument("--base", required=True, help="Base commit SHA")
    parser.add_argument("--head", required=True, help="Head commit SHA")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    labs = discover_labs()
    changed_files = git_changed_files(args.base, args.head)
    selected = select_labs(labs, changed_files)

    print(
        f"Changed files: {len(changed_files)}; selected labs: {len(selected)}/{len(labs)}",
        file=sys.stderr,
    )
    for lab in selected:
        print(f"  - {lab.name} ({lab.manifest})", file=sys.stderr)

    print(matrix_json(selected))


if __name__ == "__main__":
    main()
