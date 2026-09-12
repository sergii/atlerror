#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from causal_projection import load_concepts
from diagnosis_http_api import DiagnosisSnapshotReader
from diagnosis_mcp_server import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    MODERN_PROTOCOL_VERSION,
    PROBE_EXECUTION_CAPABILITIES_URI,
    PROTOCOL_VERSION_META_KEY,
    DiagnosisMcpServer,
)
from probe_executor_runtime import build_probe_execution_capabilities

ROOT = Path(__file__).resolve().parents[1]


class DiagnosisMcpCapabilitiesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)

    @staticmethod
    def modern_meta() -> dict:
        return {
            PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
            CLIENT_CAPABILITIES_META_KEY: {},
            CLIENT_INFO_META_KEY: {"name": "atlerror-capability-test", "version": "1.0.0"},
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

    def test_capability_resource_is_advertised_without_enabling_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = DiagnosisSnapshotReader(Path(directory) / "missing-diagnosis.json")
            server = DiagnosisMcpServer(
                reader,
                probe_capability_provider=lambda: build_probe_execution_capabilities(self.concepts),
            )

            discovered = self.modern_request(server, 1, "server/discover")["result"]
            self.assertEqual({"resources": {}}, discovered["capabilities"])
            self.assertIn(PROBE_EXECUTION_CAPABILITIES_URI, discovered["instructions"])

            listed = self.modern_request(server, 2, "resources/list")["result"]
            capability = next(
                resource
                for resource in listed["resources"]
                if resource["uri"] == PROBE_EXECUTION_CAPABILITIES_URI
            )
            self.assertEqual("probe_execution_capabilities", capability["name"])
            self.assertEqual("application/json", capability["mimeType"])
            self.assertIn("title", capability)

    def test_capability_resource_projects_registry_and_host_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = DiagnosisSnapshotReader(Path(directory) / "missing-diagnosis.json")
            server = DiagnosisMcpServer(
                reader,
                probe_capability_provider=lambda: build_probe_execution_capabilities(self.concepts),
            )

            result = self.modern_request(
                server,
                1,
                "resources/read",
                {"uri": PROBE_EXECUTION_CAPABILITIES_URI},
            )["result"]
            self.assertEqual("complete", result["resultType"])
            self.assertEqual(0, result["ttlMs"])
            self.assertEqual("private", result["cacheScope"])

            document = json.loads(result["contents"][0]["text"])
            self.assertEqual("probe_execution_capabilities", document["kind"])
            probe_ids = [entry["probe"]["id"] for entry in document["executors"]]
            self.assertEqual(
                [
                    "probe.cpu.inspect_utilization",
                    "probe.network.inspect_tcp_integrity_errors",
                ],
                probe_ids,
            )
            for entry in document["executors"]:
                self.assertIsInstance(entry["available"], bool)
                self.assertEqual("read_only", entry["probe"]["risk"])
                self.assertTrue(entry["source"])
                self.assertIsInstance(entry["policy"], dict)

    def test_capability_resource_is_absent_when_no_provider_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = DiagnosisMcpServer(
                DiagnosisSnapshotReader(Path(directory) / "missing-diagnosis.json")
            )
            response = self.modern_request(
                server,
                1,
                "resources/read",
                {"uri": PROBE_EXECUTION_CAPABILITIES_URI},
            )
            self.assertEqual(INVALID_PARAMS, response["error"]["code"])
            self.assertEqual(PROBE_EXECUTION_CAPABILITIES_URI, response["error"]["data"]["uri"])

    def test_capability_discovery_failure_is_a_resource_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def fail() -> dict:
                raise ValueError("registry mismatch")

            server = DiagnosisMcpServer(
                DiagnosisSnapshotReader(Path(directory) / "missing-diagnosis.json"),
                probe_capability_provider=fail,
            )
            response = self.modern_request(
                server,
                1,
                "resources/read",
                {"uri": PROBE_EXECUTION_CAPABILITIES_URI},
            )
            self.assertEqual(INTERNAL_ERROR, response["error"]["code"])
            self.assertIn("capability discovery failed", response["error"]["message"])

    def test_legacy_clients_can_read_the_same_capability_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = DiagnosisMcpServer(
                DiagnosisSnapshotReader(Path(directory) / "missing-diagnosis.json"),
                probe_capability_provider=lambda: build_probe_execution_capabilities(self.concepts),
            )
            initialized = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "legacy-capability-test", "version": "1.0"},
                    },
                }
            )
            self.assertEqual("2025-11-25", initialized["result"]["protocolVersion"])
            server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "resources/read",
                    "params": {"uri": PROBE_EXECUTION_CAPABILITIES_URI},
                }
            )
            document = json.loads(response["result"]["contents"][0]["text"])
            self.assertEqual("probe_execution_capabilities", document["kind"])
            self.assertNotIn("resultType", response["result"])


if __name__ == "__main__":
    unittest.main()
