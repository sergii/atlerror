#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.request import urlopen

from causal_projection import ROOT, load_concepts, load_edges
from diagnosis_http_api import DiagnosisSnapshotReader, make_handler
from diagnosis_mcp_server import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    CURRENT_DIAGNOSIS_URI,
    DIAGNOSIS_STATUS_URI,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
    DiagnosisMcpServer,
)
from live_diagnosis import build_diagnosis_snapshot
from opentelemetry_trace_adapter import (
    build_runtime_evidence as build_trace_runtime_evidence,
)
from opentelemetry_trace_adapter import load_adapter as load_trace_adapter
from opentelemetry_trace_adapter import load_payload as load_trace_payload
from prometheus_adapter import (
    build_runtime_evidence as build_prometheus_runtime_evidence,
)
from prometheus_adapter import load_adapter as load_prometheus_adapter
from prometheus_adapter import load_response_file
from runtime_evidence import format_timestamp, parse_timestamp
from runtime_evidence_composition import compose_runtime_evidence

DEFAULT_INCIDENT_ID = "incident.demo.checkout.stripe"
DEFAULT_AS_OF = datetime(2026, 9, 11, 16, 31, tzinfo=timezone.utc)
DEFAULT_OUTPUT_DIR = Path("/tmp/atlerror-demo")

PROMETHEUS_ADAPTER = Path("examples/adapters/prometheus/external-dependency.yaml")
PROMETHEUS_RESPONSE = Path(
    "examples/telemetry/prometheus/external-dependency-tcp-retransmissions.json"
)
OTEL_ADAPTER = Path("examples/adapters/opentelemetry/external-dependency.yaml")
OTEL_PAYLOAD = Path("examples/telemetry/opentelemetry/external-dependency-trace.json")


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _modern_meta() -> dict[str, Any]:
    return {
        PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META_KEY: {},
        CLIENT_INFO_META_KEY: {"name": "atlerror-demo", "version": "0.1.0"},
    }


def _mcp_read(server: DiagnosisMcpServer, uri: str, request_id: int) -> dict[str, Any]:
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "resources/read",
            "params": {"uri": uri, "_meta": _modern_meta()},
        }
    )
    if not isinstance(response, dict) or "error" in response:
        raise ValueError(f"MCP demo verification failed for {uri}: {response}")
    contents = response.get("result", {}).get("contents", [])
    if len(contents) != 1 or not isinstance(contents[0].get("text"), str):
        raise ValueError(f"MCP demo verification returned invalid resource content for {uri}")
    document = json.loads(contents[0]["text"])
    if not isinstance(document, dict):
        raise ValueError(f"MCP demo resource is not a JSON object: {uri}")
    return document


