#!/usr/bin/env python3

from __future__ import annotations

import copy
import re
from typing import Any

from mcp_probe_tools import ProbeToolInvocationError, RecommendedProbeToolController
from probe_filesystem_claim import ProbeFilesystemClaimError, acquire_probe_filesystem_claim
from probe_workflow_reconciliation import (
    reconcile_partial_probe_workflow,
    scan_partial_probe_workflows,
)

RECONCILE_PARTIAL_TOOL_NAME = "atlerror.probe.reconcile_partial"
WORKFLOW_RECOVERY_CLAIM = "workflow_recovery"
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SESSION_ID_PATTERN = re.compile(r"^probe-session\.[0-9a-f]{16}$")

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
    """Add explicit partial-workflow reconciliation to the opt-in MCP mutation boundary."""

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

    def _current_recovery_issue(
        self,
        *,
        session_id: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        snapshot, _etag = self._load_snapshot()
        try:
            issues = scan_partial_probe_workflows(
                self.session_dir,
                incident_id=snapshot["incident_id"],
            )
        except (OSError, ValueError) as exc:
            raise ProbeToolInvocationError(
                f"cannot inspect partial probe workflow state: {exc}"
            ) from exc

        matches = [issue for issue in issues if issue.get("session_id") == session_id]
        if not matches:
            raise ProbeToolInvocationError(
                f"no current partial probe workflow requires reconciliation for {session_id}"
            )
        if len(matches) != 1:
            raise ProbeToolInvocationError(
                f"multiple current partial workflow issues exist for {session_id}"
            )

        issue = matches[0]
        if issue.get("fingerprint") != fingerprint:
            raise ProbeToolInvocationError(
                "partial probe workflow fingerprint changed; refresh the agent plan before reconciling"
            )
        issue_incident_id = issue.get("incident_id")
        if issue_incident_id not in (None, snapshot["incident_id"]):
            raise ProbeToolInvocationError(
                "partial probe workflow belongs to a different incident than the current diagnosis"
            )
        return issue

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
                    issue = self._current_recovery_issue(
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
        if result.get("issue_kind") != issue.get("issue_kind"):
            raise ProbeToolInvocationError(
                "reconciliation result issue kind does not match the current partial state"
            )
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

    def call(self, name: str, arguments: Any) -> dict[str, Any]:
        if name == RECONCILE_PARTIAL_TOOL_NAME:
            return self.reconcile_partial(arguments)
        return super().call(name, arguments)
