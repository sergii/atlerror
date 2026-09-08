#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
CONCEPT_SCHEMA = ROOT / "schema" / "concept.schema.json"
RULE_SCHEMA = ROOT / "schema" / "rule.schema.json"
CLAIM_SCHEMA = ROOT / "schema" / "claim.schema.json"
EXPERIMENT_SCHEMA = ROOT / "schema" / "experiment.schema.json"

REFERENCE_FIELDS = {
    "may_indicate",
    "tested_by",
    "produces",
    "requires",
    "preferred_tools",
    "prerequisites",
    "related_to",
}


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def knowledge_files() -> list[Path]:
    return sorted((ROOT / "knowledge").rglob("*.yaml"))


def rule_files() -> list[Path]:
    return sorted((ROOT / "rules").rglob("*.yaml"))


def claim_files() -> list[Path]:
    return sorted((ROOT / "claims").rglob("*.yaml"))


def experiment_files() -> list[Path]:
    return sorted((ROOT / "experiments").rglob("*.yaml"))


def registry_ids() -> set[str]:
    ids: set[str] = set()

    capabilities = load_yaml(ROOT / "vocabulary" / "capabilities.yaml")
    for item in capabilities.get("capabilities", []):
        ids.add(item["id"])

    tools = load_yaml(ROOT / "vocabulary" / "tools.yaml")
    for item in tools.get("tools", []):
        ids.add(item["id"])

    return ids


def concept_references(concept: dict[str, Any]) -> Iterable[str]:
    for field in REFERENCE_FIELDS:
        for reference in concept.get(field, []):
            yield reference

    for prediction in concept.get("predictions", []):
        yield prediction["observation"]

    for falsifier in concept.get("falsifiers", []):
        yield falsifier["observation"]


def validate() -> None:
    concept_validator = Draft202012Validator(load_json(CONCEPT_SCHEMA))
    rule_validator = Draft202012Validator(load_json(RULE_SCHEMA))
    claim_validator = Draft202012Validator(load_json(CLAIM_SCHEMA))
    experiment_validator = Draft202012Validator(load_json(EXPERIMENT_SCHEMA))

    concepts: dict[str, dict[str, Any]] = {}
    claims: dict[str, dict[str, Any]] = {}
    experiments: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for path in knowledge_files():
        document = load_yaml(path)
        for error in sorted(concept_validator.iter_errors(document), key=lambda item: list(item.path)):
            errors.append(f"{path.relative_to(ROOT)}: schema: {error.message}")

        concept_id = document.get("id")
        if concept_id in concepts:
            errors.append(f"{path.relative_to(ROOT)}: duplicate id: {concept_id}")
        else:
            concepts[concept_id] = document

    known_concept_ids = set(concepts) | registry_ids()

    for concept_id, concept in concepts.items():
        for reference in concept_references(concept):
            if reference not in known_concept_ids:
                errors.append(f"{concept_id}: unresolved reference: {reference}")

    for path in claim_files():
        document = load_yaml(path)
        for error in sorted(claim_validator.iter_errors(document), key=lambda item: list(item.path)):
            errors.append(f"{path.relative_to(ROOT)}: schema: {error.message}")

        claim_id = document.get("id")
        if claim_id in claims or claim_id in known_concept_ids:
            errors.append(f"{path.relative_to(ROOT)}: duplicate id: {claim_id}")
        else:
            claims[claim_id] = document

        hypothesis = document.get("hypothesis")
        if hypothesis not in concepts:
            errors.append(f"{path.relative_to(ROOT)}: unresolved hypothesis: {hypothesis}")

        for prediction in document.get("predictions", []):
            observation = prediction.get("observation")
            if observation not in concepts:
                errors.append(f"{path.relative_to(ROOT)}: unresolved observation: {observation}")

    for path in experiment_files():
        document = load_yaml(path)
        for error in sorted(experiment_validator.iter_errors(document), key=lambda item: list(item.path)):
            errors.append(f"{path.relative_to(ROOT)}: schema: {error.message}")

        experiment_id = document.get("id")
        if experiment_id in experiments or experiment_id in known_concept_ids or experiment_id in claims:
            errors.append(f"{path.relative_to(ROOT)}: duplicate id: {experiment_id}")
        else:
            experiments[experiment_id] = document

        for claim_id in document.get("claims", []):
            if claim_id not in claims:
                errors.append(f"{path.relative_to(ROOT)}: unresolved claim: {claim_id}")

        build_context = document.get("runner", {}).get("build_context")
        if build_context:
            context_path = (ROOT / build_context).resolve()
            try:
                context_path.relative_to(ROOT)
            except ValueError:
                errors.append(f"{path.relative_to(ROOT)}: build context escapes repository: {build_context}")
            else:
                if not (context_path / "Dockerfile").exists():
                    errors.append(f"{path.relative_to(ROOT)}: Dockerfile missing in build context: {build_context}")

    for path in rule_files():
        document = load_yaml(path)
        for error in sorted(rule_validator.iter_errors(document), key=lambda item: list(item.path)):
            errors.append(f"{path.relative_to(ROOT)}: schema: {error.message}")

        hypothesis = document.get("hypothesis")
        observation = document.get("when", {}).get("observation")

        if hypothesis not in concepts:
            errors.append(f"{path.relative_to(ROOT)}: unresolved hypothesis: {hypothesis}")
        if observation not in concepts:
            errors.append(f"{path.relative_to(ROOT)}: unresolved observation: {observation}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        raise SystemExit(1)

    print(
        "Validated "
        f"{len(concepts)} concepts, {len(claims)} claims, "
        f"{len(experiments)} experiments, and {len(rule_files())} rules "
        "with no unresolved references."
    )


if __name__ == "__main__":
    validate()
