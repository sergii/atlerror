#!/usr/bin/env python3

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from causal_projection import load_concepts, load_edges
from diagnosis_http_api import DiagnosisSnapshotReader
from diagnosis_mcp_server import (
    AGENT_PLAN_URI,
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    CURRENT_DIAGNOSIS_URI,
    DIAGNOSIS_STATUS_URI,
    INVALID_PARAMS,
    LEGACY_RESOURCE_NOT_FOUND,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
    UNSUPPORTED_PROTOCOL_VERSION,
    DiagnosisMcpServer,
    serve_stdio,
)
from live_diagnosis import build_diagnosis_snapshot

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "examples" / "runtime-evidence" / "network-corruption-chain.yaml"
AS_OF = datetime(2026, 9, 11, 14, 48, tzinfo=timezone.utc)


class DiagnosisMcpServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)
        with EVIDENCE_PATH.open("r", encoding="utf-8") as handle:
            cls.evidence = yaml.safe_load(handle)

    def build_snapshot(self, revision: int = 1) -> dict:
        return build_diagnosis_snapshot(
            self.evidence,
            self.concepts,
            self.edges,
            as_of=AS_OF,
            evidence_revision=revision,
        )

    @staticmethod
    def write_snapshot(path: Path, snapshot: dict) -> None:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def modern_meta(version: str = MODERN_PROTOCOL_VERSION) -> dict:
        return {
            PROTOCOL_VERSION_META_KEY: version,
            CLIENT_CAPABILITIES_META_KEY: {},
            CLIENT_INFO_META_KEY: {"name": "atlerror-test", "version": "1.0.0"},
        }

    def request(
        self,
        server: DiagnosisMcpServer,
        request_id: int,
        method: str,
        params: dict | None = None,
    ) -> dict:
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        response = server.handle_message(message)
        self.assertIsNotNone(response)
        return response

    def modern_request(
        self,
        server: DiagnosisMcpServer,
        request_id: int,
        method: str,
        params: dict | None = None,
    ) -> dict:
        full_params = dict(params or {})
        full_params["_meta"] = self.modern_meta()
        return self.request(server, request_id, method, full_params)

    def test_modern_discovery_and_resource_list_are_stateless(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = DiagnosisSnapshotReader(Path(directory) / "diagnosis.json")
            server = DiagnosisMcpServer(reader)

            discovered = self.modern_request(server, 1, "server/discover")["result"]
            self.assertEqual("complete", discovered["resultType"])
            self.assertEqual([MODERN_PROTOCOL_VERSION], discovered["supportedVersions"])
            self.assertEqual({"resources": {}}, discovered["capabilities"])
            self.assertEqual(60_000, discovered["ttlMs"])
            self.assertEqual("public", discovered["cacheScope"])
            self.assertEqual(
                "atlerror-diagnosis",
                discovered["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
            )

            listed = self.modern_request(server, 2, "resources/list")["result"]
            self.assertEqual("complete", listed["resultType"])
            self.assertEqual(60_000, listed["ttlMs"])
            self.assertEqual("public", listed["cacheScope"])
            self.assertEqual(
                [AGENT_PLAN_URI, CURRENT_DIAGNOSIS_URI, DIAGNOSIS_STATUS_URI],
                [resource["uri"] for resource in listed["resources"]],
            )
            self.assertTrue(all("title" in resource for resource in listed["resources"]))

    def test_modern_current_and_status_resources_serve_existing_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "diagnosis.json"
            expected = self.build_snapshot(revision=7)
            self.write_snapshot(snapshot_path, expected)
            server = DiagnosisMcpServer(DiagnosisSnapshotReader(snapshot_path))

            current = self.modern_request(
                server,
                1,
                "resources/read",
                {"uri": CURRENT_DIAGNOSIS_URI},
            )["result"]
            self.assertEqual("complete", current["resultType"])
            self.assertEqual(0, current["ttlMs"])
            self.assertEqual("private", current["cacheScope"])
            self.assertEqual(
                expected,
                json.loads(current["contents"][0]["text"]),
            )
            self.assertEqual("application/json", current["contents"][0]["mimeType"])

            status = self.modern_request(
                server,
                2,
                "resources/read",
                {"uri": DIAGNOSIS_STATUS_URI},
            )["result"]
            status_document = json.loads(status["contents"][0]["text"])
            self.assertEqual("ready", status_document["state"])
            self.assertEqual(7, status_document["evidence_revision"])
            self.assertEqual("incident.network.retransmission_spike", status_document["incident_id"])

    def test_modern_missing_or_unknown_resource_uses_invalid_params(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = DiagnosisMcpServer(
                DiagnosisSnapshotReader(Path(directory) / "diagnosis.json")
            )
            missing = self.modern_request(
                server,
                1,
                "resources/read",
                {"uri": CURRENT_DIAGNOSIS_URI},
            )
            self.assertEqual(INVALID_PARAMS, missing["error"]["code"])
            self.assertEqual(CURRENT_DIAGNOSIS_URI, missing["error"]["data"]["uri"])

            unknown = self.modern_request(
                server,
                2,
                "resources/read",
                {"uri": "atlerror://diagnosis/unknown"},
            )
            self.assertEqual(INVALID_PARAMS, unknown["error"]["code"])

    def test_modern_rejects_unsupported_protocol_version_with_supported_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = DiagnosisMcpServer(
                DiagnosisSnapshotReader(Path(directory) / "diagnosis.json")
            )
            response = self.request(
                server,
                1,
                "resources/list",
                {
                    "_meta": {
                        **self.modern_meta("2027-01-01"),
                    }
                },
            )
            self.assertEqual(UNSUPPORTED_PROTOCOL_VERSION, response["error"]["code"])
            self.assertEqual([MODERN_PROTOCOL_VERSION], response["error"]["data"]["supported"])
            self.assertEqual("2027-01-01", response["error"]["data"]["requested"])

    def test_modern_requires_client_capabilities_in_request_meta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = DiagnosisMcpServer(
                DiagnosisSnapshotReader(Path(directory) / "diagnosis.json")
            )
            response = self.request(
                server,
                1,
                "resources/list",
                {"_meta": {PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION}},
            )
            self.assertEqual(INVALID_PARAMS, response["error"]["code"])
            self.assertIn(CLIENT_CAPABILITIES_META_KEY, response["error"]["message"])

    def test_legacy_initialize_and_resources_remain_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "diagnosis.json"
            expected = self.build_snapshot(revision=3)
            self.write_snapshot(snapshot_path, expected)
            server = DiagnosisMcpServer(DiagnosisSnapshotReader(snapshot_path))

            initialized = self.request(
                server,
                1,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-test", "version": "1.0"},
                },
            )["result"]
            self.assertEqual("2025-11-25", initialized["protocolVersion"])
            self.assertEqual({"resources": {}}, initialized["capabilities"])
            self.assertNotIn("resultType", initialized)

            notification_response = server.handle_message(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            self.assertIsNone(notification_response)

            listed = self.request(server, 2, "resources/list", {})["result"]
            self.assertNotIn("resultType", listed)
            self.assertNotIn("ttlMs", listed)
            self.assertTrue(all("title" not in resource for resource in listed["resources"]))

            current = self.request(
                server,
                3,
                "resources/read",
                {"uri": CURRENT_DIAGNOSIS_URI},
            )["result"]
            self.assertEqual(expected, json.loads(current["contents"][0]["text"]))

            missing = self.request(
                server,
                4,
                "resources/read",
                {"uri": "atlerror://diagnosis/unknown"},
            )
            self.assertEqual(LEGACY_RESOURCE_NOT_FOUND, missing["error"]["code"])

    def test_stdio_transport_emits_only_single_line_json_rpc_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = DiagnosisMcpServer(
                DiagnosisSnapshotReader(Path(directory) / "diagnosis.json")
            )
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": self.modern_meta()},
            }
            input_stream = io.StringIO(json.dumps(request) + "\nnot-json\n")
            output_stream = io.StringIO()
            error_stream = io.StringIO()

            exit_code = serve_stdio(
                server,
                input_stream=input_stream,
                output_stream=output_stream,
                error_stream=error_stream,
                verbose=True,
            )
            self.assertEqual(0, exit_code)
            lines = output_stream.getvalue().splitlines()
            self.assertEqual(2, len(lines))
            first = json.loads(lines[0])
            second = json.loads(lines[1])
            self.assertEqual(1, first["id"])
            self.assertEqual(-32700, second["error"]["code"])
            self.assertTrue(all("\n" not in line for line in lines))
            self.assertEqual(
                "",
                output_stream.getvalue()
                .replace(lines[0] + "\n", "", 1)
                .replace(lines[1] + "\n", "", 1),
            )


if __name__ == "__main__":
    unittest.main()
