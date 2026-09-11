#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from causal_projection import ROOT, load_concepts
from runtime_evidence import format_timestamp, validate_runtime_references

SCHEMA_PATH = ROOT / "schema" / "opentelemetry-trace-adapter.schema.json"
DEFAULT_TTL_SECONDS = 300
DEFAULT_CONFIDENCE = "moderate"
SPAN_KIND_BY_NUMBER = {
    0: "SPAN_KIND_UNSPECIFIED",
    1: "SPAN_KIND_INTERNAL",
    2: "SPAN_KIND_SERVER",
    3: "SPAN_KIND_CLIENT",
    4: "SPAN_KIND_PRODUCER",
    5: "SPAN_KIND_CONSUMER",
}


def load_adapter(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise ValueError("OpenTelemetry adapter document must be an object")
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda error: list(error.path))
    if errors:
        raise ValueError(
            "OpenTelemetry adapter schema validation failed: "
            + "; ".join(error.message for error in errors)
        )
    return document


def validate_adapter_references(adapter: dict[str, Any], concepts: dict[str, dict[str, Any]]) -> None:
    errors: list[str] = []
    mapping_ids: set[str] = set()
    for mapping in adapter["mappings"]:
        mapping_id = mapping["id"]
        if mapping_id in mapping_ids:
            errors.append(f"duplicate OpenTelemetry mapping id: {mapping_id}")
        mapping_ids.add(mapping_id)
        observation_id = mapping["observation"]
        observation = concepts.get(observation_id)
        if observation is None:
            errors.append(f"{mapping_id}: unknown observation concept: {observation_id}")
        elif observation.get("kind") != "observation":
            errors.append(f"{mapping_id}: mapping must reference an observation: {observation_id}")

        scope = mapping.get("scope", {})
        for entity_id in scope.get("entities", []):
            concept = concepts.get(entity_id)
            if concept is None:
                errors.append(f"{mapping_id}: unknown scope entity: {entity_id}")
            elif concept.get("kind") != "system_entity":
                errors.append(f"{mapping_id}: scope entity must be a system_entity: {entity_id}")
        for boundary_id in scope.get("boundaries", []):
            concept = concepts.get(boundary_id)
            if concept is None:
                errors.append(f"{mapping_id}: unknown scope boundary: {boundary_id}")
            elif concept.get("kind") != "boundary":
                errors.append(f"{mapping_id}: scope boundary must be a boundary: {boundary_id}")

        true_state = mapping.get("state_when_true", "observed")
        false_state = mapping.get("state_when_false", "absent")
        if true_state == false_state:
            errors.append(f"{mapping_id}: true and false states must differ")
    if errors:
        raise ValueError("; ".join(errors))


def decode_any_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if key in value:
            raw = value[key]
            if key == "intValue":
                return int(raw)
            if key == "doubleValue":
                return float(raw)
            return raw
    if "arrayValue" in value:
        array = value["arrayValue"]
        values = array.get("values", []) if isinstance(array, dict) else []
        return [decode_any_value(item) for item in values]
    return None


def decode_attributes(items: Any) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    if not isinstance(items, list):
        return attributes
    for item in items:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if isinstance(key, str) and key:
            attributes[key] = decode_any_value(item.get("value"))
    return attributes


def parse_nanos(value: Any, label: str) -> int:
    try:
        nanos = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer nanosecond timestamp") from exc
    if nanos < 0:
        raise ValueError(f"{label} must not be negative")
    return nanos


def timestamp_from_nanos(value: Any, label: str) -> datetime:
    nanos = parse_nanos(value, label)
    return datetime.fromtimestamp(nanos / 1_000_000_000, tz=timezone.utc)


def span_kind(value: Any) -> str:
    if isinstance(value, int):
        return SPAN_KIND_BY_NUMBER.get(value, f"SPAN_KIND_UNKNOWN_{value}")
    if isinstance(value, str):
        if value.isdigit():
            return SPAN_KIND_BY_NUMBER.get(int(value), f"SPAN_KIND_UNKNOWN_{value}")
        return value
    return "SPAN_KIND_UNSPECIFIED"


