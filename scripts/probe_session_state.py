#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from live_diagnosis import normalize_scope, scope_key
from probe_execution import load_probe_session
from runtime_evidence import load_runtime_evidence, validate_runtime_references, validate_scope_query

SESSION_ID_PATTERN = re.compile(r"^probe-session\.[0-9a-f]{16}$")
BINDING_SUFFIX = ".binding.json"


def _load_binding(
    path: Path,
    *,
    session_id: str,
    concepts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read probe binding {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"probe binding is not valid JSON: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"probe binding must be a JSON object: {path}")

    required = {
        "schema_version",
        "kind",
        "session_id",
        "incident_id",
        "target",
        "probe_id",
        "scope",
        "diagnosis_revision",
        "diagnosis_etag",
    }
    if set(document) != required:
        raise ValueError(f"probe binding has an unexpected structure: {path}")
    if document["schema_version"] != "0.1" or document["kind"] != "mcp_probe_binding":
        raise ValueError(f"unsupported probe binding version or kind: {path}")
    if document["session_id"] != session_id:
        raise ValueError(f"probe binding session id does not match its filename: {path}")
    for field in ("incident_id", "target", "probe_id", "diagnosis_etag"):
        if not isinstance(document[field], str) or not document[field]:
            raise ValueError(f"probe binding {field} must be a non-empty string: {path}")
    if not isinstance(document["diagnosis_revision"], int) or document["diagnosis_revision"] < 0:
        raise ValueError(f"probe binding diagnosis_revision is invalid: {path}")
    if document["scope"] is not None:
        validate_scope_query(document["scope"], concepts)
        document["scope"] = normalize_scope(document["scope"], concepts)
    return document


def _session_result_instances(
    evidence: dict[str, Any],
    session_id: str,
) -> list[dict[str, Any]]:
    return [
        instance
        for instance in evidence.get("instances", [])
        if instance.get("labels", {}).get("session_id") == session_id
    ]


def discover_active_probe_sessions(
    *,
    session_dir: Path,
    runtime_evidence_path: Path,
    concepts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project unfinished persisted MCP probe sessions without executing any probe."""
    evidence = load_runtime_evidence(runtime_evidence_path)
    validate_runtime_references(evidence, concepts)
    incident_id = evidence["incident_id"]

    if not session_dir.exists():
        return []
    if not session_dir.is_dir():
        raise ValueError(f"probe session path is not a directory: {session_dir}")

    active: list[dict[str, Any]] = []
    for binding_path in sorted(session_dir.glob(f"probe-session.*{BINDING_SUFFIX}")):
        filename = binding_path.name
        session_id = filename[: -len(BINDING_SUFFIX)]
        if SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise ValueError(f"invalid probe binding filename: {binding_path}")

        binding = _load_binding(
            binding_path,
            session_id=session_id,
            concepts=concepts,
        )
        if binding["incident_id"] != incident_id:
            continue

        session_path = session_dir / f"{session_id}.json"
        session = load_probe_session(session_path, concepts)
        if session["session_id"] != session_id:
            raise ValueError(f"probe session id does not match its filename: {session_path}")
        if session["incident_id"] != binding["incident_id"]:
            raise ValueError(f"probe session and binding incident ids differ: {session_id}")
        if session["probe"]["id"] != binding["probe_id"]:
            raise ValueError(f"probe session and binding probe ids differ: {session_id}")
        session_scope = normalize_scope(session.get("scope"), concepts)
        if scope_key(session_scope) != scope_key(binding["scope"]):
            raise ValueError(f"probe session and binding scopes differ: {session_id}")

        result_instances = _session_result_instances(evidence, session_id)
        if len(result_instances) > 1:
            raise ValueError(
                f"runtime evidence contains multiple instances for probe session {session_id}"
            )
        if result_instances:
            continue

        active.append(
            {
                "session_id": session_id,
                "incident_id": binding["incident_id"],
                "target": binding["target"],
                "probe_id": binding["probe_id"],
                "scope": copy.deepcopy(binding["scope"]),
                "started_at": session["started_at"],
                "diagnosis_revision": binding["diagnosis_revision"],
                "executor_id": session["executor"]["id"],
            }
        )

    return sorted(
        active,
        key=lambda item: (
            scope_key(item["scope"]),
            item["target"],
            item["session_id"],
        ),
    )
