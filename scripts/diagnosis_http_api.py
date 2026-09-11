#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from causal_projection import ROOT
from probe_ranking import validate_probe_ranking
from runtime_evidence import format_timestamp

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4320
SCHEMA_PATH = ROOT / "schema" / "diagnosis-snapshot.schema.json"


class SnapshotUnavailable(FileNotFoundError):
    pass


class InvalidSnapshot(ValueError):
    pass


class DiagnosisSnapshotReader:
    def __init__(self, snapshot_path: Path, *, schema_path: Path = SCHEMA_PATH) -> None:
        self.snapshot_path = snapshot_path
        with schema_path.open("r", encoding="utf-8") as handle:
            schema = json.load(handle)
        Draft202012Validator.check_schema(schema)
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self._signature: tuple[int, int, int] | None = None
        self._snapshot: dict[str, Any] | None = None
        self._etag: str | None = None
        self._loaded_at: str | None = None
        self._lock = RLock()

    @staticmethod
    def _validate_probe_rankings(document: dict[str, Any]) -> None:
        for partition in document.get("partitions", []):
            for diagnosis in partition.get("diagnoses", []):
                try:
                    validate_probe_ranking(diagnosis["probe_ranking"])
                except (OSError, ValueError) as exc:
                    raise InvalidSnapshot(
                        "embedded probe ranking validation failed: " + str(exc)
                    ) from exc

    def _load(self) -> tuple[dict[str, Any], str]:
        try:
            with self.snapshot_path.open("rb") as handle:
                raw = handle.read()
                stat_result = os.fstat(handle.fileno())
        except FileNotFoundError as exc:
            raise SnapshotUnavailable("diagnosis snapshot is not available yet") from exc
        except OSError as exc:
            raise InvalidSnapshot(f"cannot read diagnosis snapshot: {exc}") from exc

        signature = (stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns)
        if signature == self._signature and self._snapshot is not None and self._etag is not None:
            return copy.deepcopy(self._snapshot), self._etag

        try:
            decoded = raw.decode("utf-8")
            document = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidSnapshot("diagnosis snapshot must be valid UTF-8 JSON") from exc
        if not isinstance(document, dict):
            raise InvalidSnapshot("diagnosis snapshot must be a JSON object")

        errors = sorted(self._validator.iter_errors(document), key=lambda error: list(error.path))
        if errors:
            details = "; ".join(error.message for error in errors[:5])
            if len(errors) > 5:
                details += f"; and {len(errors) - 5} more validation errors"
            raise InvalidSnapshot("diagnosis snapshot schema validation failed: " + details)
        self._validate_probe_rankings(document)

        digest = hashlib.sha256(raw).hexdigest()
        etag = f'"{digest}"'
        self._signature = signature
        self._snapshot = copy.deepcopy(document)
        self._etag = etag
        self._loaded_at = format_timestamp(datetime.now(timezone.utc))
        return copy.deepcopy(document), etag

    def read(self) -> tuple[dict[str, Any], str]:
        with self._lock:
            return self._load()

    @staticmethod
    def _counts(document: dict[str, Any]) -> dict[str, int]:
        partitions = document.get("partitions", [])
        diagnoses = 0
        unranked = 0
        active_observations = 0
        next_probe_recommendations = 0
        for partition in partitions:
            if not isinstance(partition, dict):
                continue
            partition_diagnoses = partition.get("diagnoses", [])
            diagnoses += len(partition_diagnoses)
            unranked += len(partition.get("unranked_observations", []))
            active_observations += len(partition.get("observed", []))
            for diagnosis in partition_diagnoses:
                if not isinstance(diagnosis, dict):
                    continue
                probe_ranking = diagnosis.get("probe_ranking")
                if isinstance(probe_ranking, dict) and probe_ranking.get("found") is True:
                    next_probe_recommendations += 1
        return {
            "partitions": len(partitions),
            "diagnoses": diagnoses,
            "unranked_observations": unranked,
            "active_observations": active_observations,
            "next_probe_recommendations": next_probe_recommendations,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            try:
                document, etag = self._load()
            except SnapshotUnavailable:
                return {
                    "status": "ok",
                    "state": "waiting_for_snapshot",
                    "snapshot_available": False,
                    "snapshot_path": str(self.snapshot_path),
                }
            except InvalidSnapshot as exc:
                return {
                    "status": "degraded",
                    "state": "invalid_snapshot",
                    "snapshot_available": True,
                    "snapshot_path": str(self.snapshot_path),
                    "error": str(exc),
                }

            return {
                "status": "ok",
                "state": "ready",
                "snapshot_available": True,
                "snapshot_path": str(self.snapshot_path),
                "etag": etag,
                "loaded_at": self._loaded_at,
                "incident_id": document["incident_id"],
                "evidence_revision": document["evidence_revision"],
                "generated_at": document["generated_at"],
                "as_of": document["as_of"],
                "next_recompute_at": document.get("next_recompute_at"),
                **self._counts(document),
            }


def _etag_matches(header_value: str | None, etag: str) -> bool:
    if not header_value:
        return False
    candidates = [value.strip() for value in header_value.split(",")]
    return "*" in candidates or etag in candidates


def make_handler(reader: DiagnosisSnapshotReader, *, verbose: bool = False) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AtlerrorDiagnosisHTTP/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            if verbose:
                super().log_message(format, *args)

        def _send_json(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            headers: dict[str, str] | None = None,
            include_body: bool = True,
        ) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body) if include_body else 0))
            if headers:
                for key, value in headers.items():
                    self.send_header(key, value)
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _error(self, status: int, code: str, message: str, *, include_body: bool = True) -> None:
            self._send_json(
                status,
                {"error": {"code": code, "message": message}},
                include_body=include_body,
            )

        def _serve_diagnosis(self, *, include_body: bool) -> None:
            try:
                document, etag = reader.read()
            except SnapshotUnavailable as exc:
                self._error(404, "no_diagnosis_snapshot", str(exc), include_body=include_body)
                return
            except InvalidSnapshot as exc:
                self._error(503, "invalid_diagnosis_snapshot", str(exc), include_body=include_body)
                return

            headers = {
                "ETag": etag,
                "Cache-Control": "no-cache",
            }
            if _etag_matches(self.headers.get("If-None-Match"), etag):
                self.send_response(304)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(200, document, headers=headers, include_body=include_body)

        def _serve_get(self, *, include_body: bool) -> None:
            path = urlsplit(self.path).path
            if path == "/health":
                self._send_json(200, {"status": "ok"}, include_body=include_body)
                return
            if path == "/status":
                self._send_json(200, reader.status(), include_body=include_body)
                return
            if path == "/diagnosis":
                self._serve_diagnosis(include_body=include_body)
                return
            self._error(404, "not_found", "unknown diagnosis endpoint", include_body=include_body)

        def do_GET(self) -> None:
            self._serve_get(include_body=True)

        def do_HEAD(self) -> None:
            self._serve_get(include_body=False)

        def _method_not_allowed(self) -> None:
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = _method_not_allowed
        do_PUT = _method_not_allowed
        do_PATCH = _method_not_allowed
        do_DELETE = _method_not_allowed

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve a validated Atlerror diagnosis snapshot through a small read-only HTTP API."
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        required=True,
        help="Diagnosis snapshot JSON file produced by live_diagnosis_watch.py",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Listen host, default {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Listen port, default {DEFAULT_PORT}")
    parser.add_argument("--verbose", action="store_true", help="Enable HTTP access logs")
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    try:
        reader = DiagnosisSnapshotReader(
            args.snapshot,
            schema_path=root / "schema" / "diagnosis-snapshot.schema.json",
        )
        server = ThreadingHTTPServer((args.host, args.port), make_handler(reader, verbose=args.verbose))
        server.daemon_threads = True
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    host, port = server.server_address[:2]
    print(
        f"Atlerror diagnosis API listening on http://{host}:{port}/diagnosis "
        f"from snapshot {args.snapshot}",
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
