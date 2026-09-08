#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_SCHEMA = ROOT / "schema" / "experiment.schema.json"
EVIDENCE_SCHEMA = ROOT / "schema" / "empirical-evidence.schema.json"


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_document(document: Any, schema_path: Path, label: str) -> None:
    validator = Draft202012Validator(load_json(schema_path))
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.path))
    if errors:
        for error in errors:
            print(f"ERROR: {label}: {error.message}", file=sys.stderr)
        raise SystemExit(1)


def ensure_repo_path(relative_path: str) -> Path:
    path = (ROOT / relative_path).resolve()
    try:
        path.relative_to(ROOT)
    except ValueError as exc:
        raise SystemExit(f"ERROR: path escapes repository: {relative_path}") from exc
    return path


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(command))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=capture,
        check=False,
    )
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        raise SystemExit(completed.returncode)
    return completed


def run_docker(runner: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    build_context = ensure_repo_path(runner["build_context"])
    if not (build_context / "Dockerfile").exists():
        raise SystemExit(f"ERROR: Dockerfile not found in {build_context.relative_to(ROOT)}")

    image = runner["image"]
    run(["docker", "build", "-t", image, str(build_context)])
    return run(["docker", "run", "--rm", *runner.get("docker_args", []), image], capture=True)


def run_docker_compose(
    runner: dict[str, Any], experiment_id: str
) -> subprocess.CompletedProcess[str]:
    compose_file = ensure_repo_path(runner["compose_file"])
    if not compose_file.is_file():
        raise SystemExit(f"ERROR: compose file not found: {compose_file.relative_to(ROOT)}")

    project_suffix = experiment_id.replace(".", "-").replace("_", "-")
    project_name = f"atlerror-{project_suffix}"[:63]
    compose = ["docker", "compose", "-p", project_name, "-f", str(compose_file)]

    try:
        run(compose + ["up", "-d", "--build", "--wait", runner["setup_service"]])
        return run(
            compose
            + ["run", "--rm", "--no-deps", runner["evidence_service"]],
            capture=True,
        )
    finally:
        print("+ " + " ".join(compose + ["down", "-v", "--remove-orphans"]))
        subprocess.run(
            compose + ["down", "-v", "--remove-orphans"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )


def execute_runner(
    runner: dict[str, Any], experiment_id: str
) -> subprocess.CompletedProcess[str]:
    if runner["type"] == "docker":
        return run_docker(runner)
    if runner["type"] == "docker_compose":
        return run_docker_compose(runner, experiment_id)
    raise SystemExit(f"ERROR: unsupported runner type: {runner['type']}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/run_lab.py experiments/<path>.yaml")

    manifest_path = ensure_repo_path(sys.argv[1])
    manifest = load_yaml(manifest_path)
    validate_document(manifest, EXPERIMENT_SCHEMA, str(manifest_path.relative_to(ROOT)))

    experiment_id = manifest["id"]
    completed = execute_runner(manifest["runner"], experiment_id)

    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not output_lines:
        raise SystemExit("ERROR: experiment produced no evidence JSON")

    try:
        evidence = json.loads(output_lines[-1])
    except json.JSONDecodeError as exc:
        print(completed.stdout)
        raise SystemExit("ERROR: final stdout line is not valid evidence JSON") from exc

    validate_document(evidence, EVIDENCE_SCHEMA, f"evidence from {experiment_id}")

    if evidence["experiment"] != experiment_id:
        raise SystemExit(
            f"ERROR: evidence experiment {evidence['experiment']} does not match manifest {experiment_id}"
        )

    if set(evidence["claims"]) != set(manifest["claims"]):
        raise SystemExit("ERROR: evidence claims do not match experiment manifest claims")

    if evidence["result"] != manifest["expected_result"]:
        raise SystemExit(
            f"ERROR: experiment result {evidence['result']} does not match expected {manifest['expected_result']}"
        )

    results_dir = ROOT / "lab-results"
    results_dir.mkdir(exist_ok=True)
    result_path = results_dir / f"{experiment_id}.json"
    result_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(evidence, sort_keys=True))
    print(f"Evidence written to {result_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
