#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

from causal_projection import load_concepts, load_edges
from diagnosis_http_api import DiagnosisSnapshotReader, make_handler
from live_diagnosis import build_diagnosis_snapshot

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "examples" / "runtime-evidence" / "network-corruption-chain.yaml"
AS_OF = datetime(2026, 9, 11, 14, 48, tzinfo=timezone.utc)


class ApiHarness:
    def __init__(self, snapshot_path: Path) -> None:
        reader = DiagnosisSnapshotReader(snapshot_path)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(reader))
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict | None, dict[str, str]]:
        request = Request(
            self.base_url + path,
            data=body,
            headers=headers or {},
            method=method,
        )
        try:
            with urlopen(request, timeout=2) as response:
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else None
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                return response.status, payload, response_headers
        except HTTPError as exc:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else None
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
            return exc.code, payload, response_headers


class DiagnosisHttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.concepts = load_concepts(ROOT)
        cls.edges = load_edges(ROOT)
        with EVIDENCE_PATH.open("r", encoding="utf-8") as handle:
            cls.evidence = yaml.safe_load(handle)

    def build_snapshot(self, revision: int) -> dict:
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

    def test_health_and_waiting_status_work_before_snapshot_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "diagnosis.json"
            api = ApiHarness(snapshot_path)
            try:
                status, health, _ = api.request("GET", "/health")
                self.assertEqual(200, status)
                self.assertEqual({"status": "ok"}, health)

                status, payload, _ = api.request("GET", "/status")
                self.assertEqual(200, status)
                self.assertEqual("waiting_for_snapshot", payload["state"])
                self.assertFalse(payload["snapshot_available"])

                status, error, _ = api.request("GET", "/diagnosis")
                self.assertEqual(404, status)
                self.assertEqual("no_diagnosis_snapshot", error["error"]["code"])
            finally:
                api.close()

    def test_serves_validated_diagnosis_with_status_summary_and_etag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "diagnosis.json"
            expected = self.build_snapshot(7)
            self.write_snapshot(snapshot_path, expected)
            api = ApiHarness(snapshot_path)
            try:
                status, diagnosis, headers = api.request("GET", "/diagnosis")
                self.assertEqual(200, status)
                self.assertEqual(expected, diagnosis)
                self.assertIn("etag", headers)
                self.assertEqual("no-cache", headers["cache-control"])

                status, summary, _ = api.request("GET", "/status")
                self.assertEqual(200, status)
                self.assertEqual("ready", summary["state"])
                self.assertEqual("incident.network.retransmission_spike", summary["incident_id"])
                self.assertEqual(7, summary["evidence_revision"])
                self.assertEqual(1, summary["partitions"])
                self.assertGreaterEqual(summary["diagnoses"], 1)
                self.assertEqual(2, summary["active_observations"])
            finally:
                api.close()

    def test_etag_supports_conditional_get_and_changes_after_atomic_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "diagnosis.json"
            self.write_snapshot(snapshot_path, self.build_snapshot(1))
            api = ApiHarness(snapshot_path)
            try:
                _, first, headers = api.request("GET", "/diagnosis")
                first_etag = headers["etag"]
                self.assertEqual(1, first["evidence_revision"])

                status, payload, conditional_headers = api.request(
                    "GET",
                    "/diagnosis",
                    headers={"If-None-Match": first_etag},
                )
                self.assertEqual(304, status)
                self.assertIsNone(payload)
                self.assertEqual(first_etag, conditional_headers["etag"])

                self.write_snapshot(snapshot_path, self.build_snapshot(2))
                status, second, second_headers = api.request(
                    "GET",
                    "/diagnosis",
                    headers={"If-None-Match": first_etag},
                )
                self.assertEqual(200, status)
                self.assertEqual(2, second["evidence_revision"])
                self.assertNotEqual(first_etag, second_headers["etag"])
            finally:
                api.close()

    def test_invalid_snapshot_is_visible_in_status_and_not_served(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "diagnosis.json"
            snapshot_path.write_text('{"kind":"diagnosis_snapshot"}\n', encoding="utf-8")
            api = ApiHarness(snapshot_path)
            try:
                status, summary, _ = api.request("GET", "/status")
                self.assertEqual(200, status)
                self.assertEqual("degraded", summary["status"])
                self.assertEqual("invalid_snapshot", summary["state"])
                self.assertIn("schema validation failed", summary["error"])

                status, error, _ = api.request("GET", "/diagnosis")
                self.assertEqual(503, status)
                self.assertEqual("invalid_diagnosis_snapshot", error["error"]["code"])
            finally:
                api.close()

    def test_head_and_method_boundary_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "diagnosis.json"
            self.write_snapshot(snapshot_path, self.build_snapshot(1))
            api = ApiHarness(snapshot_path)
            try:
                status, payload, headers = api.request("HEAD", "/diagnosis")
                self.assertEqual(200, status)
                self.assertIsNone(payload)
                self.assertIn("etag", headers)

                status, payload, headers = api.request("POST", "/diagnosis", body=b"{}")
                self.assertEqual(405, status)
                self.assertIsNone(payload)
                self.assertEqual("GET, HEAD", headers["allow"])
            finally:
                api.close()

    def test_unknown_path_returns_structured_404(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = ApiHarness(Path(directory) / "diagnosis.json")
            try:
                status, error, _ = api.request("GET", "/unknown")
                self.assertEqual(404, status)
                self.assertEqual("not_found", error["error"]["code"])
            finally:
                api.close()


if __name__ == "__main__":
    unittest.main()
