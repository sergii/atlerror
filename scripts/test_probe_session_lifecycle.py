#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_plan import ABANDON_OPERATION
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
from mcp_probe_tools import (
    ABANDON_TOOL_NAME,
    BEGIN_TOOL_NAME,
    FINISH_TOOL_NAME,
    RecommendedProbeToolController,
)
from probe_executor_registry import default_executor_registry
from probe_session_state import discover_pending_probe_sessions

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 9, 11, 16, 31, 20, tzinfo=timezone.utc)
TARGET = "observation.network.tcp_retransmissions"
PROBE = "probe.network.inspect_tcp_integrity_errors"


def tcp_snmp(inerrs: int) -> str:
    return f"Tcp: InSegs OutSegs InErrs\nTcp: 100 90 {inerrs}\n"


class ProbeSessionLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)

    @staticmethod
    def write_json(path: Path, document: dict) -> None:
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def modern_meta() -> dict:
        return {
            PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
            CLIENT_CAPABILITIES_META_KEY: {},
            CLIENT_INFO_META_KEY: {"name": "atlerror-lifecycle-test", "version": "1.0.0"},
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
        now: list[datetime],
        *,
        max_age_seconds: int = 10,
    ) -> tuple[DiagnosisMcpServer, Path, Path, Path, Path]:
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
            executor_registry=default_executor_registry({PROBE: source_path}),
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
            clock=lambda: now[0],
            max_session_age_seconds=max_age_seconds,
        )
        provider = lambda: discover_pending_probe_sessions(
            session_dir=session_dir,
            runtime_evidence_path=evidence_path,
            concepts=self.concepts,
            as_of=now[0],
            max_age_seconds=max_age_seconds,
        )
        server = DiagnosisMcpServer(
            reader,
            probe_tools=controller,
            probe_session_provider=provider,
        )
        return server, evidence_path, snapshot_path, source_path, session_dir

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
    def target_step(plan: dict) -> dict:
        matches = [step for step in plan["steps"] if step["target"] == TARGET]
        if len(matches) != 1:
            raise AssertionError(f"expected one target step, got {len(matches)}")
        return matches[0]

    def test_duplicate_begin_is_refused_until_pending_session_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            now = [START]
            server, _evidence_path, _snapshot_path, _source_path, _session_dir = self.build_server(
                Path(directory_name),
                now,
            )
            begun = self.modern_request(
                server,
                1,
                "tools/call",
                {"name": BEGIN_TOOL_NAME, "arguments": {"target": TARGET}},
            )["result"]["structuredContent"]

            duplicate = self.modern_request(
                server,
                2,
                "tools/call",
                {"name": BEGIN_TOOL_NAME, "arguments": {"target": TARGET}},
            )["result"]
            self.assertTrue(duplicate["isError"])
            self.assertIn("unfinished probe session already exists", duplicate["content"][0]["text"])
            self.assertIn(begun["session_id"], duplicate["content"][0]["text"])

    def test_expired_session_cannot_finish_and_can_be_abandoned_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            now = [START]
            server, evidence_path, _snapshot_path, source_path, _session_dir = self.build_server(
                Path(directory_name),
                now,
                max_age_seconds=10,
            )
            initial_evidence = evidence_path.read_text(encoding="utf-8")
            begun = self.modern_request(
                server,
                1,
                "tools/call",
                {"name": BEGIN_TOOL_NAME, "arguments": {"target": TARGET}},
            )["result"]["structuredContent"]
            session_id = begun["session_id"]
            self.assertEqual("active", begun["lifecycle_state"])

            now[0] = START + timedelta(seconds=11)
            expired_plan = self.read_plan(server, 2)
            step = self.target_step(expired_plan)
            self.assertEqual("probe_session_expired", step["state"])
            self.assertEqual("ready_to_abandon_expired_probe", step["reason"])
            self.assertEqual(ABANDON_OPERATION, step["operation"])
            self.assertEqual({"sessionId": session_id}, step["arguments"])
            self.assertTrue(step["allowed"])
            self.assertEqual("expired", step["session"]["lifecycle_state"])
            self.assertEqual(1, expired_plan["summary"]["probe_session_expired"])
            self.assertEqual(0, expired_plan["summary"]["actionable"])

            source_path.write_text(tcp_snmp(5), encoding="utf-8")
            refused_finish = self.modern_request(
                server,
                3,
                "tools/call",
                {"name": FINISH_TOOL_NAME, "arguments": {"sessionId": session_id}},
            )["result"]
            self.assertTrue(refused_finish["isError"])
            self.assertIn("probe session expired", refused_finish["content"][0]["text"])
            self.assertEqual(initial_evidence, evidence_path.read_text(encoding="utf-8"))

            abandoned = self.modern_request(
                server,
                4,
                "tools/call",
                {"name": ABANDON_TOOL_NAME, "arguments": {"sessionId": session_id}},
            )["result"]
            self.assertFalse(abandoned["isError"])
            self.assertFalse(abandoned["structuredContent"]["already_abandoned"])

            repeated = self.modern_request(
                server,
                5,
                "tools/call",
                {"name": ABANDON_TOOL_NAME, "arguments": {"sessionId": session_id}},
            )["result"]
            self.assertFalse(repeated["isError"])
            self.assertTrue(repeated["structuredContent"]["already_abandoned"])
            self.assertEqual(initial_evidence, evidence_path.read_text(encoding="utf-8"))

            after = self.read_plan(server, 6)
            step = self.target_step(after)
            self.assertEqual("actionable", step["state"])
            self.assertIsNone(step["session"])
            self.assertEqual(0, after["summary"]["probe_session_expired"])

            now[0] = START + timedelta(seconds=12)
            restarted = self.modern_request(
                server,
                7,
                "tools/call",
                {"name": BEGIN_TOOL_NAME, "arguments": {"target": TARGET}},
            )["result"]
            self.assertFalse(restarted["isError"])
            self.assertNotEqual(session_id, restarted["structuredContent"]["session_id"])

    def test_active_session_can_be_abandoned_without_creating_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            now = [START]
            server, evidence_path, _snapshot_path, _source_path, _session_dir = self.build_server(
                Path(directory_name),
                now,
            )
            initial_evidence = evidence_path.read_text(encoding="utf-8")
            begun = self.modern_request(
                server,
                1,
                "tools/call",
                {"name": BEGIN_TOOL_NAME, "arguments": {"target": TARGET}},
            )["result"]["structuredContent"]
            session_id = begun["session_id"]

            abandoned = self.modern_request(
                server,
                2,
                "tools/call",
                {"name": ABANDON_TOOL_NAME, "arguments": {"sessionId": session_id}},
            )["result"]
            self.assertFalse(abandoned["isError"])
            self.assertEqual("abandoned", abandoned["structuredContent"]["status"])
            self.assertEqual(initial_evidence, evidence_path.read_text(encoding="utf-8"))

            finish = self.modern_request(
                server,
                3,
                "tools/call",
                {"name": FINISH_TOOL_NAME, "arguments": {"sessionId": session_id}},
            )["result"]
            self.assertTrue(finish["isError"])
            self.assertIn("was abandoned", finish["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
