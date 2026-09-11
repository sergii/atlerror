#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from causal_projection import ROOT, load_concepts
from runtime_evidence import (
    SCHEMA_PATH as RUNTIME_EVIDENCE_SCHEMA_PATH,
    build_scope_query,
    format_timestamp,
    parse_timestamp,
    validate_runtime_references,
    validate_scope_query,
)

SESSION_SCHEMA_PATH = ROOT / "schema" / "probe-execution-session.schema.json"
DEFAULT_SOURCE_PATH = Path("/proc/net/snmp")
DEFAULT_TTL_SECONDS = 300
SUPPORTED_PROBE_ID = "probe.network.inspect_tcp_integrity_errors"
SUPPORTED_CAPABILITY_ID = "capability.network.inspect_tcp_integrity_errors"
SUPPORTED_OBSERVATION_ID = "observation.network.tcp_integrity_errors"
EXECUTOR_ID = "executor.linux.proc_net_snmp.tcp_inerrs"


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


def parse_tcp_inerrs(text: str) -> int:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index in range(len(lines) - 1):
        header_line = lines[index]
        value_line = lines[index + 1]
        if not header_line.startswith("Tcp:") or not value_line.startswith("Tcp:"):
            continue

        headers = header_line.split()[1:]
        values = value_line.split()[1:]
        if len(headers) != len(values):
            raise ValueError("/proc/net/snmp Tcp header and value columns have different lengths")
        try:
            inerrs_index = headers.index("InErrs")
        except ValueError as exc:
            raise ValueError("/proc/net/snmp Tcp section does not expose InErrs") from exc
        try:
            value = int(values[inerrs_index])
        except ValueError as exc:
            raise ValueError("/proc/net/snmp Tcp.InErrs must be an integer") from exc
        if value < 0:
            raise ValueError("/proc/net/snmp Tcp.InErrs must not be negative")
        return value
    raise ValueError("/proc/net/snmp does not contain a Tcp header/value pair")


def read_tcp_inerrs(path: Path = DEFAULT_SOURCE_PATH) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read TCP counters from {path}: {exc}") from exc
    return parse_tcp_inerrs(text)


def _execution_spec(
    probe_id: str,
    concepts: dict[str, dict[str, Any]],
) -> dict[str, str]:
    probe = concepts.get(probe_id)
    if probe is None:
        raise ValueError(f"unknown probe concept: {probe_id}")
    if probe.get("kind") != "probe":
        raise ValueError(f"execution target must be a probe concept: {probe_id}")

    risk = probe.get("risk")
    if risk != "read_only":
        raise ValueError(
            f"active probe execution refuses non-read-only probe {probe_id}: risk={risk}"
        )
    if probe_id != SUPPORTED_PROBE_ID:
        raise ValueError(f"no built-in read-only executor is registered for probe: {probe_id}")

    requires = set(probe.get("requires", []))
    produces = set(probe.get("produces", []))
    if SUPPORTED_CAPABILITY_ID not in requires:
        raise ValueError(
            f"{probe_id} must require {SUPPORTED_CAPABILITY_ID} for this executor"
        )
    if SUPPORTED_OBSERVATION_ID not in produces:
        raise ValueError(
            f"{probe_id} must produce {SUPPORTED_OBSERVATION_ID} for this executor"
        )
    return {
        "id": probe_id,
        "risk": "read_only",
        "capability": SUPPORTED_CAPABILITY_ID,
        "observation": SUPPORTED_OBSERVATION_ID,
    }