def iter_spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    resource_spans = payload.get("resourceSpans", payload.get("resource_spans", []))
    if not isinstance(resource_spans, list):
        raise ValueError("OTLP trace payload resourceSpans must be an array")
    for resource_index, resource_span in enumerate(resource_spans):
        if not isinstance(resource_span, dict):
            raise ValueError(f"resourceSpans[{resource_index}] must be an object")
        resource = resource_span.get("resource", {})
        resource_attributes = decode_attributes(resource.get("attributes", [])) if isinstance(resource, dict) else {}
        scope_spans = resource_span.get("scopeSpans", resource_span.get("scope_spans", []))
        if not isinstance(scope_spans, list):
            raise ValueError(f"resourceSpans[{resource_index}].scopeSpans must be an array")
        for scope_index, scope_span in enumerate(scope_spans):
            if not isinstance(scope_span, dict):
                raise ValueError(f"scopeSpans[{scope_index}] must be an object")
            spans = scope_span.get("spans", [])
            if not isinstance(spans, list):
                raise ValueError("scopeSpans.spans must be an array")
            for span_index, span in enumerate(spans):
                if not isinstance(span, dict):
                    raise ValueError(f"span[{span_index}] must be an object")
                span_attributes = decode_attributes(span.get("attributes", []))
                attributes = dict(resource_attributes)
                attributes.update(span_attributes)
                output.append({"span": span, "attributes": attributes})
    return output


def match_span(mapping: dict[str, Any], span: dict[str, Any], attributes: dict[str, Any]) -> bool:
    match = mapping["match"]
    expected_kind = match.get("span_kind")
    if expected_kind is not None and span_kind(span.get("kind")) != expected_kind:
        return False
    expected_name = match.get("name")
    if expected_name is not None and span.get("name") != expected_name:
        return False
    for key, expected in match.get("attributes", {}).items():
        if attributes.get(key) != expected:
            return False
    return True


def compare(value: float, operator: str, threshold: float) -> bool:
    if operator == "gt":
        return value > threshold
    if operator == "gte":
        return value >= threshold
    if operator == "lt":
        return value < threshold
    if operator == "lte":
        return value <= threshold
    if operator == "eq":
        return value == threshold
    if operator == "neq":
        return value != threshold
    raise ValueError(f"unsupported comparison operator: {operator}")


