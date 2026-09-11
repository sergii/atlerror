#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import gzip
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

from causal_projection import ROOT, load_concepts
from opentelemetry_trace_adapter import (
    iter_spans,
    load_adapter,
    match_span,
    span_to_instance,
    validate_adapter_references,
)
from runtime_evidence import format_timestamp, parse_timestamp, validate_runtime_references

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4318
DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024


class RequestTooLarge(ValueError):
    pass


class UnsupportedMediaType(ValueError):
    pass


def semantic_scope_key(instance: dict[str, Any]) -> str:
    identity = {
        "observation": instance["observation"],
        "scope": instance.get("scope"),
    }
    return json.dumps(identity, sort_keys=True, separators=(",", ":"))


def extract_instances(
    adapter: dict[str, Any],
    payload: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
    *,
    source_uri: str | None = None,
) -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    for item in iter_spans(payload):
        span = item["span"]
        attributes = item["attributes"]
        for mapping in adapter["mappings"]:
            if match_span(mapping, span, attributes):
                instances.append(
                    span_to_instance(
                        adapter,
                        mapping,
                        span,
                        attributes,
                        concepts,
                        source_uri=source_uri,
                    )
                )
    instances.sort(key=lambda instance: instance["id"])
    return instances


class EvidenceStore:
    def __init__(
        self,
        *,
        incident_id: str,
        adapter_id: str,
        concepts: dict[str, dict[str, Any]],
        snapshot_path: Path | None = None,
    ) -> None:
        self.incident_id = incident_id
        self.adapter_id = adapter_id
        self.concepts = concepts
        self.snapshot_path = snapshot_path
        self._instances_by_scope: dict[str, dict[str, Any]] = {}
        self._lock = Lock()
        self._accepted_requests_total = 0
        self._matched_instances_total = 0
        self._inserted_instances_total = 0
        self._replaced_instances_total = 0
        self._duplicate_instances_total = 0
        self._ignored_out_of_order_total = 0
        self._last_ingest_at: str | None = None

    def _document_from(self, instances_by_scope: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "kind": "runtime_evidence",
            "incident_id": self.incident_id,
            "description": (
                "Live OTLP/HTTP trace evidence selected by latest observation per exact scope "
                f"through adapter {self.adapter_id}."
            ),
            "instances": sorted(
                (copy.deepcopy(instance) for instance in instances_by_scope.values()),
                key=lambda instance: instance["id"],
            ),
        }

    def _write_snapshot(self, document: dict[str, Any]) -> None:
        if self.snapshot_path is None:
            return
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_name(self.snapshot_path.name + ".tmp")
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.snapshot_path)

    @staticmethod
    def _is_newer(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
        candidate_time = parse_timestamp(candidate["observed_at"], f"{candidate['id']}.observed_at")
        current_time = parse_timestamp(current["observed_at"], f"{current['id']}.observed_at")
        if candidate_time != current_time:
            return candidate_time > current_time
        return candidate["id"] > current["id"]

    def ingest(self, instances: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            proposed = dict(self._instances_by_scope)
            inserted = 0
            replaced = 0
            duplicates = 0
            ignored_out_of_order = 0

            for instance in instances:
                key = semantic_scope_key(instance)
                current = proposed.get(key)
                if current is None:
                    proposed[key] = instance
                    inserted += 1
                    continue

                if current["id"] == instance["id"]:
                    if current != instance:
                        raise ValueError(
                            "deterministic evidence instance id was replayed with different content: "
                            + instance["id"]
                        )
                    duplicates += 1
                    continue

                if self._is_newer(instance, current):
                    proposed[key] = instance
                    replaced += 1
                else:
                    ignored_out_of_order += 1

            if proposed:
                document = self._document_from(proposed)
                validate_runtime_references(document, self.concepts)
                if inserted or replaced:
                    self._write_snapshot(document)

            self._instances_by_scope = proposed
            self._accepted_requests_total += 1
            self._matched_instances_total += len(instances)
            self._inserted_instances_total += inserted
            self._replaced_instances_total += replaced
            self._duplicate_instances_total += duplicates
            self._ignored_out_of_order_total += ignored_out_of_order
            self._last_ingest_at = format_timestamp(datetime.now(timezone.utc))
            return self.status()

    def evidence(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._instances_by_scope:
                return None
            return self._document_from(self._instances_by_scope)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "incident_id": self.incident_id,
                "adapter_id": self.adapter_id,
                "aggregation_policy": "latest_per_observation_and_exact_scope",
                "stored_instances": len(self._instances_by_scope),
                "accepted_requests_total": self._accepted_requests_total,
                "matched_instances_total": self._matched_instances_total,
                "inserted_instances_total": self._inserted_instances_total,
                "replaced_instances_total": self._replaced_instances_total,
                "duplicate_instances_total": self._duplicate_instances_total,
                "ignored_out_of_order_total": self._ignored_out_of_order_total,
                "last_ingest_at": self._last_ingest_at,
            }


class ReceiverState:
    def __init__(
        self,
        *,
        adapter: dict[str, Any],
        concepts: dict[str, dict[str, Any]],
        store: EvidenceStore,
        max_body_bytes: int,
        source_uri: str | None,
        verbose: bool,
    ) -> None:
        validate_adapter_references(adapter, concepts)
        self.adapter = adapter
        self.concepts = concepts
        self.store = store
        self.max_body_bytes = max_body_bytes
        self.source_uri = source_uri
        self.verbose = verbose

    def translate(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return extract_instances(
            self.adapter,
            payload,
            self.concepts,
            source_uri=self.source_uri,
        )


def make_handler(state: ReceiverState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AtlerrorOTLP/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            if state.verbose:
                super().log_message(format, *args)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, code: str, message: str) -> None:
            self._send_json(status, {"error": {"code": code, "message": message}})

        def _read_json_body(self) -> dict[str, Any]:
            content_type = self.headers.get_content_type()
            if content_type != "application/json":
                raise UnsupportedMediaType(
                    f"OTLP receiver supports application/json, got {content_type}"
                )

            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ValueError("Content-Length is required")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("Content-Length must be an integer") from exc
            if length < 0:
                raise ValueError("Content-Length must not be negative")
            if length > state.max_body_bytes:
                raise RequestTooLarge(
                    f"compressed request body exceeds {state.max_body_bytes} bytes"
                )

            body = self.rfile.read(length)
            encoding = self.headers.get("Content-Encoding", "identity").strip().lower()
            if encoding in ("", "identity"):
                decoded = body
            elif encoding == "gzip":
                try:
                    decoded = gzip.decompress(body)
                except OSError as exc:
                    raise ValueError("invalid gzip request body") from exc
            else:
                raise UnsupportedMediaType(
                    f"unsupported Content-Encoding: {encoding}"
                )

            if len(decoded) > state.max_body_bytes:
                raise RequestTooLarge(
                    f"decoded request body exceeds {state.max_body_bytes} bytes"
                )
            try:
                payload = json.loads(decoded.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("request body must be valid UTF-8 OTLP JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("OTLP trace payload must be a JSON object")
            return payload

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/v1/traces":
                self._error(404, "not_found", "unknown receiver endpoint")
                return
            try:
                payload = self._read_json_body()
                instances = state.translate(payload)
                state.store.ingest(instances)
            except RequestTooLarge as exc:
                self._error(413, "request_too_large", str(exc))
                return
            except UnsupportedMediaType as exc:
                self._error(415, "unsupported_media_type", str(exc))
                return
            except ValueError as exc:
                self._error(400, "invalid_argument", str(exc))
                return
            except OSError as exc:
                self._error(500, "snapshot_write_failed", str(exc))
                return

            # OTLP/HTTP JSON uses an empty ExportTraceServiceResponse on success.
            self._send_json(200, {})

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/health":
                self._send_json(200, {"status": "ok"})
                return
            if path == "/status":
                self._send_json(200, state.store.status())
                return
            if path == "/evidence":
                evidence = state.store.evidence()
                if evidence is None:
                    self._error(404, "no_evidence", "no mapped trace evidence has been received")
                    return
                self._send_json(200, evidence)
                return
            self._error(404, "not_found", "unknown receiver endpoint")

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Receive OTLP/HTTP JSON traces and expose the latest Atlerror runtime evidence "
            "per semantic observation and exact scope."
        )
    )
    parser.add_argument("adapter", type=Path, help="OpenTelemetry trace adapter mapping YAML")
    parser.add_argument("--incident-id", required=True, help="Incident ID for this receiver process")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Listen host, default {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Listen port, default {DEFAULT_PORT}")
    parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=DEFAULT_MAX_BODY_BYTES,
        help="Maximum compressed and decoded OTLP request body size",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="Optional JSON file atomically updated with the current evidence bundle",
    )
    parser.add_argument(
        "--source-uri",
        help="Optional source URI recorded on generated runtime evidence instances",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable HTTP access logs")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.max_body_bytes <= 0:
        parser.error("--max-body-bytes must be positive")

    try:
        adapter = load_adapter(args.adapter)
        concepts = load_concepts(root)
        store = EvidenceStore(
            incident_id=args.incident_id,
            adapter_id=adapter["id"],
            concepts=concepts,
            snapshot_path=args.snapshot,
        )
        state = ReceiverState(
            adapter=adapter,
            concepts=concepts,
            store=store,
            max_body_bytes=args.max_body_bytes,
            source_uri=args.source_uri,
            verbose=args.verbose,
        )
        server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
        server.daemon_threads = True
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    host, port = server.server_address[:2]
    print(
        f"Atlerror OTLP receiver listening on http://{host}:{port}/v1/traces "
        f"for incident {args.incident_id}",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
