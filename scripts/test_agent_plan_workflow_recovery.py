#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agent_plan_recovery import build_agent_plan_projection
from causal_projection import load_concepts, load_edges
from diagnosis_http_api import DiagnosisSnapshotReader
from diagnosis_mcp_server import (
    AGENT_PLAN_URI,
    CLIENT_CAPABILITIES_META_KEY,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
    DiagnosisMcpServer,
)
from live_diagnosis import build_diagnosis_snapshot
from probe_workflow_reconciliation import (
    reconcile_partial_probe_workflow,
    scan_partial_probe_workflows,
)

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "examples" / "runtime-evidence" / "network-corruption-chain.yaml"
AS_OF = datetime(2026, 9, 11, 14, 48, tzinfo=timezone.utc)
TARGET = "observation.network.tcp_retransmissions"
PROBE = "probe.network.inspect_tcp_integrity_errors"
EXECUTOR = "executor.linux.proc_net_snmp.tcp_inerrs"
SESSION_ID = "probe-session.0123456789abcdef"


class AgentPlanWorkflowRecoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)
        with EVIDENCE_PATH.open("r", encoding="utf-8") as handle:
            evidence = yaml.safe_load(handle)
        cls.evidence = copy.deepcopy(evidence)
        cls.evidence["instances"] = [
            instance
            for instance in cls.evidence["instances"]
            if instance["observation"] == TARGET
        ]

    def build_snapshot(self) -> dict:
        snapshot = build_diagnosis_snapshot(
            copy.deepcopy(self.evidence),
            self.concepts,
            self.edges,
            as_of=AS_OF,
            evidence_revision=9,
        )
        diagnosis = next(
            diagnosis
            for partition in snapshot["partitions"]
            for diagnosis in partition["diagnoses"]
            if diagnosis["target"] == TARGET
        )
        diagnosis["probe_execution"].update(
            {
                "probe_id": PROBE,
                "registered": True,
                "executable_here": True,
                "unavailable_reason": None,
                "executor": {"id": EXECUTOR, "platform": "linux"},
                "capability": "capability.network.inspect_tcp_integrity_errors",
                "observation": "observation.network.tcp_integrity_errors",
                "source": "/proc/net/snmp",
                "policy": {"classification": "delta_positive"},
            }
        )
        return snapshot

    @staticmethod
    def modern_meta() -> dict:
        return {
            PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
            CLIENT_CAPABILITIES_META_KEY: {},
        }

    def read_plan(self, server: DiagnosisMcpServer, request_id: int) -> dict:
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "resources/read",
                "params": {"uri": AGENT_PLAN_URI, "_meta": self.modern_meta()},
            }
        )
        self.assertIsNotNone(response)
        self.assertNotIn("error", response)
        return json.loads(response["result"]["contents"][0]["text"])

    @staticmethod
    def write_json(path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def test_normal_plan_keeps_existing_semantics_and_reports_zero_recovery(self) -> None:
        plan = build_agent_plan_projection(
            self.build_snapshot(),
            active_execution_enabled=False,
            recovery_issues=[],
        )
        step = next(step for step in plan["steps"] if step["target"] == TARGET)
        self.assertEqual("blocked", step["state"])
        self.assertEqual("active_execution_disabled", step["reason"])
        self.assertEqual(0, plan["summary"]["workflow_recovery_required"])

    def test_recovery_issue_gates_normal_diagnostic_actions(self) -> None:
        snapshot = self.build_snapshot()
        issue = {
            "session_id": SESSION_ID,
            "incident_id": snapshot["incident_id"],
            "issue_kind": "orphan_session",
            "fingerprint": "a" * 64,
            "files": [f"{SESSION_ID}.json"],
            "recovery": "discard_partial_state",
        }
        plan = build_agent_plan_projection(
            snapshot,
            active_execution_enabled=True,
            recovery_issues=[issue],
        )
        self.assertEqual(1, len(plan["steps"]))
        step = plan["steps"][0]
        self.assertIsNone(step["target"])
        self.assertEqual("workflow_recovery_required", step["state"])
        self.assertEqual("partial_probe_workflow_state", step["reason"])
        self.assertFalse(step["allowed"])
        self.assertEqual("reconcile_partial_workflow", step["fallback"])
        self.assertEqual(SESSION_ID, step["recovery"]["session_id"])
        self.assertEqual("orphan_session", step["recovery"]["issue_kind"])
        self.assertEqual(1, plan["summary"]["workflow_recovery_required"])
        self.assertEqual(0, plan["summary"]["actionable"])

    def test_mcp_agent_plan_surfaces_real_partial_state_and_resumes_after_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            snapshot_path = directory / "diagnosis.json"
            session_dir = directory / "sessions"
            snapshot = self.build_snapshot()
            self.write_json(snapshot_path, snapshot)

            binding = {
                "schema_version": "0.1",
                "kind": "mcp_probe_binding",
                "session_id": SESSION_ID,
                "incident_id": snapshot["incident_id"],
                "target": TARGET,
                "probe_id": PROBE,
                "scope": snapshot["partitions"][0]["scope"],
                "diagnosis_revision": snapshot["evidence_revision"],
                "diagnosis_etag": "test-etag",
            }
            self.write_json(session_dir / f"{SESSION_ID}.binding.json", binding)

            reader = DiagnosisSnapshotReader(snapshot_path)
            server = DiagnosisMcpServer(
                reader,
                probe_recovery_provider=lambda incident_id: scan_partial_probe_workflows(
                    session_dir,
                    incident_id=incident_id,
                ),
            )

            plan = self.read_plan(server, 1)
            self.assertEqual(1, plan["summary"]["workflow_recovery_required"])
            step = plan["steps"][0]
            self.assertEqual("workflow_recovery_required", step["state"])
            self.assertEqual("orphan_binding", step["recovery"]["issue_kind"])
            self.assertEqual(SESSION_ID, step["recovery"]["session_id"])

            reconciled = reconcile_partial_probe_workflow(
                session_dir,
                SESSION_ID,
                reconciled_at=AS_OF,
            )
            self.assertFalse(reconciled["already_reconciled"])

            resumed = self.read_plan(server, 2)
            self.assertEqual(0, resumed["summary"]["workflow_recovery_required"])
            self.assertTrue(any(step["target"] == TARGET for step in resumed["steps"]))
            self.assertFalse(any(step["state"] == "workflow_recovery_required" for step in resumed["steps"]))

    def test_unknown_incident_partial_state_is_still_projected_fail_closed(self) -> None:
        snapshot = self.build_snapshot()
        issue = {
            "session_id": SESSION_ID,
            "incident_id": None,
            "issue_kind": "orphan_session",
            "fingerprint": "b" * 64,
            "files": [f"{SESSION_ID}.json"],
            "recovery": "discard_partial_state",
        }
        plan = build_agent_plan_projection(
            snapshot,
            active_execution_enabled=False,
            recovery_issues=[issue],
        )
        self.assertEqual("workflow_recovery_required", plan["steps"][0]["state"])
        self.assertIsNone(plan["steps"][0]["recovery"]["incident_id"])


if __name__ == "__main__":
    unittest.main()