def evaluate_signal(mapping: dict[str, Any], span: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    signal = mapping["signal"]
    signal_type = signal["type"]
    if signal_type == "duration_ms":
        start = parse_nanos(span.get("startTimeUnixNano"), f"{mapping['id']}.startTimeUnixNano")
        end = parse_nanos(span.get("endTimeUnixNano"), f"{mapping['id']}.endTimeUnixNano")
        if end < start:
            raise ValueError(f"{mapping['id']}: span end precedes start")
        duration_ms = (end - start) / 1_000_000
        if not math.isfinite(duration_ms):
            raise ValueError(f"{mapping['id']}: span duration must be finite")
        threshold = float(signal["threshold"])
        matched = compare(duration_ms, signal["operator"], threshold)
        comparison = "above_baseline" if duration_ms > threshold else "below_baseline" if duration_ms < threshold else "equal"
        measurement: dict[str, Any] = {
            "value": duration_ms,
            "baseline": threshold,
            "delta": duration_ms - threshold,
            "unit": mapping.get("unit", "ms"),
            "comparison": comparison,
        }
        return matched, measurement
    if signal_type == "status_error":
        status = span.get("status", {})
        code = status.get("code", "STATUS_CODE_UNSET") if isinstance(status, dict) else "STATUS_CODE_UNSET"
        is_error = code in (2, "2", "STATUS_CODE_ERROR")
        return is_error, {
            "value": str(code),
            "comparison": "present" if is_error else "absent",
        }
    raise ValueError(f"{mapping['id']}: unsupported signal type: {signal_type}")


def validate_dynamic_ref(mapping_id: str, attribute_name: str, value: Any, expected_kind: str, concepts: dict[str, dict[str, Any]]) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{mapping_id}: required semantic scope attribute is missing or non-string: {attribute_name}")
    concept = concepts.get(value)
    if concept is None:
        raise ValueError(f"{mapping_id}: attribute {attribute_name} references unknown {expected_kind}: {value}")
    if concept.get("kind") != expected_kind:
        raise ValueError(f"{mapping_id}: attribute {attribute_name} must reference {expected_kind}: {value}")
    return value


def build_scope(mapping: dict[str, Any], attributes: dict[str, Any], concepts: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    config = mapping.get("scope")
    if not isinstance(config, dict):
        return None
    mapping_id = mapping["id"]
    entities = set(config.get("entities", []))
    boundaries = set(config.get("boundaries", []))
    scope_attributes = dict(config.get("attributes", {}))

    for attribute_name in config.get("entity_attributes", []):
        entities.add(validate_dynamic_ref(mapping_id, attribute_name, attributes.get(attribute_name), "system_entity", concepts))
    for attribute_name in config.get("boundary_attributes", []):
        boundaries.add(validate_dynamic_ref(mapping_id, attribute_name, attributes.get(attribute_name), "boundary", concepts))
    for scope_name, attribute_name in config.get("attribute_map", {}).items():
        value = attributes.get(attribute_name)
        if value is None:
            raise ValueError(f"{mapping_id}: required scope attribute is missing: {attribute_name}")
        scope_attributes[scope_name] = str(value)

    result: dict[str, Any] = {}
    if entities:
        result["entities"] = sorted(entities)
    if boundaries:
        result["boundaries"] = sorted(boundaries)
    if scope_attributes:
        result["attributes"] = dict(sorted(scope_attributes.items()))
    return result or None


def instance_id(adapter_id: str, mapping_id: str, span: dict[str, Any]) -> str:
    identity = f"{adapter_id}|{mapping_id}|{span.get('traceId','')}|{span.get('spanId','')}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"evidence.opentelemetry.{mapping_id}.{digest}"


def span_to_instance(adapter: dict[str, Any], mapping: dict[str, Any], span: dict[str, Any], attributes: dict[str, Any], concepts: dict[str, dict[str, Any]], *, source_uri: str | None) -> dict[str, Any]:
    matched, measurement = evaluate_signal(mapping, span)
    true_state = mapping.get("state_when_true", "observed")
    false_state = mapping.get("state_when_false", "absent")
    state = true_state if matched else false_state
    end_time = timestamp_from_nanos(span.get("endTimeUnixNano"), f"{mapping['id']}.endTimeUnixNano")
    ttl = mapping.get("ttl_seconds", adapter.get("default_ttl_seconds", DEFAULT_TTL_SECONDS))
    confidence = mapping.get("confidence", adapter.get("default_confidence", DEFAULT_CONFIDENCE))

    source_attributes = {
        "otel.trace_id": str(span.get("traceId", "")),
        "otel.span_id": str(span.get("spanId", "")),
        "otel.span_name": str(span.get("name", "")),
        "otel.span_kind": span_kind(span.get("kind")),
    }
    for key in ("service.name", "peer.service", "server.address", "db.system"):
        if key in attributes:
            source_attributes[key] = str(attributes[key])
    source: dict[str, Any] = {
        "type": "trace",
        "name": f"opentelemetry:{mapping['id']}",
        "attributes": source_attributes,
    }
    if source_uri is not None:
        source["uri"] = source_uri

    instance: dict[str, Any] = {
        "id": instance_id(adapter["id"], mapping["id"], span),
        "observation": mapping["observation"],
        "state": state,
        "observed_at": format_timestamp(end_time),
        "expires_at": format_timestamp(end_time + timedelta(seconds=ttl)),
        "confidence": confidence,
        "source": source,
        "measurement": measurement,
        "labels": {
            "adapter": adapter["id"],
            "mapping": mapping["id"],
        },
    }
    scope = build_scope(mapping, attributes, concepts)
    if scope is not None:
        instance["scope"] = scope
    return instance


def build_runtime_evidence(adapter: dict[str, Any], payload: dict[str, Any], concepts: dict[str, dict[str, Any]], *, incident_id: str, source_uri: str | None = None) -> dict[str, Any]:
    validate_adapter_references(adapter, concepts)
    instances: list[dict[str, Any]] = []
    spans = iter_spans(payload)
    for item in spans:
        span = item["span"]
        attributes = item["attributes"]
        for mapping in adapter["mappings"]:
            if match_span(mapping, span, attributes):
                instances.append(span_to_instance(adapter, mapping, span, attributes, concepts, source_uri=source_uri))
    if not instances:
        raise ValueError("OpenTelemetry payload matched no adapter mappings")
    instances.sort(key=lambda instance: instance["id"])
    document = {
        "schema_version": "0.1",
        "kind": "runtime_evidence",
        "incident_id": incident_id,
        "description": f"Generated by OpenTelemetry trace adapter {adapter['id']}.",
        "instances": instances,
    }
    validate_runtime_references(document, concepts)
    return document


def load_payload(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("OTLP trace payload must be a JSON object")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Translate OTLP/HTTP JSON trace payloads into Atlerror runtime evidence.")
    parser.add_argument("adapter", type=Path, help="OpenTelemetry trace adapter mapping YAML")
    parser.add_argument("payload", type=Path, help="OTLP/HTTP JSON trace payload")
    parser.add_argument("--incident-id", required=True, help="Runtime evidence incident ID")
    parser.add_argument("--source-uri", help="Optional source URI recorded in runtime evidence provenance")
    parser.add_argument("--format", choices=["yaml", "json"], default="yaml")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        adapter = load_adapter(args.adapter)
        payload = load_payload(args.payload)
        concepts = load_concepts(root)
        evidence = build_runtime_evidence(
            adapter,
            payload,
            concepts,
            incident_id=args.incident_id,
            source_uri=args.source_uri or str(args.payload),
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.format == "json":
        print(json.dumps(evidence, indent=2 if args.pretty else None, sort_keys=True))
    else:
        print(yaml.safe_dump(evidence, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
