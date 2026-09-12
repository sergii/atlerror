#!/usr/bin/env python3

from __future__ import annotations

import copy
import re
from typing import Any

from agent_plan import STATES, build_agent_plan, validate_agent_plan

RECOVERY_STATE = "workflow_recovery_required"
RECOVERY_REASON = "partial_probe_workflow_state"
RECOVERY_FALLBACK = "reconcile_partial_workflow"
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SESSION_ID_PATTERN = re.compile(r"^probe-session\.[0-9a-f]{16}$")


def _validated_recovery_issues(
    recovery_issues: list[dict[str, Any]] | None,
    *,
    incident_id: str,
) -> list[dict[str, Any]]:
    if recovery_issues is None:
        return []
    if not isinstance(recovery_issues, list):
        raise ValueError("probe workflow recovery issues must be an array")

    validated: list[dict[str, Any]] = []
    for issue in recovery_issues:
        if not isinstance(issue, dict):
            raise ValueError("probe workflow recovery issue must be an object")
        issue = copy.deepcopy(issue)
        session_id = issue.get("session_id")
        if not isinstance(session_id, str) or SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise ValueError("probe workflow recovery issue has invalid session_id")
        issue_incident_id = issue.get("incident_id")
        if issue_incident_id is not None and issue_incident_id != incident_id:
            raise ValueError(
                f"probe workflow recovery issue belongs to another incident: {session_id}"
            )
        if issue.get("issue_kind") not in {"orphan_session", "orphan_binding"}:
            raise ValueError(f"probe workflow recovery issue has invalid issue_kind: {session_id}")
        fingerprint = issue.get("fingerprint")
        if not isinstance(fingerprint, str) or FINGERPRINT_PATTERN.fullmatch(fingerprint) is None:
            raise ValueError(f"probe workflow recovery issue has invalid fingerprint: {session_id}")
        files = issue.get("files")
        if (
            not isinstance(files, list)
            or not files
            or any(not isinstance(item, str) or not item for item in files)
        ):
            raise ValueError(f"probe workflow recovery issue has invalid files: {session_id}")
        if issue.get("recovery") != "discard_partial_state":
            raise ValueError(f"probe workflow recovery issue has unsupported recovery: {session_id}")
        validated.append(issue)

    return sorted(validated, key=lambda item: item["session_id"])


def _recovery_step(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": None,
        "target": None,
        "state": RECOVERY_STATE,
        "reason": RECOVERY_REASON,
        "recommended_probe": None,
        "registered": None,
        "executable_here": None,
        "executor_id": None,
        "operation": None,
        "arguments": None,
        "allowed": False,
        "requires_opt_in": False,
        "unavailable_reason": None,
        "fallback": RECOVERY_FALLBACK,
        "session": None,
        "recovery": {
            "session_id": issue["session_id"],
            "incident_id": issue.get("incident_id"),
            "issue_kind": issue["issue_kind"],
            "fingerprint": issue["fingerprint"],
            "files": copy.deepcopy(issue["files"]),
            "resolution": issue["recovery"],
        },
    }


def build_agent_plan_projection(
    snapshot: dict[str, Any],
    *,
    active_execution_enabled: bool,
    active_sessions: list[dict[str, Any]] | None = None,
    recovery_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the normal agent plan or gate it behind unresolved workflow recovery."""
    incident_id = snapshot.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id:
        raise ValueError("diagnosis snapshot must include incident_id")
    evidence_revision = snapshot.get("evidence_revision")
    if not isinstance(evidence_revision, int) or evidence_revision < 0:
        raise ValueError("diagnosis snapshot must include a non-negative evidence_revision")

    issues = _validated_recovery_issues(recovery_issues, incident_id=incident_id)
    if not issues:
        document = build_agent_plan(
            snapshot,
            active_execution_enabled=active_execution_enabled,
            active_sessions=active_sessions,
        )
        document["summary"][RECOVERY_STATE] = 0
        validate_agent_plan(document)
        return document

    summary = {state: 0 for state in STATES}
    summary[RECOVERY_STATE] = len(issues)
    document = {
        "schema_version": "0.1",
        "kind": "agent_plan",
        "incident_id": incident_id,
        "evidence_revision": evidence_revision,
        "active_execution_enabled": active_execution_enabled,
        "steps": [_recovery_step(issue) for issue in issues],
        "summary": summary,
    }
    validate_agent_plan(document)
    return document