def verify_mcp(snapshot_path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    reader = DiagnosisSnapshotReader(snapshot_path)
    server = DiagnosisMcpServer(reader)
    current = _mcp_read(server, CURRENT_DIAGNOSIS_URI, 1)
    status = _mcp_read(server, DIAGNOSIS_STATUS_URI, 2)
    if current != expected:
        raise ValueError("MCP current diagnosis does not match persisted diagnosis snapshot")
    if status.get("state") != "ready":
        raise ValueError("MCP diagnosis status is not ready")
    return status


def verify_http(snapshot_path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    reader = DiagnosisSnapshotReader(snapshot_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(reader))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        with urlopen(f"http://{host}:{port}/diagnosis", timeout=2) as response:
            current = json.load(response)
        with urlopen(f"http://{host}:{port}/status", timeout=2) as response:
            status = json.load(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    if current != expected:
        raise ValueError("HTTP diagnosis does not match persisted diagnosis snapshot")
    if status.get("state") != "ready":
        raise ValueError("HTTP diagnosis status is not ready")
    return status


def summarize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    partitions: list[dict[str, Any]] = []
    for partition in snapshot["partitions"]:
        diagnoses: list[dict[str, Any]] = []
        for diagnosis in partition["diagnoses"]:
            ranking = diagnosis["ranking"]
            probes = diagnosis["probe_ranking"]
            top_candidate = ranking["candidates"][0]
            next_probe = probes["probes"][0]["probe"]["id"] if probes["found"] else None
            diagnoses.append(
                {
                    "target": diagnosis["target"],
                    "top_hypothesis": top_candidate["source"]["id"],
                    "candidate_count": len(ranking["candidates"]),
                    "next_probe": next_probe,
                    "probe_status": "recommended" if probes["found"] else probes["not_found_reason"],
                }
            )
        partitions.append(
            {
                "scope": partition["scope"],
                "observed": partition["observed"],
                "absent": partition["absent"],
                "diagnoses": diagnoses,
                "unranked_observations": partition["unranked_observations"],
            }
        )
    return {
        "schema_version": "0.1",
        "kind": "demo_summary",
        "incident_id": snapshot["incident_id"],
        "as_of": snapshot["as_of"],
        "partitions": partitions,
    }


def build_demo(
    *,
    root: Path = ROOT,
    incident_id: str = DEFAULT_INCIDENT_ID,
    as_of: datetime = DEFAULT_AS_OF,
) -> dict[str, Any]:
    if as_of.utcoffset() is None:
        raise ValueError("demo as_of must include a timezone")

    concepts = load_concepts(root)
    edges = load_edges(root)

    prometheus_adapter = load_prometheus_adapter(root / PROMETHEUS_ADAPTER)
    prometheus_response = load_response_file(root / PROMETHEUS_RESPONSE)
    prometheus_evidence = build_prometheus_runtime_evidence(
        prometheus_adapter,
        {"tcp_retransmissions_rate": prometheus_response},
        concepts,
        incident_id=incident_id,
        source_uri="fixture://prometheus/external-dependency-tcp-retransmissions.json",
    )

    trace_evidence = build_trace_runtime_evidence(
        load_trace_adapter(root / OTEL_ADAPTER),
        load_trace_payload(root / OTEL_PAYLOAD),
        concepts,
        incident_id=incident_id,
        source_uri="fixture://opentelemetry/external-dependency-trace.json",
    )

    composed = compose_runtime_evidence(
        [prometheus_evidence, trace_evidence],
        concepts,
    )
    diagnosis = build_diagnosis_snapshot(
        composed,
        concepts,
        edges,
        as_of=as_of,
        evidence_revision=1,
    )
    summary = summarize_snapshot(diagnosis)
    return {
        "prometheus_evidence": prometheus_evidence,
        "trace_evidence": trace_evidence,
        "runtime_evidence": composed,
        "diagnosis": diagnosis,
        "summary": summary,
    }


def run_demo(
    output_dir: Path,
    *,
    root: Path = ROOT,
    incident_id: str = DEFAULT_INCIDENT_ID,
    as_of: datetime = DEFAULT_AS_OF,
    verify_transports: bool = True,
) -> dict[str, Any]:
    result = build_demo(root=root, incident_id=incident_id, as_of=as_of)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = {
        "prometheus_evidence": output_dir / "prometheus-evidence.json",
        "trace_evidence": output_dir / "opentelemetry-evidence.json",
        "runtime_evidence": output_dir / "runtime-evidence.json",
        "diagnosis": output_dir / "diagnosis.json",
        "summary": output_dir / "demo-summary.json",
    }
    for key, path in paths.items():
        _write_json(path, result[key])

    persisted, _etag = DiagnosisSnapshotReader(paths["diagnosis"]).read()
    if persisted != result["diagnosis"]:
        raise ValueError("persisted diagnosis snapshot does not match generated diagnosis")

    transport_status: dict[str, Any] = {
        "http_verified": False,
        "mcp_verified": False,
    }
    if verify_transports:
        http_status = verify_http(paths["diagnosis"], result["diagnosis"])
        mcp_status = verify_mcp(paths["diagnosis"], result["diagnosis"])
        transport_status = {
            "http_verified": True,
            "mcp_verified": True,
            "http_state": http_status["state"],
            "mcp_state": mcp_status["state"],
        }

    result["transport_status"] = transport_status
    result["paths"] = {key: str(path) for key, path in paths.items()}
    return result


def _print_summary(result: dict[str, Any]) -> None:
    summary = result["summary"]
    print(f"Atlerror demo incident: {summary['incident_id']}")
    print(f"As of: {summary['as_of']}")
    for partition in summary["partitions"]:
        scope = partition["scope"] or {}
        boundaries = ", ".join(scope.get("boundaries", [])) or "unscoped"
        attributes = ", ".join(
            f"{key}={value}" for key, value in scope.get("attributes", {}).items()
        )
        scope_text = boundaries + (f" ({attributes})" if attributes else "")
        print(f"Scope: {scope_text}")
        print("Observed: " + ", ".join(partition["observed"]))
        for diagnosis in partition["diagnoses"]:
            line = f"- {diagnosis['target']} -> {diagnosis['top_hypothesis']}"
            if diagnosis["next_probe"] is not None:
                line += f"; next probe: {diagnosis['next_probe']}"
            print(line)
    transports = result["transport_status"]
    if transports["http_verified"] and transports["mcp_verified"]:
        print("Read-only transports verified: HTTP, MCP")
    print(f"Artifacts: {result['paths']['diagnosis']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the deterministic Atlerror checkout-to-Stripe demo from telemetry fixtures "
            "through adapters, multi-source evidence, diagnosis, next-probe ranking, and "
            "read-only HTTP/MCP verification."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for demo artifacts, default {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument("--incident-id", default=DEFAULT_INCIDENT_ID)
    parser.add_argument(
        "--as-of",
        default=format_timestamp(DEFAULT_AS_OF),
        help="Timezone-aware ISO 8601 time used for evidence freshness",
    )
    parser.add_argument(
        "--skip-transport-checks",
        action="store_true",
        help="Skip ephemeral HTTP and MCP read-back verification",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the compact demo summary as JSON instead of human-readable text",
    )
    return parser


def main(root: Path = ROOT) -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        as_of = parse_timestamp(args.as_of, "--as-of")
        result = run_demo(
            args.output_dir,
            root=root,
            incident_id=args.incident_id,
            as_of=as_of,
            verify_transports=not args.skip_transport_checks,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    if args.json:
        payload = {
            **result["summary"],
            "transport_status": result["transport_status"],
            "paths": result["paths"],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
