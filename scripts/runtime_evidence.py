#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schema" / "runtime-evidence.schema.json"


def parse_timestamp(value: str, label: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset: {value}")
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_runtime_evidence(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise ValueError("runtime evidence document must be an object")

    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ValueError(f"runtime evidence schema validation failed: {details}")
    return document


def validate_runtime_references(
    document: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
) -> None:
    instance_ids: set[str] = set()
    errors: list[str] = []

    for instance in document["instances"]:
        instance_id = instance["id"]
        if instance_id in instance_ids:
            errors.append(f"duplicate runtime evidence instance id: {instance_id}")
        instance_ids.add(instance_id)

        observation_id = instance["observation"]
        observation = concepts.get(observation_id)
        if observation is None:
            errors.append(f"{instance_id}: unknown observation concept: {observation_id}")
        elif observation.get("kind") != "observation":
            errors.append(f"{instance_id}: evidence must reference an observation concept: {observation_id}")

        scope = instance.get("scope", {})
        for entity_id in scope.get("entities", []):
            entity = concepts.get(entity_id)
            if entity is None:
                errors.append(f"{instance_id}: unknown scope entity: {entity_id}")
            elif entity.get("kind") != "system_entity":
                errors.append(f"{instance_id}: scope entity must be a system_entity: {entity_id}")

        for boundary_id in scope.get("boundaries", []):
            boundary = concepts.get(boundary_id)
            if boundary is None:
                errors.append(f"{instance_id}: unknown scope boundary: {boundary_id}")
            elif boundary.get("kind") != "boundary":
                errors.append(f"{instance_id}: scope boundary must be a boundary: {boundary_id}")

        observed_at = parse_timestamp(instance["observed_at"], f"{instance_id}.observed_at")
        expires_at_raw = instance.get("expires_at")
        if expires_at_raw is not None:
            expires_at = parse_timestamp(expires_at_raw, f"{instance_id}.expires_at")
            if expires_at <= observed_at:
                errors.append(f"{instance_id}: expires_at must be later than observed_at")

    if errors:
        raise ValueError("; ".join(errors))


def _instance_ref(instance: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": instance["id"],
        "observation": instance["observation"],
        "state": instance["state"],
        "confidence": instance["confidence"],
    }


def resolve_runtime_evidence(
    document: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
    *,
    as_of: datetime,
    source_path: str | None = None,
) -> tuple[set[str], set[str], dict[str, Any]]:
    validate_runtime_references(document, concepts)
    as_of = as_of.astimezone(timezone.utc)

    active: list[dict[str, Any]] = []
    stale_ids: list[str] = []
    future_ids: list[str] = []
    states_by_observation: dict[str, set[str]] = {}

    for instance in document["instances"]:
        observed_at = parse_timestamp(instance["observed_at"], f"{instance['id']}.observed_at")
        expires_at_raw = instance.get("expires_at")
        expires_at = (
            parse_timestamp(expires_at_raw, f"{instance['id']}.expires_at")
            if expires_at_raw is not None
            else None
        )

        if observed_at > as_of:
            future_ids.append(instance["id"])
            continue
        if expires_at is not None and expires_at <= as_of:
            stale_ids.append(instance["id"])
            continue

        active.append(instance)
        states_by_observation.setdefault(instance["observation"], set()).add(instance["state"])

    conflicts = sorted(
        observation
        for observation, states in states_by_observation.items()
        if len(states) > 1
    )
    if conflicts:
        raise ValueError(
            "active runtime evidence contains contradictory states for: " + ", ".join(conflicts)
        )

    observed = {
        observation
        for observation, states in states_by_observation.items()
        if states == {"observed"}
    }
    absent = {
        observation
        for observation, states in states_by_observation.items()
        if states == {"absent"}
    }

    context: dict[str, Any] = {
        "incident_id": document["incident_id"],
        "as_of": format_timestamp(as_of),
        "active_instances": sorted(
            (_instance_ref(instance) for instance in active),
            key=lambda item: item["id"],
        ),
        "stale_instance_ids": sorted(stale_ids),
        "future_instance_ids": sorted(future_ids),
    }
    if source_path is not None:
        context["source_path"] = source_path

    return observed, absent, context


def load_concepts(root: Path) -> dict[str, dict[str, Any]]:
    concepts: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "knowledge").rglob("*.yaml")):
        with path.open("r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        if not isinstance(document, dict):
            continue
        concept_id = document.get("id")
        if isinstance(concept_id, str):
            concepts[concept_id] = document
    return concepts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and resolve runtime evidence instances into active observation state."
    )
    parser.add_argument("path", type=Path, help="Runtime evidence YAML or JSON document")
    parser.add_argument(
        "--as-of",
        required=True,
        help="ISO 8601 time used to classify active, stale, and future evidence",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        as_of = parse_timestamp(args.as_of, "--as-of")
        document = load_runtime_evidence(args.path)
        concepts = load_concepts(root)
        observed, absent, context = resolve_runtime_evidence(
            document,
            concepts,
            as_of=as_of,
            source_path=str(args.path),
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    output = {
        "schema_version": "0.1",
        "kind": "runtime_evidence_context",
        "observed": sorted(observed),
        "absent": sorted(absent),
        **context,
    }
    indent = 2 if args.pretty else None
    print(json.dumps(output, indent=indent, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
