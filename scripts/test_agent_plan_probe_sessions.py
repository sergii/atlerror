#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_plan import FINISH_OPERATION, build_agent_plan
from causal_projection import load_concepts, load_edges
from demo_checkout_stripe import DEFAULT_AS_OF, build_demo
from diagnosis_http_api import DiagnosisSnapshotReader
from diagnosis_mcp_server import (
    AGENT_PLAN_URI,
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
    DiagnosisMcpServer,
)
from live_diagnosis import build_diagnosis_snapshot
from mcp_probe_tools import BEGIN_TOOL_NAME, FINISH_TOOL_NAME, RecommendedProbeToolController
from probe_executor_registry import default_executor_registry
from probe_session_state import discover_pending_probe_sessions

ROOT = Path(__file__).resolve().parents[1]
CLOCK = datetime(2026, 9, 11, 16, 31, 20, tzinfo=timezone.utc)
TARGET = "observation.network.tcp_retransmissions"
EXPECTED_PROBE = "probe.network.inspect_tcp_integrity_errors"
EXPECTED_EXECUTOR = "executor.linux.proc_net_snmp.tcp_inerrs"


def tcp_snmp(inerrs: int) -> str:
    return f"Tcp: InSegs OutSegs InErrs\nTcp: 100 90 {inerrs}\n"


class AgentPlanProbeSessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)

    @staticmethod
    def write_json(path: Path, document: dict) -> None:
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def modern_meta() -> dict:
        return {
            PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
            CLIENT_CAPABILITIES_META_KEY: {},
            CLIENT_INFO_META_KEY: {"name": "atlerror-session-plan-test", "version": "1.0.0"},
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
    ) -> tuple[DiagnosisMcpServer, Path, Path]:
        demo = build_demo(root=ROOT)
        evidence_path = directory / "runtime-evidence.json"
        snapshot_path = directory / "diagnosis.json"
        source_path = directory / "proc-net-snmp"
        session_dir = directory / "sessions"
        source_path.write_text(tcp_snmp(0), encoding="utf-8")

        snapshot = build_diagnosis_snapshot(
            demo["runtime_evidence"],
            self.concepts,
            self.edges,
            as_of=DEFAULT_AS_OF,
            evidence_revision=1,
            executor_registry=default_executor_registry({EXPECTED_PROBE: source_path}),
        )
        self.write_json(evidence_path, demo["runtime_evidence"])
        self.write_json(snapshot_path, snapshot)

        reader = DiagnosisSnapshotReader(snapshot_path)
        controller = RecommendedProbeToolController(
            reader=reader,
            runtime_evidence_path=evidence_path,
            snapshot_path=snapshot_path,
            concepts=self.concepts,
            edges=self.edges,
            session_dir=session_dir,
            source_path=source_path,
            clock=lambda: CLOCK,
        )
        provider = lambda: discover_pending_probe_sessions(
            session_dir=session_dir,
            runtime_evidence_path=evidence_path,
            concepts=self.concepts,
            as_of=CLOCK,
            max_age_seconds=controller.max_session_age_seconds,
        )
        return (
            DiagnosisMcpServer(
                reader,
                probe_tools=controller,
                probe_session_provider=provider,
            ),
            source_path,
            snapshot_path,
        )

    def read_plan(self, server: DiagnosisMcpServer, request_id: int) -> dict:
        response = self.modern_request(
            server,
            request_id,
            "resources/read",
            {"uri": AGENT_PLAN_URI},
        )
        self.assertNotIn("error", response)
        return json.loads(response["result"]["contents"][0]["text"])

    @staticmethod
    def target_steps(plan: dict) -> list[dict]:
        return [step for step in plan["steps"] if step["target"] == TARGET]

    def test_agent_plan_moves_from_begin_to_in_progress_to_recomputed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            server, source_path, _snapshot_path = self.build_server(Path(directory_name))

            before = self.read_plan(server, 1)
            retransmission = self.target_steps(before)
            self.assertEqual(1, len(retransmission))
            self.assertEqual("actionable", retransmission[0]["state"])
            self.assertEqual(BEGIN_TOOL_NAME, retransmission[0]["operation"])
            self.assertIsNone(retransmission[0]["session"])
            self.assertEqual(0, before["summary"]["probe_in_progress"])

            begun = self.modern_request(
                server,
                2,
                "tools/call",
                {"name": BEGIN_TOOL_NAME, "arguments": {"target": TARGET}},
            )["result"]["structuredContent"]
            session_id = begun["session_id"]

            in_progress = self.read_plan(server, 3)
            retransmission = self.target_steps(in_progress)
            self.assertEqual(1, len(retransmission))
            step = retransmission[0]
            self.assertEqual("probe_in_progress", step["state"])
            self.assertEqual("ready_to_finish_probe", step["reason"])
            self.assertEqual(FINISH_OPERATION, step["operation"])
            self.assertEqual({"sessionId": session_id}, step["arguments"])
            self.assertTrue(step["allowed"])
            self.assertFalse(step["requires_opt_in"])
            self.assertEqual(session_id, step["session"]["id"])
            self.assertEqual(EXPECTED_PROBE, step["session"]["probe_id"])
            self.assertEqual(EXPECTED_EXECUTOR, step["session"]["executor_id"])
            self.assertEqual("active", step["session"]["lifecycle_state"])
            self.assertTrue(step["session"]["expires_at"])
            self.assertEqual(1, step["session"]["diagnosis_revision"])
            self.assertTrue(step["session"]["matches_current_recommendation"])
            self.assertEqual(1, in_progress["summary"]["probe_in_progress"])
            self.assertEqual(0, in_progress["summary"]["actionable"])

            source_path.write_text(tcp_snmp(3), encoding="utf-8")
            finished = self.modern_request(
                server,
                4,
                "tools/call",
                {"name": FINISH_TOOL_NAME, "arguments": {"sessionId": session_id}},
            )["result"]
            self.assertFalse(finished["isError"])

            after = self.read_plan(server, 5)
            self.assertEqual(0, after["summary"]["probe_in_progress"])
            self.assertTrue(
                all(
                    step.get("session") is None
                    or step["session"].get("id") != session_id
                    for step in after["steps"]
                )
            )
            self.assertEqual(2, after["evidence_revision"])

    def test_in_progress_session_remains_visible_when_execution_is_not_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            server, _source_path, snapshot_path = self.build_server(Path(directory_name))
            begun = self.modern_request(
                server,
                1,
                "tools/call",
                {"name": BEGIN_TOOL_NAME, "arguments": {"target": TARGET}},
            )["result"]["structuredContent"]
            session_id = begun["session_id"]

            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            scope = next(
                partition["scope"]
                for partition in snapshot["partitions"]
                if any(diagnosis["target"] == TARGET for diagnosis in partition["diagnoses"])
            )
            plan = build_agent_plan(
                snapshot,
                active_execution_enabled=False,
                active_sessions=[
                    {
                        "session_id": session_id,
                        "incident_id": snapshot["incident_id"],
                        "target": TARGET,
                        "probe_id": EXPECTED_PROBE,
                        "scope": scope,
                        "started_at": begun["started_at"],
                        "expires_at": begun["expires_at"],
                        "lifecycle_state": "active",
                        "diagnosis_revision": begun["diagnosis_revision"],
                        "executor_id": EXPECTED_EXECUTOR,
                    }
                ],
            )
            step = self.target_steps(plan)[0]
            self.assertEqual("probe_in_progress", step["state"])
            self.assertEqual("probe_in_progress_execution_disabled", step["reason"])
            self.assertEqual(FINISH_TOOL_NAME, step["operation"])
            self.assertFalse(step["allowed"])
            self.assertTrue(step["requires_opt_in"])
            self.assertEqual("enable_readonly_probe_tools", step["fallback"])


if __name__ == "__main__":
    unittest.main()
