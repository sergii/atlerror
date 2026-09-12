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
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
    DiagnosisMcpServer,
)
from mcp_probe_tools import (
    ABANDON_TOOL_NAME,
    BEGIN_TOOL_NAME,
    FINISH_TOOL_NAME,
    RecommendedProbeToolController,
)

ROOT = Path(__file__).resolve().parents[1]
CLOCK = datetime(2026, 9, 11, 16, 31, 20, tzinfo=timezone.utc)
TARGET = "observation.network.tcp_retransmissions"
EXPECTED_PROBE = "probe.network.inspect_tcp_integrity_errors"


def tcp_snmp(inerrs: int) -> str:
    return f"Tcp: InSegs OutSegs InErrs\nTcp: 100 90 {inerrs}\n"


class DiagnosisMcpProbeToolsTest(unittest.TestCase):
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
            CLIENT_INFO_META_KEY: {"name": "atlerror-probe-test", "version": "1.0.0"},
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

    def build_server(self, directory: Path) -> tuple[DiagnosisMcpServer, Path, Path, Path]:
        demo = build_demo(root=ROOT)
        evidence_path = directory / "runtime-evidence.json"
        snapshot_path = directory / "diagnosis.json"
        source_path = directory / "proc-net-snmp"
        session_dir = directory / "sessions"
        self.write_json(evidence_path, demo["runtime_evidence"])
        self.write_json(snapshot_path, demo["diagnosis"])
        source_path.write_text(tcp_snmp(0), encoding="utf-8")

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
        return DiagnosisMcpServer(reader, probe_tools=controller), evidence_path, snapshot_path, source_path

    def test_tools_are_absent_until_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            reader = DiagnosisSnapshotReader(directory / "diagnosis.json")
            server = DiagnosisMcpServer(reader)

            discovered = self.modern_request(server, 1, "server/discover")["result"]
            self.assertEqual({"resources": {}}, discovered["capabilities"])

            listed = self.modern_request(server, 2, "tools/list")
            self.assertEqual(METHOD_NOT_FOUND, listed["error"]["code"])

    def test_modern_discovery_and_tool_list_advertise_only_registered_probe_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            server, _evidence_path, _snapshot_path, _source_path = self.build_server(
                Path(directory_name)
            )

            discovered = self.modern_request(server, 1, "server/discover")["result"]
            self.assertEqual({"resources": {}, "tools": {}}, discovered["capabilities"])

            listed = self.modern_request(server, 2, "tools/list")["result"]
            self.assertEqual("complete", listed["resultType"])
            self.assertEqual(60_000, listed["ttlMs"])
            self.assertEqual("public", listed["cacheScope"])
            self.assertEqual(
                [ABANDON_TOOL_NAME, BEGIN_TOOL_NAME, FINISH_TOOL_NAME],
                [tool["name"] for tool in listed["tools"]],
            )
            self.assertTrue(all(tool["annotations"]["destructiveHint"] is False for tool in listed["tools"]))
            self.assertTrue(all(tool["annotations"]["openWorldHint"] is False for tool in listed["tools"]))

    def test_begin_is_bound_to_current_top_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            server, _evidence_path, _snapshot_path, _source_path = self.build_server(
                Path(directory_name)
            )

            begun = self.modern_request(
                server,
                1,
                "tools/call",
                {"name": BEGIN_TOOL_NAME, "arguments": {"target": TARGET}},
            )["result"]
            self.assertFalse(begun["isError"])
            payload = begun["structuredContent"]
            self.assertEqual("baseline_captured", payload["status"])
            self.assertEqual(EXPECTED_PROBE, payload["probe_id"])
            self.assertEqual(0, payload["baseline"]["value"])
            self.assertEqual(1, payload["diagnosis_revision"])
            self.assertEqual("active", payload["lifecycle_state"])
            self.assertTrue(payload["expires_at"])

            refused = self.modern_request(
                server,
                2,
                "tools/call",
                {
                    "name": BEGIN_TOOL_NAME,
                    "arguments": {
                        "target": TARGET,
                        "scope": {
                            "boundaries": ["boundary.application.external_dependency"],
                            "attributes": {"service": "checkout-api", "dependency": "other"},
                        },
                    },
                },
            )["result"]
            self.assertTrue(refused["isError"])
            self.assertIn("no current next-probe recommendation", refused["content"][0]["text"])

    def test_finish_appends_probe_evidence_and_recomputes_diagnosis_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            server, evidence_path, snapshot_path, source_path = self.build_server(directory)

            begun = self.modern_request(
                server,
                1,
                "tools/call",
                {"name": BEGIN_TOOL_NAME, "arguments": {"target": TARGET}},
            )["result"]["structuredContent"]
            session_id = begun["session_id"]

            source_path.write_text(tcp_snmp(3), encoding="utf-8")
            finished = self.modern_request(
                server,
                2,
                "tools/call",
                {"name": FINISH_TOOL_NAME, "arguments": {"sessionId": session_id}},
            )["result"]
            self.assertFalse(finished["isError"])
            payload = finished["structuredContent"]
            self.assertFalse(payload["already_completed"])
            self.assertEqual("observed", payload["observation"]["state"])
            self.assertEqual(3, payload["observation"]["measurement"]["delta"])
            self.assertEqual("hypothesis.network.packet_corruption", payload["top_hypothesis"])
            self.assertNotEqual(EXPECTED_PROBE, payload["next_probe"])
            self.assertEqual(2, payload["diagnosis_revision"])

            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            probe_instances = [
                instance
                for instance in evidence["instances"]
                if instance.get("labels", {}).get("session_id") == session_id
            ]
            self.assertEqual(1, len(probe_instances))
            self.assertEqual("probe", probe_instances[0]["source"]["type"])

            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            retransmission = next(
                diagnosis
                for partition in snapshot["partitions"]
                for diagnosis in partition["diagnoses"]
                if diagnosis["target"] == TARGET
            )
            self.assertEqual(
                "hypothesis.network.packet_corruption",
                retransmission["ranking"]["candidates"][0]["source"]["id"],
            )
            recommended_ids = [
                candidate["probe"]["id"]
                for candidate in retransmission["probe_ranking"].get("probes", [])
            ]
            self.assertNotIn(EXPECTED_PROBE, recommended_ids)

            repeated = self.modern_request(
                server,
                3,
                "tools/call",
                {"name": FINISH_TOOL_NAME, "arguments": {"sessionId": session_id}},
            )["result"]["structuredContent"]
            self.assertTrue(repeated["already_completed"])
            self.assertEqual(2, repeated["diagnosis_revision"])
            evidence_after_retry = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence, evidence_after_retry)

    def test_unknown_tool_is_protocol_error_while_probe_refusal_is_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            server, _evidence_path, _snapshot_path, _source_path = self.build_server(
                Path(directory_name)
            )

            unknown = self.modern_request(
                server,
                1,
                "tools/call",
                {"name": "atlerror.shell", "arguments": {}},
            )
            self.assertEqual(INVALID_PARAMS, unknown["error"]["code"])

            invalid = self.modern_request(
                server,
                2,
                "tools/call",
                {"name": BEGIN_TOOL_NAME, "arguments": {}},
            )["result"]
            self.assertTrue(invalid["isError"])
            self.assertIn("invalid tool arguments", invalid["content"][0]["text"])

    def test_legacy_tools_use_existing_handshake_without_modern_result_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            server, _evidence_path, _snapshot_path, _source_path = self.build_server(
                Path(directory_name)
            )
            initialized = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "legacy-probe-test", "version": "1.0"},
                    },
                }
            )
            self.assertEqual(
                {"resources": {}, "tools": {}},
                initialized["result"]["capabilities"],
            )
            server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = server.handle_message(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            )["result"]
            self.assertNotIn("resultType", listed)
            self.assertEqual(
                [ABANDON_TOOL_NAME, BEGIN_TOOL_NAME, FINISH_TOOL_NAME],
                [tool["name"] for tool in listed["tools"]],
            )


if __name__ == "__main__":
    unittest.main()
