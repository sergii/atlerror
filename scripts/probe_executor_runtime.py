#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from causal_projection import ROOT, load_concepts
from probe_executor_registry import (
    CAPABILITIES_SCHEMA_PATH,
    TCP_INTEGRITY_CAPABILITY_ID,
    TCP_INTEGRITY_EXECUTOR_ID,
    TCP_INTEGRITY_OBSERVATION_ID,
    TCP_INTEGRITY_PROBE_ID,
    TCP_INTEGRITY_SOURCE_PATH,
    ProbeExecutor,
    ProbeExecutorRegistry,
    default_executor_registry,
    parse_tcp_inerrs,
    read_tcp_inerrs,
)
from runtime_evidence import (
    SCHEMA_PATH as RUNTIME_EVIDENCE_SCHEMA_PATH,
    build_scope_query,
    format_timestamp,
    parse_timestamp,
    validate_runtime_references,
    validate_scope_query,
)

SESSION_SCHEMA_PATH = ROOT / "schema" / "probe-execution-session.schema.json"
DEFAULT_SOURCE_PATH = TCP_INTEGRITY_SOURCE_PATH
DEFAULT_TTL_SECONDS = 300
SUPPORTED_PROBE_ID = TCP_INTEGRITY_PROBE_ID
SUPPORTED_CAPABILITY_ID = TCP_INTEGRITY_CAPABILITY_ID
SUPPORTED_OBSERVATION_ID = TCP_INTEGRITY_OBSERVATION_ID
EXECUTOR_ID = TCP_INTEGRITY_EXECUTOR_ID


