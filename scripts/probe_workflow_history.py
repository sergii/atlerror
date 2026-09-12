#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from causal_projection import ROOT, load_concepts
from live_diagnosis import normalize_scope, scope_key
from probe_execution import load_probe_session
from probe_session_state import (
    ABANDONMENT_SUFFIX,
    BINDING_SUFFIX,
    DEFAULT_SESSION_MAX_AGE_SECONDS,
    classify_probe_session_lifecycle,
)
from probe_workflow_reconciliation import (
    RECONCILIATION_SUFFIX,
    scan_partial_probe_workflows,
)
from runtime_evidence import (
    format_timestamp,
    load_runtime_evidence,
    parse_timestamp,
    validate_runtime_references,
    validate_scope_query,
)

SCHEMA_PATH = ROOT / "schema" / "probe-workflow-history.schema.json"
SESSION_ID_PATTERN = re.compile(r"^probe-session\.[0-9a-f]{16}$")
STATUSES = (
    "active",
    "expired",
    "completed",
    "abandoned",
    "reconciled_partial",
    "recovery_required",
)


def _load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return schema


def validate_probe_workflow_history(
    document: dict[str, Any],
    *,
    schema_path: Path = SCHEMA_PATH,
) -> None:
    validator = Draft202012Validator(
        _load_schema(schema_path),
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        raise ValueError(
            "probe workflow history schema validation failed: "
            + "; ".join(error.message for error in errors)
        )


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return document


def _load_binding(
    path: Path,
    *,
    session_id: str,
    concepts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    document = _load_json_object(path, "probe binding")
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


def _load_abandonment(
    path: Path,
    *,
    session_id: str,
    incident_id: str,
) -> dict[str, Any]:
    document = _load_json_object(path, "probe abandonment")
    required = {
        "schema_version",
        "kind",
        "session_id",
        "incident_id",
        "abandoned_at",
        "reason",
    }
    if set(document) != required:
        raise ValueError(f"probe abandonment has an unexpected structure: {path}")
    if document["schema_version"] != "0.1" or document["kind"] != "mcp_probe_abandonment":
        raise ValueError(f"unsupported probe abandonment version or kind: {path}")
    if document["session_id"] != session_id or document["incident_id"] != incident_id:
        raise ValueError(f"probe abandonment does not match session {session_id}")
    parse_timestamp(document["abandoned_at"], "probe abandonment abandoned_at")
    if document["reason"] != "operator_abandoned":
        raise ValueError(f"unsupported probe abandonment reason: {session_id}")
    return document


def _load_reconciliation(path: Path, *, session_id: str) -> dict[str, Any]:
    document = _load_json_object(path, "probe workflow reconciliation")
    required = {
        "schema_version",
        "kind",
        "session_id",
        "incident_id",
        "issue_kind",
        "fingerprint",
        "resolution",
        "reconciled_at",
        "files",
    }
    if set(document) != required:
        raise ValueError(f"probe workflow reconciliation has an unexpected structure: {path}")
    if (
        document["schema_version"] != "0.1"
        or document["kind"] != "mcp_probe_workflow_reconciliation"
    ):
        raise ValueError(f"unsupported probe workflow reconciliation version or kind: {path}")
    if document["session_id"] != session_id:
        raise ValueError(f"probe workflow reconciliation session id does not match filename: {path}")
    if document["issue_kind"] not in {"orphan_session", "orphan_binding"}:
        raise ValueError(f"unsupported reconciliation issue kind: {session_id}")
    fingerprint = document["fingerprint"]
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError(f"invalid reconciliation fingerprint: {session_id}")
    if document["resolution"] != "discard_partial_state":
        raise ValueError(f"unsupported reconciliation resolution: {session_id}")
    parse_timestamp(document["reconciled_at"], "probe workflow reconciliation reconciled_at")
    files = document["files"]
    if not isinstance(files, list) or not files or any(not isinstance(item, str) or not item for item in files):
        raise ValueError(f"invalid reconciliation file list: {session_id}")
    incident_id = document["incident_id"]
    if incident_id is not None and (not isinstance(incident_id, str) or not incident_id):
        raise ValueError(f"invalid reconciliation incident id: {session_id}")
    return document


def _session_id_from_artifact(path: Path) -> str | None:
    name = path.name
    for suffix in (BINDING_SUFFIX, ABANDONMENT_SUFFIX, RECONCILIATION_SUFFIX, ".json"):
        if not name.endswith(suffix):
            continue
        candidate = name[: -len(suffix)]
        return candidate if SESSION_ID_PATTERN.fullmatch(candidate) is not None else None
    return None


def _result_instances(evidence: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    return [
        instance
        for instance in evidence.get("instances", [])
        if instance.get("labels", {}).get("session_id") == session_id
    ]


def _recovery_projection(
    issue: dict[str, Any],
    *,
    reconciled_at: str | None,
) -> dict[str, Any]:
    return {
        "issue_kind": issue["issue_kind"],
        "fingerprint": issue["fingerprint"],
        "resolution": issue.get("recovery", issue.get("resolution")),
        "files": copy.deepcopy(issue["files"]),
        "reconciled_at": reconciled_at,
    }


def _empty_entry(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "status": "recovery_required",
        "incident_id": None,
        "target": None,
        "probe_id": None,
        "executor_id": None,
        "scope": None,
        "started_at": None,
        "expires_at": None,
        "terminal_at": None,
        "diagnosis_revision": None,
        "outcome": None,
        "abandonment": None,
        "recovery": None,
    }


def _populate_from_surviving_artifact(
    entry: dict[str, Any],
    *,
    session_path: Path,
    binding_path: Path,
    concepts: dict[str, dict[str, Any]],
) -> None:
    session_id = entry["session_id"]
    if session_path.exists():
        try:
            session = load_probe_session(session_path, concepts)
        except (OSError, ValueError):
            return
        entry["incident_id"] = session.get("incident_id")
        entry["probe_id"] = session.get("probe", {}).get("id")
        entry["executor_id"] = session.get("executor", {}).get("id")
        entry["scope"] = normalize_scope(session.get("scope"), concepts)
        entry["started_at"] = session.get("started_at")
    if binding_path.exists():
        try:
            binding = _load_binding(binding_path, session_id=session_id, concepts=concepts)
        except ValueError:
            return
        entry["incident_id"] = binding["incident_id"]
        entry["target"] = binding["target"]
        entry["probe_id"] = binding["probe_id"]
        entry["scope"] = copy.deepcopy(binding["scope"])
        entry["diagnosis_revision"] = binding["diagnosis_revision"]


def build_probe_workflow_history(
    *,
    session_dir: Path,
    runtime_evidence_path: Path,
    concepts: dict[str, dict[str, Any]],
    incident_id: str | None = None,
    as_of: datetime | None = None,
    max_age_seconds: int = DEFAULT_SESSION_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Project persisted local probe workflow state into deterministic incident history."""
    evidence = load_runtime_evidence(runtime_evidence_path)
    validate_runtime_references(evidence, concepts)
    evidence_incident_id = evidence["incident_id"]
    if incident_id is not None and incident_id != evidence_incident_id:
        raise ValueError("requested history incident differs from runtime evidence incident")
    incident_id = evidence_incident_id
    if max_age_seconds <= 0:
        raise ValueError("probe session max age must be positive")
    as_of = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if session_dir.exists() and not session_dir.is_dir():
        raise ValueError(f"probe session path is not a directory: {session_dir}")

    unresolved = {
        issue["session_id"]: issue
        for issue in scan_partial_probe_workflows(session_dir, incident_id=incident_id)
    }
    candidate_ids: set[str] = set(unresolved)
    if session_dir.exists():
        for path in session_dir.iterdir():
            if not path.is_file():
                continue
            session_id = _session_id_from_artifact(path)
            if session_id is not None:
                candidate_ids.add(session_id)
    for instance in evidence.get("instances", []):
        session_id = instance.get("labels", {}).get("session_id")
        if isinstance(session_id, str) and SESSION_ID_PATTERN.fullmatch(session_id) is not None:
            candidate_ids.add(session_id)

    sessions: list[dict[str, Any]] = []
    for session_id in sorted(candidate_ids):
        session_path = session_dir / f"{session_id}.json"
        binding_path = session_dir / f"{session_id}{BINDING_SUFFIX}"
        abandonment_path = session_dir / f"{session_id}{ABANDONMENT_SUFFIX}"
        reconciliation_path = session_dir / f"{session_id}{RECONCILIATION_SUFFIX}"

        if session_id in unresolved:
            issue = unresolved[session_id]
            entry = _empty_entry(session_id)
            entry["incident_id"] = issue.get("incident_id")
            _populate_from_surviving_artifact(
                entry,
                session_path=session_path,
                binding_path=binding_path,
                concepts=concepts,
            )
            entry["recovery"] = _recovery_projection(issue, reconciled_at=None)
            sessions.append(entry)
            continue

        reconciliation = None
        if reconciliation_path.exists():
            reconciliation = _load_reconciliation(reconciliation_path, session_id=session_id)
            if reconciliation["incident_id"] not in (None, incident_id):
                reconciliation = None

        session_exists = session_path.exists()
        binding_exists = binding_path.exists()
        if session_exists != binding_exists:
            if reconciliation is None:
                continue
            entry = _empty_entry(session_id)
            entry["status"] = "reconciled_partial"
            entry["incident_id"] = reconciliation["incident_id"]
            entry["terminal_at"] = reconciliation["reconciled_at"]
            _populate_from_surviving_artifact(
                entry,
                session_path=session_path,
                binding_path=binding_path,
                concepts=concepts,
            )
            entry["recovery"] = _recovery_projection(
                reconciliation,
                reconciled_at=reconciliation["reconciled_at"],
            )
            sessions.append(entry)
            continue

        result_instances = _result_instances(evidence, session_id)
        if len(result_instances) > 1:
            raise ValueError(
                f"runtime evidence contains multiple instances for probe session {session_id}"
            )

        if not session_exists and not binding_exists:
            if reconciliation is not None:
                entry = _empty_entry(session_id)
                entry["status"] = "reconciled_partial"
                entry["incident_id"] = reconciliation["incident_id"]
                entry["terminal_at"] = reconciliation["reconciled_at"]
                entry["recovery"] = _recovery_projection(
                    reconciliation,
                    reconciled_at=reconciliation["reconciled_at"],
                )
                sessions.append(entry)
                continue
            if result_instances:
                instance = result_instances[0]
                entry = _empty_entry(session_id)
                entry["status"] = "completed"
                entry["incident_id"] = incident_id
                entry["probe_id"] = instance.get("labels", {}).get("probe")
                entry["executor_id"] = instance.get("labels", {}).get("executor")
                entry["scope"] = normalize_scope(instance.get("scope"), concepts)
                entry["terminal_at"] = instance["observed_at"]
                entry["outcome"] = {
                    "evidence_instance_id": instance["id"],
                    "observation": instance["observation"],
                    "state": instance["state"],
                    "observed_at": instance["observed_at"],
                    "measurement": copy.deepcopy(instance.get("measurement")),
                }
                sessions.append(entry)
                continue
            if abandonment_path.exists():
                raise ValueError(
                    f"probe abandonment exists without session and binding: {session_id}"
                )
            continue

        session = load_probe_session(session_path, concepts)
        binding = _load_binding(binding_path, session_id=session_id, concepts=concepts)
        if session["session_id"] != session_id:
            raise ValueError(f"probe session id does not match its filename: {session_path}")
        if session["incident_id"] != binding["incident_id"]:
            raise ValueError(f"probe session and binding incident ids differ: {session_id}")
        if session["probe"]["id"] != binding["probe_id"]:
            raise ValueError(f"probe session and binding probe ids differ: {session_id}")
        session_scope = normalize_scope(session.get("scope"), concepts)
        if scope_key(session_scope) != scope_key(binding["scope"]):
            raise ValueError(f"probe session and binding scopes differ: {session_id}")
        if session["incident_id"] != incident_id:
            continue

        abandonment = None
        if abandonment_path.exists():
            abandonment = _load_abandonment(
                abandonment_path,
                session_id=session_id,
                incident_id=incident_id,
            )
        if result_instances and abandonment is not None:
            raise ValueError(
                f"probe session has both completion evidence and abandonment marker: {session_id}"
            )

        lifecycle_state, expires_at = classify_probe_session_lifecycle(
            session,
            as_of=as_of,
            max_age_seconds=max_age_seconds,
        )
        entry = {
            "session_id": session_id,
            "status": lifecycle_state,
            "incident_id": incident_id,
            "target": binding["target"],
            "probe_id": binding["probe_id"],
            "executor_id": session["executor"]["id"],
            "scope": copy.deepcopy(binding["scope"]),
            "started_at": session["started_at"],
            "expires_at": expires_at,
            "terminal_at": None,
            "diagnosis_revision": binding["diagnosis_revision"],
            "outcome": None,
            "abandonment": None,
            "recovery": None,
        }
        if reconciliation is not None:
            entry["recovery"] = _recovery_projection(
                reconciliation,
                reconciled_at=reconciliation["reconciled_at"],
            )

        if result_instances:
            instance = result_instances[0]
            entry["status"] = "completed"
            entry["terminal_at"] = instance["observed_at"]
            entry["outcome"] = {
                "evidence_instance_id": instance["id"],
                "observation": instance["observation"],
                "state": instance["state"],
                "observed_at": instance["observed_at"],
                "measurement": copy.deepcopy(instance.get("measurement")),
            }
        elif abandonment is not None:
            entry["status"] = "abandoned"
            entry["terminal_at"] = abandonment["abandoned_at"]
            entry["abandonment"] = {
                "abandoned_at": abandonment["abandoned_at"],
                "reason": abandonment["reason"],
            }
        sessions.append(entry)

    sessions.sort(
        key=lambda item: (
            item["started_at"] or item["terminal_at"] or "",
            item["session_id"],
        )
    )
    summary = {status: 0 for status in STATUSES}
    for session in sessions:
        summary[session["status"]] += 1

    document = {
        "schema_version": "0.1",
        "kind": "probe_workflow_history",
        "incident_id": incident_id,
        "generated_at": format_timestamp(as_of),
        "summary": summary,
        "sessions": sessions,
    }
    validate_probe_workflow_history(document)
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Project persisted Atlerror probe sessions into read-only incident workflow history."
    )
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--runtime-evidence", type=Path, required=True)
    parser.add_argument("--incident-id")
    parser.add_argument(
        "--max-session-age-seconds",
        type=int,
        default=DEFAULT_SESSION_MAX_AGE_SECONDS,
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(root: Path = ROOT) -> int:
    args = build_parser().parse_args()
    concepts = load_concepts(root)
    try:
        document = build_probe_workflow_history(
            session_dir=args.session_dir,
            runtime_evidence_path=args.runtime_evidence,
            concepts=concepts,
            incident_id=args.incident_id,
            max_age_seconds=args.max_session_age_seconds,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(document, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
