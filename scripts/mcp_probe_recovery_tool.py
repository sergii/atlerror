#!/usr/bin/env python3

from __future__ import annotations

import copy
import re
from typing import Any

from mcp_probe_tools import (
    ABANDON_TOOL_NAME,
    BEGIN_TOOL_NAME,
    FINISH_TOOL_NAME,
    ProbeToolInvocationError,
    RecommendedProbeToolController,
)
from probe_filesystem_claim import ProbeFilesystemClaimError, acquire_probe_filesystem_claim
from probe_workflow_journal import append_probe_workflow_event
from probe_workflow_reconciliation import (
    reconcile_partial_probe_workflow,
    scan_partial_probe_workflows,
)

RECONCILE_PARTIAL_TOOL_NAME = "atlerror.probe.reconcile_partial"
WORKFLOW_RECOVERY_CLAIM = "workflow_recovery"
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SESSION_ID_PATTERN = re.compile(r"^probe-session\.[0-9a-f]{16}$")

_EVENT_TYPE_BY_TOOL = {
    BEGIN_TOOL_NAME: "begin",
    FINISH_TOOL_NAME: "finish",
    ABANDON_TOOL_NAME: "abandon",
    RECONCILE_PARTIAL_TOOL_NAME: "reconcile",
}

_RECONCILE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sessionId", "fingerprint"],
    "properties": {
        "sessionId": {
            "type": "string",
            "pattern": r"^probe-session\.[0-9a-f]{16}$",
            "description": "Partial probe workflow session identifier from the current agent plan.",
        },
        "fingerprint": {
            "type": "string",
            "pattern": r"^[0-9a-f]{64}$",
            "description": (
                "Exact SHA-256 partial-state fingerprint from the current agent plan. "
                "The tool refuses stale fingerprints."
            ),
        },
    },
}