def _session_id(
    *,
    incident_id: str,
    probe_id: str,
    started_at: datetime,
    scope: dict[str, Any] | None,
    source_path: Path,
) -> str:
    identity = json.dumps(
        {
            "incident_id": incident_id,
            "probe_id": probe_id,
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
) -> None:
    _validate_document(session, SESSION_SCHEMA_PATH, "probe execution session")
    probe_id = session["probe"]["id"]
    expected = _execution_spec(probe_id, concepts)
    if session["probe"] != expected:
        raise ValueError("probe execution session no longer matches canonical probe semantics")
    validate_scope_query(session.get("scope"), concepts)
    if session["executor"]["id"] != EXECUTOR_ID:
        raise ValueError(f"unsupported probe executor: {session['executor']['id']}")
    if session["executor"]["platform"] != "linux":
        raise ValueError("built-in TCP integrity executor requires platform=linux")


def begin_probe_session(
    *,
    incident_id: str,
    probe_id: str,
    concepts: dict[str, dict[str, Any]],
    source_path: Path = DEFAULT_SOURCE_PATH,
    scope: dict[str, Any] | None = None,
    started_at: datetime | None = None,
) -> dict[str, Any]:
    if not incident_id:
        raise ValueError("incident_id must not be empty")
    probe = _execution_spec(probe_id, concepts)
    validate_scope_query(scope, concepts)
    started_at = (started_at or _now_utc()).astimezone(timezone.utc)
    baseline = read_tcp_inerrs(source_path)

    session: dict[str, Any] = {
        "schema_version": "0.1",
        "kind": "probe_execution_session",
        "session_id": _session_id(
            incident_id=incident_id,
            probe_id=probe_id,
            started_at=started_at,
            scope=scope,
            source_path=source_path,
        ),
        "incident_id": incident_id,
        "probe": probe,
        "executor": {
            "id": EXECUTOR_ID,
            "platform": "linux",
            "source": str(source_path),
        },
        "started_at": format_timestamp(started_at),
        "baseline": {
            "counter": "Tcp.InErrs",
            "value": baseline,
        },
        "state": "baseline_captured",
    }
    if scope is not None:
        session["scope"] = scope
    validate_probe_session(session, concepts)
    return session


def _runtime_evidence_instance_id(session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"evidence.probe.tcp_integrity_errors.{digest}"


def finish_probe_session(
    session: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
    *,
    finished_at: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    validate_probe_session(session, concepts)
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    started_at = parse_timestamp(session["started_at"], "session.started_at")
    finished_at = (finished_at or _now_utc()).astimezone(timezone.utc)
    if finished_at < started_at:
        raise ValueError("probe finish time must not be earlier than session start time")

    source_path = Path(session["executor"]["source"])
    baseline = int(session["baseline"]["value"])
    current = read_tcp_inerrs(source_path)
    if current < baseline:
        raise ValueError(
            "Tcp.InErrs decreased during the probe session; the counter may have reset or "
            "the source may have changed"
        )

    delta = current - baseline
    state = "observed" if delta > 0 else "absent"
    comparison = "changed" if delta > 0 else "equal"
    session_id = session["session_id"]
    source_attributes = {
        "executor": EXECUTOR_ID,
        "capability": SUPPORTED_CAPABILITY_ID,
        "counter": "Tcp.InErrs",
        "session_id": session_id,
    }
    instance: dict[str, Any] = {
        "id": _runtime_evidence_instance_id(session_id),
        "observation": SUPPORTED_OBSERVATION_ID,
        "state": state,
        "observed_at": format_timestamp(finished_at),
        "expires_at": format_timestamp(finished_at + timedelta(seconds=ttl_seconds)),
        "confidence": "moderate",
        "source": {
            "type": "probe",
            "name": SUPPORTED_PROBE_ID,
            "uri": f"file://{source_path}",
            "attributes": source_attributes,
        },
        "measurement": {
            "value": current,
            "baseline": baseline,
            "delta": delta,
            "unit": "segments",
            "comparison": comparison,
        },
        "labels": {
            "probe": SUPPORTED_PROBE_ID,
            "executor": EXECUTOR_ID,
            "session_id": session_id,
        },
        "note": (
            "Linux Tcp.InErrs is broader than checksum-only failure accounting, and checksum "
            "offload can affect what reaches this counter. Interpret the result with host and "
            "interface context."
        ),
    }
    if "scope" in session:
        instance["scope"] = session["scope"]

    document = {
        "schema_version": "0.1",
        "kind": "runtime_evidence",
        "incident_id": session["incident_id"],
        "description": (
            f"Runtime evidence produced by completed read-only probe {SUPPORTED_PROBE_ID}."
        ),
        "instances": [instance],
    }
    _validate_document(document, RUNTIME_EVIDENCE_SCHEMA_PATH, "runtime evidence")
    validate_runtime_references(document, concepts)
    return document


def load_probe_session(
    path: Path,
    concepts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read probe session {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"probe session is not valid JSON: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("probe session must be a JSON object")
    validate_probe_session(document, concepts)
    return document


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scope-entity",
        action="append",
        default=[],
        metavar="SYSTEM_ENTITY_ID",
        help="Scope the probe result to this semantic system entity; may be repeated",
    )
    parser.add_argument(
        "--scope-boundary",
        action="append",
        default=[],
        metavar="BOUNDARY_ID",
        help="Scope the probe result to this semantic boundary; may be repeated",
    )
    parser.add_argument(
        "--scope-attribute",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Add an exact scope attribute; may be repeated",
    )


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
    parser = argparse.ArgumentParser(
        description=(
            "Execute explicitly registered read-only Atlerror probes without shell commands, "
            "arbitrary subprocesses, network mutation, or remediation."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    begin = subparsers.add_parser(
        "begin",
        help="Capture the baseline for a read-only probe session",
    )
    begin.add_argument("--incident-id", required=True)
    begin.add_argument("--probe", required=True)
    begin.add_argument("--session", type=Path, required=True)
    begin.add_argument(
        "--source-path",
        type=Path,
        default=DEFAULT_SOURCE_PATH,
        help=f"Linux TCP counter source, default {DEFAULT_SOURCE_PATH}",
    )
    _add_scope_arguments(begin)

    finish = subparsers.add_parser(
        "finish",
        help="Complete a probe session and emit standard runtime evidence",
    )
    finish.add_argument("--session", type=Path, required=True)
    finish.add_argument("--output", type=Path)
    finish.add_argument(
        "--ttl-seconds",
        type=int,
        default=DEFAULT_TTL_SECONDS,
        help=f"Evidence lifetime, default {DEFAULT_TTL_SECONDS} seconds",
    )
    finish.add_argument("--pretty", action="store_true")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    concepts = load_concepts(root)

    try:
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
        evidence = finish_probe_session(
            session,
            concepts,
            ttl_seconds=args.ttl_seconds,
        )
        if args.output is not None:
            _write_json_atomic(args.output, evidence)
        indent = 2 if args.pretty else None
        print(json.dumps(evidence, indent=indent, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