def _load_schema(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return schema


def _validate_document(document: dict[str, Any], schema_path: Path, label: str) -> None:
    validator = Draft202012Validator(
        _load_schema(schema_path),
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        raise ValueError(
            f"{label} schema validation failed: "
            + "; ".join(error.message for error in errors)
        )


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _registry(registry: ProbeExecutorRegistry | None) -> ProbeExecutorRegistry:
    return registry if registry is not None else default_executor_registry()


def _execution_spec(executor: ProbeExecutor) -> dict[str, str]:
    return {
        "id": executor.probe_id,
        "risk": "read_only",
        "capability": executor.capability_id,
        "observation": executor.observation_id,
    }


def _session_id(
    *,
    incident_id: str,
    probe_id: str,
    executor_id: str,
    started_at: datetime,
    scope: dict[str, Any] | None,
    source_path: Path,
) -> str:
    identity = json.dumps(
        {
            "incident_id": incident_id,
            "probe_id": probe_id,
            "executor_id": executor_id,
            "started_at": format_timestamp(started_at),
            "scope": scope,
            "source": str(source_path),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"probe-session.{digest}"


def validate_probe_session(
    session: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
    *,
    registry: ProbeExecutorRegistry | None = None,
) -> None:
    _validate_document(session, SESSION_SCHEMA_PATH, "probe execution session")
    registry = _registry(registry)
    probe_id = session["probe"]["id"]
    executor = registry.resolve(probe_id, concepts)
    if session["probe"] != _execution_spec(executor):
        raise ValueError("probe execution session no longer matches canonical probe semantics")

    validate_scope_query(session.get("scope"), concepts)
    executor_document = session["executor"]
    if executor_document["id"] != executor.id:
        raise ValueError(f"unsupported probe executor: {executor_document['id']}")
    if executor_document["platform"] != executor.platform:
        raise ValueError(
            f"probe execution session platform no longer matches executor {executor.id}"
        )
    if executor_document["policy"] != executor.policy:
        raise ValueError(f"probe execution session policy no longer matches executor {executor.id}")
    if executor_document["source_mode"] == "registered":
        if Path(executor_document["source"]) != executor.source_path:
            raise ValueError(f"registered probe source no longer matches executor {executor.id}")
    if session["baseline"]["metric"] != executor.baseline_metric:
        raise ValueError(f"probe baseline metric no longer matches executor {executor.id}")


def _bound_executor(
    session: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
    registry: ProbeExecutorRegistry,
) -> ProbeExecutor:
    validate_probe_session(session, concepts, registry=registry)
    executor = registry.resolve(session["probe"]["id"], concepts)
    if session["executor"]["source_mode"] == "explicit_override":
        executor = executor.with_source(Path(session["executor"]["source"]))
    return executor


def begin_probe_session(
    *,
    incident_id: str,
    probe_id: str,
    concepts: dict[str, dict[str, Any]],
    source_path: Path | None = None,
    scope: dict[str, Any] | None = None,
    started_at: datetime | None = None,
    registry: ProbeExecutorRegistry | None = None,
) -> dict[str, Any]:
    if not incident_id:
        raise ValueError("incident_id must not be empty")
    registry = _registry(registry)
    executor = registry.resolve(probe_id, concepts)
    source_mode = "registered"
    if source_path is not None:
        executor = executor.with_source(source_path)
        source_mode = "explicit_override"

    validate_scope_query(scope, concepts)
    available, unavailable_reason = executor.availability()
    if not available:
        raise ValueError(
            f"probe executor {executor.id} is unavailable: {unavailable_reason or 'unknown reason'}"
        )

    started_at = (started_at or _now_utc()).astimezone(timezone.utc)
    session: dict[str, Any] = {
        "schema_version": "0.1",
        "kind": "probe_execution_session",
        "session_id": _session_id(
            incident_id=incident_id,
            probe_id=probe_id,
            executor_id=executor.id,
            started_at=started_at,
            scope=scope,
            source_path=executor.source_path,
        ),
        "incident_id": incident_id,
        "probe": _execution_spec(executor),
        "executor": {
            "id": executor.id,
            "platform": executor.platform,
            "source": str(executor.source_path),
            "source_mode": source_mode,
            "policy": copy.deepcopy(executor.policy),
        },
        "started_at": format_timestamp(started_at),
        "baseline": executor.capture(),
        "state": "baseline_captured",
    }
    if scope is not None:
        session["scope"] = copy.deepcopy(scope)
    validate_probe_session(session, concepts, registry=registry)
    return session


def _runtime_evidence_instance_id(session_id: str, observation_id: str) -> str:
    digest = hashlib.sha256(f"{session_id}:{observation_id}".encode("utf-8")).hexdigest()[:16]
    return f"evidence.probe.{digest}"


def finish_probe_session(
    session: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
    *,
    finished_at: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    registry: ProbeExecutorRegistry | None = None,
) -> dict[str, Any]:
    registry = _registry(registry)
    executor = _bound_executor(session, concepts, registry)
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    started_at = parse_timestamp(session["started_at"], "session.started_at")
    finished_at = (finished_at or _now_utc()).astimezone(timezone.utc)
    if finished_at < started_at:
        raise ValueError("probe finish time must not be earlier than session start time")

    result = executor.evaluate(session["baseline"])
    session_id = session["session_id"]
    instance: dict[str, Any] = {
        "id": _runtime_evidence_instance_id(session_id, executor.observation_id),
        "observation": executor.observation_id,
        "state": result["state"],
        "observed_at": format_timestamp(finished_at),
        "expires_at": format_timestamp(finished_at + timedelta(seconds=ttl_seconds)),
        "confidence": "moderate",
        "source": {
            "type": "probe",
            "name": executor.probe_id,
            "uri": f"file://{executor.source_path}",
            "attributes": {
                "executor": executor.id,
                "capability": executor.capability_id,
                "metric": executor.baseline_metric,
                "session_id": session_id,
            },
        },
        "measurement": copy.deepcopy(result["measurement"]),
        "labels": {
            "probe": executor.probe_id,
            "executor": executor.id,
            "session_id": session_id,
        },
        "note": result["note"],
    }
    if "scope" in session:
        instance["scope"] = copy.deepcopy(session["scope"])

    document = {
        "schema_version": "0.1",
        "kind": "runtime_evidence",
        "incident_id": session["incident_id"],
        "description": f"Runtime evidence produced by completed read-only probe {executor.probe_id}.",
        "instances": [instance],
    }
    _validate_document(document, RUNTIME_EVIDENCE_SCHEMA_PATH, "runtime evidence")
    validate_runtime_references(document, concepts)
    return document


def build_probe_execution_capabilities(
    concepts: dict[str, dict[str, Any]],
    *,
    registry: ProbeExecutorRegistry | None = None,
) -> dict[str, Any]:
    document = _registry(registry).capability_projection(concepts)
    _validate_document(document, CAPABILITIES_SCHEMA_PATH, "probe execution capabilities")
    return document


def load_probe_session(
    path: Path,
    concepts: dict[str, dict[str, Any]],
    *,
    registry: ProbeExecutorRegistry | None = None,
) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read probe session {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"probe session is not valid JSON: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("probe session must be a JSON object")
    validate_probe_session(document, concepts, registry=registry)
    return document


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scope-entity", action="append", default=[], metavar="SYSTEM_ENTITY_ID")
    parser.add_argument("--scope-boundary", action="append", default=[], metavar="BOUNDARY_ID")
    parser.add_argument("--scope-attribute", action="append", default=[], metavar="KEY=VALUE")


def _parse_scope_attributes(values: list[str]) -> list[tuple[str, str]]:
    attributes: list[tuple[str, str]] = []
    for value in values:
        if "=" not in value:
            raise ValueError("--scope-attribute must use KEY=VALUE")
        key, attribute_value = value.split("=", 1)
        key = key.strip()
        attribute_value = attribute_value.strip()
        if not key or not attribute_value:
            raise ValueError("--scope-attribute must use non-empty KEY=VALUE")
        attributes.append((key, attribute_value))
    return attributes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover and execute registered read-only Atlerror probes.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capabilities = subparsers.add_parser("capabilities")
    capabilities.add_argument("--pretty", action="store_true")

    begin = subparsers.add_parser("begin")
    begin.add_argument("--incident-id", required=True)
    begin.add_argument("--probe", required=True)
    begin.add_argument("--session", type=Path, required=True)
    begin.add_argument("--source-path", type=Path)
    _add_scope_arguments(begin)

    finish = subparsers.add_parser("finish")
    finish.add_argument("--session", type=Path, required=True)
    finish.add_argument("--output", type=Path)
    finish.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    finish.add_argument("--pretty", action="store_true")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    concepts = load_concepts(root)
    try:
        if args.command == "capabilities":
            document = build_probe_execution_capabilities(concepts)
            print(json.dumps(document, indent=2 if args.pretty else None, sort_keys=True))
            return 0
        if args.command == "begin":
            scope = build_scope_query(
                entities=args.scope_entity,
                boundaries=args.scope_boundary,
                attributes=_parse_scope_attributes(args.scope_attribute),
            )
            session = begin_probe_session(
                incident_id=args.incident_id,
                probe_id=args.probe,
                concepts=concepts,
                source_path=args.source_path,
                scope=scope,
            )
            _write_json_atomic(args.session, session)
            print(json.dumps(session, indent=2, sort_keys=True))
            return 0

        session = load_probe_session(args.session, concepts)
        evidence = finish_probe_session(session, concepts, ttl_seconds=args.ttl_seconds)
        if args.output is not None:
            _write_json_atomic(args.output, evidence)
        print(json.dumps(evidence, indent=2 if args.pretty else None, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