class RecoveryAwareProbeToolController(RecommendedProbeToolController):
    """Add recovery and append-only audit journaling to the opt-in MCP mutation boundary."""

    @staticmethod
    def tool_names() -> tuple[str, ...]:
        return tuple(
            sorted(
                (*RecommendedProbeToolController.tool_names(), RECONCILE_PARTIAL_TOOL_NAME)
            )
        )

    @staticmethod
    def tool_descriptors() -> list[dict[str, Any]]:
        tools = RecommendedProbeToolController.tool_descriptors()
        tools.append(
            {
                "name": RECONCILE_PARTIAL_TOOL_NAME,
                "title": "Reconcile partial probe workflow",
                "description": (
                    "Mark exactly one current partial probe workflow as discarded. The caller must "
                    "provide both the session ID and exact fingerprint from the current agent plan. "
                    "The tool refuses stale or unrelated state, never reads a probe source, never "
                    "creates runtime evidence, and preserves surviving partial files for audit."
                ),
                "inputSchema": copy.deepcopy(_RECONCILE_INPUT_SCHEMA),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            }
        )
        return sorted(tools, key=lambda tool: tool["name"])

    def _inspect_recovery_state(
        self,
        *,
        session_id: str,
        fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        snapshot, _etag = self._load_snapshot()
        try:
            current_issues = scan_partial_probe_workflows(
                self.session_dir,
                incident_id=snapshot["incident_id"],
            )
            all_issues = scan_partial_probe_workflows(self.session_dir)
        except (OSError, ValueError) as exc:
            raise ProbeToolInvocationError(
                f"cannot inspect partial probe workflow state: {exc}"
            ) from exc

        matches = [issue for issue in current_issues if issue.get("session_id") == session_id]
        if len(matches) > 1:
            raise ProbeToolInvocationError(
                f"multiple current partial workflow issues exist for {session_id}"
            )
        if matches:
            issue = matches[0]
            if issue.get("fingerprint") != fingerprint:
                raise ProbeToolInvocationError(
                    "partial probe workflow fingerprint changed; refresh the agent plan before reconciling"
                )
            return snapshot, issue

        unrelated = [issue for issue in all_issues if issue.get("session_id") == session_id]
        if unrelated:
            raise ProbeToolInvocationError(
                "partial probe workflow does not belong to the current diagnosis incident"
            )
        return snapshot, None

    @staticmethod
    def _result_payload(result: dict[str, Any], *, session_id: str) -> dict[str, Any]:
        return {
            "status": "reconciled",
            "already_reconciled": bool(result.get("already_reconciled")),
            "session_id": session_id,
            "incident_id": result.get("incident_id"),
            "issue_kind": result["issue_kind"],
            "fingerprint": result["fingerprint"],
            "resolution": result["resolution"],
            "reconciled_at": result["reconciled_at"],
            "files": copy.deepcopy(result["files"]),
        }

    def reconcile_partial(self, arguments: Any) -> dict[str, Any]:
        arguments = self._validate_arguments(_RECONCILE_INPUT_SCHEMA, arguments)
        session_id = arguments["sessionId"]
        fingerprint = arguments["fingerprint"]

        if SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise ProbeToolInvocationError("invalid probe session id")
        if FINGERPRINT_PATTERN.fullmatch(fingerprint) is None:
            raise ProbeToolInvocationError("invalid partial workflow fingerprint")

        with self._lock:
            try:
                with acquire_probe_filesystem_claim(
                    self.session_dir,
                    purpose=WORKFLOW_RECOVERY_CLAIM,
                    identity={"session_id": session_id},
                    acquired_at=self.clock(),
                ):
                    snapshot, issue = self._inspect_recovery_state(
                        session_id=session_id,
                        fingerprint=fingerprint,
                    )
                    try:
                        result = reconcile_partial_probe_workflow(
                            self.session_dir,
                            session_id,
                            reconciled_at=self.clock(),
                        )
                    except (OSError, ValueError) as exc:
                        raise ProbeToolInvocationError(str(exc)) from exc
            except ProbeFilesystemClaimError as exc:
                raise ProbeToolInvocationError(str(exc)) from exc

        if result.get("fingerprint") != fingerprint:
            raise ProbeToolInvocationError(
                "reconciliation result fingerprint does not match the requested partial state"
            )
        result_incident_id = result.get("incident_id")
        if result_incident_id not in (None, snapshot["incident_id"]):
            raise ProbeToolInvocationError(
                "reconciliation result belongs to a different incident than the current diagnosis"
            )
        if issue is not None and result.get("issue_kind") != issue.get("issue_kind"):
            raise ProbeToolInvocationError(
                "reconciliation result issue kind does not match the current partial state"
            )
        return self._result_payload(result, session_id=session_id)

    def _journal_data(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        if name == BEGIN_TOOL_NAME:
            return {
                "target": result["target"],
                "probe_id": result["probe_id"],
                "scope": copy.deepcopy(result["scope"]),
                "started_at": result["started_at"],
                "expires_at": result["expires_at"],
                "diagnosis_revision": result["diagnosis_revision"],
                "baseline": copy.deepcopy(result["baseline"]),
            }
        if name == FINISH_TOOL_NAME:
            evidence = self._load_runtime_evidence()
            matching = [
                instance
                for instance in evidence.get("instances", [])
                if instance.get("id") == result["evidence_instance_id"]
            ]
            if len(matching) != 1:
                raise ProbeToolInvocationError(
                    "cannot journal completed probe because its evidence instance is unavailable"
                )
            instance = matching[0]
            return {
                "target": result["target"],
                "probe_id": result["probe_id"],
                "scope": copy.deepcopy(result["scope"]),
                "evidence_instance_id": result["evidence_instance_id"],
                "observation": {
                    "id": instance["observation"],
                    "state": instance["state"],
                    "observed_at": instance["observed_at"],
                    "measurement": copy.deepcopy(instance.get("measurement")),
                },
            }
        if name == ABANDON_TOOL_NAME:
            return {
                "target": result["target"],
                "probe_id": result["probe_id"],
                "scope": copy.deepcopy(result["scope"]),
                "abandoned_at": result["abandoned_at"],
            }
        if name == RECONCILE_PARTIAL_TOOL_NAME:
            return {
                "marker_incident_id": result.get("incident_id"),
                "issue_kind": result["issue_kind"],
                "fingerprint": result["fingerprint"],
                "resolution": result["resolution"],
                "reconciled_at": result["reconciled_at"],
                "files": copy.deepcopy(result["files"]),
            }
        raise KeyError(name)

    def _record_journal_event(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        snapshot, _etag = self._load_snapshot()
        incident_id = result.get("incident_id") or snapshot["incident_id"]
        try:
            event = append_probe_workflow_event(
                self.session_dir,
                event_type=_EVENT_TYPE_BY_TOOL[name],
                incident_id=incident_id,
                session_id=result["session_id"],
                data=self._journal_data(name, result),
                recorded_at=self.clock(),
            )
        except (OSError, ValueError) as exc:
            raise ProbeToolInvocationError(
                "probe workflow transition committed but journal append failed: " + str(exc)
            ) from exc
        return {
            "sequence": event["sequence"],
            "event_hash": event["event_hash"],
            "previous_hash": event["previous_hash"],
            "already_recorded": event["already_recorded"],
        }

    def call(self, name: str, arguments: Any) -> dict[str, Any]:
        if name == RECONCILE_PARTIAL_TOOL_NAME:
            result = self.reconcile_partial(arguments)
        else:
            result = super().call(name, arguments)
        if name not in _EVENT_TYPE_BY_TOOL:
            return result
        payload = copy.deepcopy(result)
        payload["journal_event"] = self._record_journal_event(name, result)
        return payload
