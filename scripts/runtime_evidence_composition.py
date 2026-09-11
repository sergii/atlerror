#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from causal_projection import ROOT, load_concepts
from runtime_evidence import SCHEMA_PATH as RUNTIME_EVIDENCE_SCHEMA_PATH
from runtime_evidence import load_runtime_evidence, validate_runtime_references


def validate_runtime_evidence_document(
    document: dict[str, Any],
    *,
    schema_path: Path = RUNTIME_EVIDENCE_SCHEMA_PATH,
) -> None:
    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(
            "runtime evidence schema validation failed: "
            + "; ".join(error.message for error in errors)
        )


def compose_runtime_evidence(
    documents: list[dict[str, Any]],
    concepts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if len(documents) < 2:
        raise ValueError("runtime evidence composition requires at least two source documents")

    incident_ids: set[str] = set()
    instances_by_id: dict[str, dict[str, Any]] = {}

    for index, document in enumerate(documents, start=1):
        if not isinstance(document, dict):
            raise ValueError(f"runtime evidence source {index} must be an object")
        validate_runtime_evidence_document(document)
        validate_runtime_references(document, concepts)
        incident_ids.add(document["incident_id"])

        for instance in document["instances"]:
            instance_id = instance["id"]
            existing = instances_by_id.get(instance_id)
            if existing is None:
                instances_by_id[instance_id] = copy.deepcopy(instance)
                continue
            if existing != instance:
                raise ValueError(
                    "runtime evidence instance id collision with different content: "
                    + instance_id
                )

    if len(incident_ids) != 1:
        raise ValueError(
            "runtime evidence sources must use the same incident_id: "
            + ", ".join(sorted(incident_ids))
        )

    incident_id = next(iter(incident_ids))
    output: dict[str, Any] = {
        "schema_version": "0.1",
        "kind": "runtime_evidence",
        "incident_id": incident_id,
        "description": (
            f"Composed runtime evidence for {incident_id} from {len(documents)} source documents."
        ),
        "instances": sorted(
            instances_by_id.values(),
            key=lambda instance: instance["id"],
        ),
    }
    validate_runtime_evidence_document(output)
    validate_runtime_references(output, concepts)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compose multiple Atlerror runtime evidence bundles for one incident without "
            "changing instance provenance or semantic scope."
        )
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Runtime evidence YAML or JSON documents to compose",
    )
    parser.add_argument("--format", choices=["yaml", "json"], default="yaml")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    if len(args.paths) < 2:
        parser.error("provide at least two runtime evidence documents")

    try:
        concepts = load_concepts(root)
        documents = [load_runtime_evidence(path) for path in args.paths]
        output = compose_runtime_evidence(documents, concepts)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.format == "json":
        indent = 2 if args.pretty else None
        print(json.dumps(output, indent=indent, sort_keys=True))
    else:
        print(yaml.safe_dump(output, sort_keys=False), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
