#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml
from jsonschema import Draft202012Validator

from causal_projection import ROOT, load_concepts
from runtime_evidence import format_timestamp, parse_timestamp, validate_runtime_references

SCHEMA_PATH = ROOT / "schema" / "prometheus-adapter.schema.json"
DEFAULT_CONFIDENCE = "moderate"
DEFAULT_TTL_SECONDS = 300


def load_adapter(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise ValueError("Prometheus adapter document must be an object")

    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ValueError(f"Prometheus adapter schema validation failed: {details}")
    return document


def validate_adapter_references(
    adapter: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
) -> None:
    errors: list[str] = []
    mapping_ids: set[str] = set()

    for mapping in adapter["mappings"]:
        mapping_id = mapping["id"]
        if mapping_id in mapping_ids:
            errors.append(f"duplicate Prometheus mapping id: {mapping_id}")
        mapping_ids.add(mapping_id)

        observation_id = mapping["observation"]
        observation = concepts.get(observation_id)
        if observation is None:
            errors.append(f"{mapping_id}: unknown observation concept: {observation_id}")
        elif observation.get("kind") != "observation":
            errors.append(f"{mapping_id}: mapping must reference an observation: {observation_id}")

        rule = mapping["state_rule"]
        if rule["state_when_true"] == rule["state_when_false"]:
            errors.append(f"{mapping_id}: true and false states must differ")

        scope = mapping.get("scope", {})
        for entity_id in scope.get("entities", []):
            entity = concepts.get(entity_id)
            if entity is None:
                errors.append(f"{mapping_id}: unknown scope entity: {entity_id}")
            elif entity.get("kind") != "system_entity":
                errors.append(f"{mapping_id}: scope entity must be a system_entity: {entity_id}")
        for boundary_id in scope.get("boundaries", []):
            boundary = concepts.get(boundary_id)
            if boundary is None:
                errors.append(f"{mapping_id}: unknown scope boundary: {boundary_id}")
            elif boundary.get("kind") != "boundary":
                errors.append(f"{mapping_id}: scope boundary must be a boundary: {boundary_id}")

    if errors:
        raise ValueError("; ".join(errors))


def _numeric_sample(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite: {value!r}")
    return number


def parse_prometheus_response(response: dict[str, Any], mapping_id: str) -> list[dict[str, Any]]:
    if response.get("status") != "success":
        error_type = response.get("errorType", "unknown")
        error = response.get("error", "Prometheus query failed")
        raise ValueError(f"{mapping_id}: Prometheus error {error_type}: {error}")

    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"{mapping_id}: Prometheus response is missing data")
    result_type = data.get("resultType")
    result = data.get("result")
    samples: list[dict[str, Any]] = []

    if result_type == "vector":
        if not isinstance(result, list):
            raise ValueError(f"{mapping_id}: vector result must be a list")
        for index, item in enumerate(result):
            if not isinstance(item, dict):
                raise ValueError(f"{mapping_id}: vector item {index} must be an object")
            metric = item.get("metric", {})
            value = item.get("value")
            if not isinstance(metric, dict) or not isinstance(value, list) or len(value) != 2:
                raise ValueError(f"{mapping_id}: invalid vector item {index}")
            labels = {str(key): str(label_value) for key, label_value in metric.items()}
            timestamp = datetime.fromtimestamp(
                _numeric_sample(value[0], f"{mapping_id}.timestamp"),
                tz=timezone.utc,
            )
            samples.append(
                {
                    "labels": labels,
                    "timestamp": timestamp,
                    "value": _numeric_sample(value[1], f"{mapping_id}.value"),
                }
            )
    elif result_type == "scalar":
        if not isinstance(result, list) or len(result) != 2:
            raise ValueError(f"{mapping_id}: scalar result must contain timestamp and value")
        timestamp = datetime.fromtimestamp(
            _numeric_sample(result[0], f"{mapping_id}.timestamp"),
            tz=timezone.utc,
        )
        samples.append(
            {
                "labels": {},
                "timestamp": timestamp,
                "value": _numeric_sample(result[1], f"{mapping_id}.value"),
            }
        )
    else:
        raise ValueError(
            f"{mapping_id}: unsupported Prometheus resultType {result_type!r}; expected vector or scalar"
        )

    return samples


def evaluate_rule(value: float, rule: dict[str, Any]) -> tuple[str, str]:
    threshold = float(rule["threshold"])
    operator = rule["operator"]
    comparisons = {
        "gt": value > threshold,
        "gte": value >= threshold,
        "lt": value < threshold,
        "lte": value <= threshold,
        "eq": value == threshold,
        "neq": value != threshold,
    }
    matched = comparisons[operator]
    state = rule["state_when_true"] if matched else rule["state_when_false"]

    if value > threshold:
        comparison = "above_baseline"
    elif value < threshold:
        comparison = "below_baseline"
    else:
        comparison = "equal"
    return state, comparison


def _validate_dynamic_scope_ref(
    mapping_id: str,
    label_name: str,
    concept_id: str,
    expected_kind: str,
    concepts: dict[str, dict[str, Any]],
) -> None:
    concept = concepts.get(concept_id)
    if concept is None:
        raise ValueError(
            f"{mapping_id}: label {label_name} references unknown {expected_kind}: {concept_id}"
        )
    if concept.get("kind") != expected_kind:
        raise ValueError(
            f"{mapping_id}: label {label_name} must reference {expected_kind}: {concept_id}"
        )


def build_scope(
    mapping: dict[str, Any],
    labels: dict[str, str],
    concepts: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    scope_config = mapping.get("scope")
    if not isinstance(scope_config, dict):
        return None

    mapping_id = mapping["id"]
    entities = set(scope_config.get("entities", []))
    boundaries = set(scope_config.get("boundaries", []))
    attributes = dict(scope_config.get("attributes", {}))

    for label_name in scope_config.get("entity_labels", []):
        if label_name not in labels:
            raise ValueError(f"{mapping_id}: required scope label is missing: {label_name}")
        entity_id = labels[label_name]
        _validate_dynamic_scope_ref(mapping_id, label_name, entity_id, "system_entity", concepts)
        entities.add(entity_id)

    for label_name in scope_config.get("boundary_labels", []):
        if label_name not in labels:
            raise ValueError(f"{mapping_id}: required scope label is missing: {label_name}")
        boundary_id = labels[label_name]
        _validate_dynamic_scope_ref(mapping_id, label_name, boundary_id, "boundary", concepts)
        boundaries.add(boundary_id)

    for attribute_name, label_name in scope_config.get("attribute_labels", {}).items():
        if label_name not in labels:
            raise ValueError(f"{mapping_id}: required scope label is missing: {label_name}")
        attributes[attribute_name] = labels[label_name]

    scope: dict[str, Any] = {}
    if entities:
        scope["entities"] = sorted(entities)
    if boundaries:
        scope["boundaries"] = sorted(boundaries)
    if attributes:
        scope["attributes"] = dict(sorted(attributes.items()))
    return scope or None


def _instance_id(adapter_id: str, mapping_id: str, labels: dict[str, str]) -> str:
    identity = json.dumps(
        {"adapter": adapter_id, "mapping": mapping_id, "labels": labels},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"evidence.prometheus.{mapping_id}.{digest}"


def sample_to_instance(
    adapter: dict[str, Any],
    mapping: dict[str, Any],
    sample: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
    *,
    source_uri: str | None,
) -> dict[str, Any]:
    value = sample["value"]
    labels = sample["labels"]
    observed_at = sample["timestamp"]
    state, comparison = evaluate_rule(value, mapping["state_rule"])
    threshold = float(mapping["state_rule"]["threshold"])
    ttl_seconds = mapping.get(
        "ttl_seconds",
        adapter.get("default_ttl_seconds", DEFAULT_TTL_SECONDS),
    )
    confidence = mapping.get(
        "confidence",
        adapter.get("default_confidence", DEFAULT_CONFIDENCE),
    )

    source_attributes = {f"label.{key}": value for key, value in sorted(labels.items())}
    source_attributes["prometheus.query"] = mapping["query"]
    source: dict[str, Any] = {
        "type": "metric",
        "name": f"prometheus:{mapping['id']}",
        "attributes": source_attributes,
    }
    if source_uri is not None:
        source["uri"] = source_uri

    measurement: dict[str, Any] = {
        "value": value,
        "baseline": threshold,
        "delta": value - threshold,
        "comparison": comparison,
    }
    if "unit" in mapping:
        measurement["unit"] = mapping["unit"]

    instance: dict[str, Any] = {
        "id": _instance_id(adapter["id"], mapping["id"], labels),
        "observation": mapping["observation"],
        "state": state,
        "observed_at": format_timestamp(observed_at),
        "expires_at": format_timestamp(observed_at + timedelta(seconds=ttl_seconds)),
        "confidence": confidence,
        "source": source,
        "measurement": measurement,
        "labels": {
            "adapter": adapter["id"],
            "mapping": mapping["id"],
        },
    }
    scope = build_scope(mapping, labels, concepts)
    if scope is not None:
        instance["scope"] = scope
    return instance


def build_runtime_evidence(
    adapter: dict[str, Any],
    responses: dict[str, dict[str, Any]],
    concepts: dict[str, dict[str, Any]],
    *,
    incident_id: str,
    source_uri: str | None = None,
) -> dict[str, Any]:
    validate_adapter_references(adapter, concepts)
    instances: list[dict[str, Any]] = []

    for mapping in adapter["mappings"]:
        mapping_id = mapping["id"]
        if mapping_id not in responses:
            raise ValueError(f"missing Prometheus response for mapping: {mapping_id}")
        for sample in parse_prometheus_response(responses[mapping_id], mapping_id):
            instances.append(
                sample_to_instance(
                    adapter,
                    mapping,
                    sample,
                    concepts,
                    source_uri=source_uri,
                )
            )

    instances.sort(key=lambda instance: instance["id"])
    document = {
        "schema_version": "0.1",
        "kind": "runtime_evidence",
        "incident_id": incident_id,
        "description": f"Generated by Prometheus adapter {adapter['id']}.",
        "instances": instances,
    }
    validate_runtime_references(document, concepts)
    return document


def fetch_prometheus_response(
    base_url: str,
    query: str,
    *,
    at: datetime | None,
    timeout_seconds: float,
    bearer_token: str | None,
) -> dict[str, Any]:
    params = {"query": query}
    if at is not None:
        params["time"] = format_timestamp(at)
    endpoint = base_url.rstrip("/") + "/api/v1/query?" + urlencode(params)
    headers = {"Accept": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = Request(endpoint, headers=headers)
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("Prometheus HTTP response must be a JSON object")
    return payload


def parse_response_overrides(values: list[str]) -> dict[str, Path]:
    overrides: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--response must use MAPPING_ID=PATH")
        mapping_id, path = value.split("=", 1)
        if not mapping_id or not path:
            raise ValueError("--response must use non-empty MAPPING_ID=PATH")
        if mapping_id in overrides:
            raise ValueError(f"duplicate --response mapping: {mapping_id}")
        overrides[mapping_id] = Path(path)
    return overrides


def load_response_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        response = json.load(handle)
    if not isinstance(response, dict):
        raise ValueError(f"Prometheus response file must contain an object: {path}")
    return response


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate Prometheus instant-query results into Atlerror runtime evidence."
    )
    parser.add_argument("adapter", type=Path, help="Prometheus adapter mapping YAML")
    parser.add_argument("--incident-id", required=True, help="Runtime evidence incident ID")
    parser.add_argument("--base-url", help="Prometheus server base URL, for example http://localhost:9090")
    parser.add_argument(
        "--response",
        action="append",
        default=[],
        metavar="MAPPING_ID=PATH",
        help="Use a saved Prometheus API response for one mapping instead of querying the server",
    )
    parser.add_argument("--at", help="Optional ISO 8601 instant passed to Prometheus queries")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=10.0,
        help="Prometheus HTTP timeout in seconds",
    )
    parser.add_argument(
        "--bearer-token-env",
        help="Environment variable containing a bearer token for Prometheus HTTP requests",
    )
    parser.add_argument("--format", choices=["yaml", "json"], default="yaml")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    try:
        adapter = load_adapter(args.adapter)
        concepts = load_concepts(root)
        validate_adapter_references(adapter, concepts)
        overrides = parse_response_overrides(args.response)
        known_mapping_ids = {mapping["id"] for mapping in adapter["mappings"]}
        unknown_overrides = sorted(set(overrides) - known_mapping_ids)
        if unknown_overrides:
            raise ValueError("--response references unknown mappings: " + ", ".join(unknown_overrides))
        at = parse_timestamp(args.at, "--at") if args.at is not None else None

        bearer_token = None
        if args.bearer_token_env is not None:
            bearer_token = os.environ.get(args.bearer_token_env)
            if not bearer_token:
                raise ValueError(
                    f"environment variable is empty or missing: {args.bearer_token_env}"
                )

        responses: dict[str, dict[str, Any]] = {}
        for mapping in adapter["mappings"]:
            mapping_id = mapping["id"]
            override = overrides.get(mapping_id)
            if override is not None:
                responses[mapping_id] = load_response_file(override)
                continue
            if args.base_url is None:
                raise ValueError(
                    f"mapping {mapping_id} needs --base-url or --response {mapping_id}=PATH"
                )
            responses[mapping_id] = fetch_prometheus_response(
                args.base_url,
                mapping["query"],
                at=at,
                timeout_seconds=args.timeout_seconds,
                bearer_token=bearer_token,
            )

        document = build_runtime_evidence(
            adapter,
            responses,
            concepts,
            incident_id=args.incident_id,
            source_uri=args.base_url,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    if args.format == "json":
        indent = 2 if args.pretty else None
        print(json.dumps(document, indent=indent, sort_keys=True))
    else:
        print(yaml.safe_dump(document, sort_keys=False), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
