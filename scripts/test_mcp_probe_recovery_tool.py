#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from causal_projection import load_concepts, load_edges
from demo_checkout_stripe import build_demo
from diagnosis_http_api import DiagnosisSnapshotReader
from diagnosis_mcp_server import (
    AGENT_PLAN_URI,
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
    DiagnosisMcpServer,
)
from mcp_probe_recovery_tool import (
    RECONCILE_PARTIAL_TOOL_NAME,
    RecoveryAwareProbeToolController,
)
from probe_workflow_reconciliation import (
    reconciliation_path,
    scan_partial_probe_workflows,
)

ROOT = Path(__file__).resolve().parents[1]
CLOCK = datetime(2026, 9, 12, 18, 15, tzinfo=timezone.utc)
SESSION_ID = "probe-session.0123456789abcdef"
TARGET = "observation.network.tcp_retransmissions"
PROBE = "probe.network.inspect_tcp_integrity_errors"


def tcp_snmp(inerrs: int) -> str:
    return f"Tcp: InSegs OutSegs InErrs\nTcp: 100 90 {inerrs}\n"


class McpProbeRecoveryToolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)

    @staticmethod
    def write_json(path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def modern_meta() -> dict:
        return {
            PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
            CLIENT_CAPABILITIES_META_KEY: {},
            CLIENT_INFO_META_KEY: {"name": "atlerror-recovery-test", "version": "1.0.0"},
        }

    def modern_request(
        self,
        server: DiagnosisMcpServer,
        request_id: int,
        method: str,
        params: dict | None = None,
    ) -> dict:
        full_params = dict(params or {})
        full_params["_meta"] = self.modern_meta()
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": full_params}
        )
        self.assertIsNotNone(response)
        return response

    def build_server(
        self,
        directory: Path,
    ) -> tuple[DiagnosisMcpServer, Path, Path, Path, dict]:
        demo = build_demo(root=ROOT)
        evidence_path = directory / "runtime-evidence.json"
        snapshot_path = directory / "diagnosis.json"
        source_path = directory / "proc-net-snmp"
        session_dir = directory / "sessions"
        self.write_json(evidence_path, demo["runtime_evidence"])
        self.write_json(snapshot_path, demo["diagnosis"])
        source_path.write_text(tcp_snmp(0), encoding="utf-8")

        reader = DiagnosisSnapshotReader(snapshot_path)
        controller = RecoveryAwareProbeToolController(
            reader=reader,
            runtime_evidence_path=evidence_path,
            snapshot_path=snapshot_path,
            concepts=self.concepts,
            edges=self.edges,
            session_dir=session_dir,
            source_path=source_path,
            clock=lambda: CLOCK,
        )
        server = DiagnosisMcpServer(
            reader,
            probe_tools=controller,
            probe_recovery_provider=lambda incident_id: scan_partial_probe_workflows(
                session_dir,
                incident_id=incident_id,
            ),
        )
        return server, evidence_path, snapshot_path, session_dir, demo["diagnosis"]

    def write_orphan_binding(
        self,
        session_dir: Path,
        snapshot: dict,
        *,
        incident_id: str | None = None,
    ) -> Path:
        binding = {
            "schema_version": "0.1",
            "kind": "mcp_probe_binding",
            "session_id": SESSION_ID,
            "incident_id": incident_id or snapshot["incident_id"],
            "target": TARGET,
            "probe_id": PROBE,
            "scope": snapshot["partitions"][0]["scope"],
            "diagnosis_revision": snapshot["evidence_revision"],
            "diagnosis_etag": "test-etag",
        }
        path = session_dir / f"{SESSION_ID}.binding.json"
        self.write_json(path, binding)
        return path

    def read_plan(self, server: DiagnosisMcpServer, request_id: int) -> dict:
        response = self.modern_request(
            server,
            request_id,
            "resources/read",
            {"uri": AGENT_PLAN_URI},
        )
        self.assertNotIn("error", response)
        return json.loads(response["result"]["contents"][0]["text"])

    def call_reconcile(self, server: DiagnosisMcpServer, request_id: int, arguments: dict) -> dict:
        return self.modern_request(
            server,
            request_id,
            "tools/call",
            {"name": RECONCILE_PARTIAL_TOOL_NAME, "arguments": arguments},
        )["result"]

    def test_tool_list_and_agent_plan_bind_exact_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            server, _evidence_path, _snapshot_path, session_dir, snapshot = self.build_server(directory)
            self.write_orphan_binding(session_dir, snapshot)

            listed = self.modern_request(server, 1, "tools/list")["result"]
            names = [tool["name"] for tool in listed["tools"]]
            self.assertIn(RECONCILE_PARTIAL_TOOL_NAME, names)
            descriptor = next(
                tool for tool in listed["tools"] if tool["name"] == RECONCILE_PARTIAL_TOOL_NAME
            )
            self.assertEqual(["sessionId", "fingerprint"], descriptor["inputSchema"]["required"])
            self.assertTrue(descriptor["annotations"]["idempotentHint"])
            self.assertFalse(descriptor["annotations"]["destructiveHint"])

            plan = self.read_plan(server, 2)
            step = plan["steps"][0]
            self.assertEqual("workflow_recovery_required", step["state"])
            self.assertEqual(RECONCILE_PARTIAL_TOOL_NAME, step["operation"])
            self.assertTrue(step["allowed"])
            self.assertFalse(step["requires_opt_in"])
            self.assertEqual("none", step["fallback"])
            self.assertEqual(SESSION_ID, step["arguments"]["sessionId"])
            self.assertEqual(step["recovery"]["fingerprint"], step["arguments"]["fingerprint"])

    def test_reconcile_marks_exact_state_and_plan_resumes_without_evidence_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            server, evidence_path, snapshot_path, session_dir, snapshot = self.build_server(directory)
            self.write_orphan_binding(session_dir, snapshot)
            evidence_before = evidence_path.read_bytes()
            snapshot_before = snapshot_path.read_bytes()

            plan = self.read_plan(server, 1)
            arguments = plan["steps"][0]["arguments"]
            result = self.call_reconcile(server, 2, arguments)
            self.assertFalse(result["isError"])
            payload = result["structuredContent"]
            self.assertEqual("reconciled", payload["status"])
            self.assertFalse(payload["already_reconciled"])
            self.assertEqual(arguments["fingerprint"], payload["fingerprint"])
            self.assertEqual("discard_partial_state", payload["resolution"])
            self.assertTrue(reconciliation_path(session_dir, SESSION_ID).exists())
            self.assertEqual([], scan_partial_probe_workflows(session_dir, incident_id=snapshot["incident_id"]))
            self.assertEqual(evidence_before, evidence_path.read_bytes())
            self.assertEqual(snapshot_before, snapshot_path.read_bytes())

            resumed = self.read_plan(server, 3)
            self.assertEqual(0, resumed["summary"]["workflow_recovery_required"])
            self.assertTrue(any(step["target"] == TARGET for step in resumed["steps"]))

    def test_same_fingerprint_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            server, evidence_path, snapshot_path, session_dir, snapshot = self.build_server(directory)
            self.write_orphan_binding(session_dir, snapshot)
            plan = self.read_plan(server, 1)
            arguments = plan["steps"][0]["arguments"]

            first = self.call_reconcile(server, 2, arguments)["structuredContent"]
            second = self.call_reconcile(server, 3, arguments)["structuredContent"]
            self.assertFalse(first["already_reconciled"])
            self.assertTrue(second["already_reconciled"])
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            self.assertEqual(first["reconciled_at"], second["reconciled_at"])
            self.assertEqual(
                json.loads(evidence_path.read_text(encoding="utf-8")),
                json.loads(evidence_path.read_text(encoding="utf-8")),
            )
            self.assertEqual(snapshot["evidence_revision"], json.loads(snapshot_path.read_text())["evidence_revision"])

    def test_stale_fingerprint_is_refused_after_partial_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            server, _evidence_path, _snapshot_path, session_dir, snapshot = self.build_server(directory)
            binding_path = self.write_orphan_binding(session_dir, snapshot)
            plan = self.read_plan(server, 1)
            stale_arguments = plan["steps"][0]["arguments"]

            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            binding["diagnosis_etag"] = "changed-after-plan"
            self.write_json(binding_path, binding)

            refused = self.call_reconcile(server, 2, stale_arguments)
            self.assertTrue(refused["isError"])
            self.assertIn("fingerprint changed", refused["content"][0]["text"])
            self.assertFalse(reconciliation_path(session_dir, SESSION_ID).exists())

            refreshed = self.read_plan(server, 3)
            self.assertNotEqual(
                stale_arguments["fingerprint"],
                refreshed["steps"][0]["arguments"]["fingerprint"],
            )

    def test_tool_refuses_partial_state_owned_by_another_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            server, _evidence_path, _snapshot_path, session_dir, snapshot = self.build_server(directory)
            self.write_orphan_binding(session_dir, snapshot, incident_id="incident.other")
            issue = scan_partial_probe_workflows(session_dir)[0]

            refused = self.call_reconcile(
                server,
                1,
                {"sessionId": SESSION_ID, "fingerprint": issue["fingerprint"]},
            )
            self.assertTrue(refused["isError"])
            self.assertIn("does not belong to the current diagnosis incident", refused["content"][0]["text"])
            self.assertFalse(reconciliation_path(session_dir, SESSION_ID).exists())

    def test_unknown_incident_partial_state_can_be_explicitly_reconciled_by_exact_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            server, _evidence_path, _snapshot_path, session_dir, _snapshot = self.build_server(directory)
            orphan = session_dir / f"{SESSION_ID}.json"
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_text('{"partial":true}\n', encoding="utf-8")

            plan = self.read_plan(server, 1)
            step = plan["steps"][0]
            self.assertIsNone(step["recovery"]["incident_id"])
            reconciled = self.call_reconcile(server, 2, step["arguments"])
            self.assertFalse(reconciled["isError"])
            self.assertIsNone(reconciled["structuredContent"]["incident_id"])


if __name__ == "__main__":
    unittest.main()
