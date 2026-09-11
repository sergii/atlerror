#!/usr/bin/env python3

from __future__ import annotations

import copy
import gzip
import json
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from causal_projection import load_concepts
from opentelemetry_trace_adapter import load_adapter, load_payload
from otlp_http_receiver import EvidenceStore, ReceiverState, make_handler

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "examples" / "adapters" / "opentelemetry" / "external-dependency.yaml"
PAYLOAD_PATH = ROOT / "examples" / "telemetry" / "opentelemetry" / "external-dependency-trace.json"


class ReceiverHarness:
    def __init__(self, *, snapshot_path: Path | None = None, max_body_bytes: int = 1024 * 1024) -> None:
        concepts = load_concepts(ROOT)
        adapter = load_adapter(ADAPTER_PATH)
        store = EvidenceStore(
            incident_id="incident.checkout.live",
            adapter_id=adapter["id"],
            concepts=concepts,
            snapshot_path=snapshot_path,
        )
        state = ReceiverState(
            adapter=adapter,
            concepts=concepts,
            store=store,
            max_body_bytes=max_body_bytes,
            source_uri="http://collector.example/v1/traces",
            verbose=False,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
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
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        request = Request(
            self.base_url + path,
            data=body,
            headers=headers or {},
            method=method,
        )
        try:
            with urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return response.status, payload
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            return exc.code, payload

    def post_payload(
        self,
        payload: dict,
        *,
        compressed: bool = False,
        content_type: str = "application/json",
    ) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": content_type}
        if compressed:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        return self.request("POST", "/v1/traces", body=body, headers=headers)


class OtlpHttpReceiverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.receiver = ReceiverHarness()

    def tearDown(self) -> None:
        self.receiver.close()

    def payload(self) -> dict:
        return load_payload(PAYLOAD_PATH)

    def test_accepts_otlp_json_and_exposes_runtime_evidence(self) -> None:
        status, response = self.receiver.post_payload(self.payload())
        self.assertEqual(200, status)
        self.assertEqual({}, response)

        status, evidence = self.receiver.request("GET", "/evidence")
        self.assertEqual(200, status)
        self.assertEqual("runtime_evidence", evidence["kind"])
        self.assertEqual("incident.checkout.live", evidence["incident_id"])
        self.assertEqual(2, len(evidence["instances"]))
        self.assertEqual(
            {
                "observation.dependency.latency",
                "observation.network.connection_timeout",
            },
            {instance["observation"] for instance in evidence["instances"]},
        )
        for instance in evidence["instances"]:
            self.assertEqual(
                "boundary.application.external_dependency",
                instance["scope"]["boundaries"][0],
            )
            self.assertEqual("stripe", instance["scope"]["attributes"]["dependency"])
            self.assertEqual(
                "http://collector.example/v1/traces",
                instance["source"]["uri"],
            )

    def test_accepts_gzip_and_deduplicates_replayed_trace(self) -> None:
        self.assertEqual(200, self.receiver.post_payload(self.payload(), compressed=True)[0])
        self.assertEqual(200, self.receiver.post_payload(self.payload(), compressed=True)[0])

        _, status = self.receiver.request("GET", "/status")
        self.assertEqual(2, status["stored_instances"])
        self.assertEqual(2, status["accepted_requests_total"])
        self.assertEqual(4, status["matched_instances_total"])
        self.assertEqual(2, status["inserted_instances_total"])
        self.assertEqual(2, status["duplicate_instances_total"])
        self.assertEqual(0, status["replaced_instances_total"])

    def test_latest_state_per_observation_and_exact_scope_wins(self) -> None:
        original = self.payload()
        self.assertEqual(200, self.receiver.post_payload(original)[0])

        newer = copy.deepcopy(original)
        span = newer["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        span["traceId"] = "1123456789abcdef0123456789abcdef"
        span["spanId"] = "1123456789abcdef"
        span["startTimeUnixNano"] = "1789144201000000000"
        span["endTimeUnixNano"] = "1789144201050000000"
        span["status"] = {"code": "STATUS_CODE_UNSET"}
        self.assertEqual(200, self.receiver.post_payload(newer)[0])

        _, evidence = self.receiver.request("GET", "/evidence")
        self.assertEqual(
            {"absent"},
            {instance["state"] for instance in evidence["instances"]},
        )

        self.assertEqual(200, self.receiver.post_payload(original)[0])
        _, after_replay = self.receiver.request("GET", "/evidence")
        self.assertEqual(
            {"absent"},
            {instance["state"] for instance in after_replay["instances"]},
        )

        _, status = self.receiver.request("GET", "/status")
        self.assertEqual(2, status["replaced_instances_total"])
        self.assertEqual(2, status["ignored_out_of_order_total"])
        self.assertEqual(
            "latest_per_observation_and_exact_scope",
            status["aggregation_policy"],
        )

    def test_unmatched_spans_are_accepted_without_creating_evidence(self) -> None:
        payload = self.payload()
        payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["kind"] = "SPAN_KIND_SERVER"

        status, response = self.receiver.post_payload(payload)
        self.assertEqual(200, status)
        self.assertEqual({}, response)

        status, error = self.receiver.request("GET", "/evidence")
        self.assertEqual(404, status)
        self.assertEqual("no_evidence", error["error"]["code"])

        _, receiver_status = self.receiver.request("GET", "/status")
        self.assertEqual(1, receiver_status["accepted_requests_total"])
        self.assertEqual(0, receiver_status["matched_instances_total"])

    def test_invalid_dynamic_semantic_scope_is_rejected(self) -> None:
        payload = self.payload()
        attributes = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        for attribute in attributes:
            if attribute["key"] == "atlerror.boundary":
                attribute["value"]["stringValue"] = "boundary.unknown.path"

        status, error = self.receiver.post_payload(payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_argument", error["error"]["code"])
        self.assertIn("unknown boundary", error["error"]["message"])

    def test_rejects_non_json_otlp_encoding(self) -> None:
        status, error = self.receiver.request(
            "POST",
            "/v1/traces",
            body=b"protobuf",
            headers={"Content-Type": "application/x-protobuf"},
        )
        self.assertEqual(415, status)
        self.assertEqual("unsupported_media_type", error["error"]["code"])

    def test_rejects_body_larger_than_configured_limit(self) -> None:
        receiver = ReceiverHarness(max_body_bytes=64)
        try:
            status, error = receiver.post_payload(self.payload())
            self.assertEqual(413, status)
            self.assertEqual("request_too_large", error["error"]["code"])
        finally:
            receiver.close()

    def test_snapshot_tracks_current_evidence_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "runtime-evidence.json"
            receiver = ReceiverHarness(snapshot_path=snapshot_path)
            try:
                self.assertEqual(200, receiver.post_payload(self.payload())[0])
                self.assertTrue(snapshot_path.exists())
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                _, evidence = receiver.request("GET", "/evidence")
                self.assertEqual(evidence, snapshot)
                self.assertFalse(snapshot_path.with_name(snapshot_path.name + ".tmp").exists())
            finally:
                receiver.close()

    def test_health_endpoint(self) -> None:
        status, payload = self.receiver.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}, payload)


if __name__ == "__main__":
    unittest.main()
