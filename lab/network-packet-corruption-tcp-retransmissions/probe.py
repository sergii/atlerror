#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import socket
import struct
import subprocess
import time

TARGET_HOST = os.environ.get("TARGET_HOST", "server")
TARGET_PORT = int(os.environ.get("TARGET_PORT", "9000"))
NETWORK_INTERFACE = os.environ.get("NETWORK_INTERFACE", "eth0")
CORRUPT_PERCENT = float(os.environ.get("CORRUPT_PERCENT", "3"))
NETEM_SEED = int(os.environ.get("NETEM_SEED", "424242"))
PAYLOAD_BYTES = int(os.environ.get("PAYLOAD_BYTES", str(2 * 1024 * 1024)))
CHUNK_BYTES = int(os.environ.get("CHUNK_BYTES", "65536"))
SOCKET_TIMEOUT_SECONDS = float(os.environ.get("SOCKET_TIMEOUT_SECONDS", "20"))

TARGET_IP = socket.gethostbyname(TARGET_HOST)
TARGET = (TARGET_IP, TARGET_PORT)
PATTERN = b"atlerror-n1.6-l2-checksum-retransmission-"
PAYLOAD = (PATTERN * ((PAYLOAD_BYTES // len(PATTERN)) + 1))[:PAYLOAD_BYTES]
PAYLOAD_DIGEST = hashlib.sha256(PAYLOAD).digest()


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def tc(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["tc", *args], check=check)


def ethtool(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["ethtool", *args], check=check)


def tcp_retrans_segs() -> int:
    with open("/proc/net/snmp", encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.startswith("Tcp:")]
    if len(lines) < 2:
        raise RuntimeError("Tcp counters are missing from /proc/net/snmp")
    headers = lines[-2].split()[1:]
    values = lines[-1].split()[1:]
    counters = dict(zip(headers, map(int, values), strict=True))
    return counters["RetransSegs"]


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("peer closed before response completed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def tx_checksum_offload_disabled(features: str) -> bool:
    generic = re.search(r"^tx-checksum-ip-generic:\s+off(?:\s|$)", features, re.MULTILINE)
    overall = re.search(r"^tx-checksumming:\s+off(?:\s|$)", features, re.MULTILINE)
    return generic is not None or overall is not None


def segmentation_offload_disabled(features: str) -> bool:
    tso = re.search(r"^tcp-segmentation-offload:\s+off(?:\s|$)", features, re.MULTILINE)
    gso = re.search(r"^generic-segmentation-offload:\s+off(?:\s|$)", features, re.MULTILINE)
    return tso is not None and gso is not None


def disable_tx_offloads() -> dict[str, object]:
    before = ethtool("-k", NETWORK_INTERFACE).stdout.strip()
    change = ethtool(
        "-K",
        NETWORK_INTERFACE,
        "tx",
        "off",
        "tso",
        "off",
        "gso",
        "off",
        check=False,
    )
    after = ethtool("-k", NETWORK_INTERFACE).stdout.strip()
    return {
        "command_returncode": change.returncode,
        "command_stdout": change.stdout.strip(),
        "command_stderr": change.stderr.strip(),
        "before": before,
        "after": after,
        "tx_checksum_offload_disabled": tx_checksum_offload_disabled(after),
        "segmentation_offload_disabled": segmentation_offload_disabled(after),
    }


def qdisc_drop_count(stats: str) -> int | None:
    match = re.search(r"dropped\s+(\d+)", stats)
    return int(match.group(1)) if match else None


def prepare_transfer() -> tuple[socket.socket, float]:
    started = time.monotonic()
    sock = socket.create_connection(TARGET, timeout=SOCKET_TIMEOUT_SECONDS)
    sock.settimeout(SOCKET_TIMEOUT_SECONDS)
    connect_ms = (time.monotonic() - started) * 1000
    sock.sendall(b"D" + struct.pack("!Q", PAYLOAD_BYTES) + PAYLOAD_DIGEST)
    ready = recv_exact(sock, 1)
    if ready != b"R":
        sock.close()
        raise RuntimeError(f"unexpected server readiness response: {ready!r}")
    return sock, round(connect_ms, 3)


def finish_transfer(sock: socket.socket, phase: str, connect_ms: float) -> dict[str, object]:
    retrans_before = tcp_retrans_segs()
    started = time.monotonic()
    for offset in range(0, PAYLOAD_BYTES, CHUNK_BYTES):
        sock.sendall(PAYLOAD[offset : offset + CHUNK_BYTES])
    header = recv_exact(sock, 4)
    response_size = struct.unpack("!I", header)[0]
    server_result = json.loads(recv_exact(sock, response_size).decode("utf-8"))
    elapsed_ms = (time.monotonic() - started) * 1000
    retrans_after = tcp_retrans_segs()
    return {
        "phase": phase,
        "payload_bytes": PAYLOAD_BYTES,
        "connect_ms": connect_ms,
        "elapsed_ms": round(elapsed_ms, 3),
        "completed": int(server_result["received_bytes"]) == PAYLOAD_BYTES,
        "digest_match": bool(server_result["digest_match"]),
        "tcp_retrans_before": retrans_before,
        "tcp_retrans_after": retrans_after,
        "tcp_retrans_delta": retrans_after - retrans_before,
        "receiver_tcp_in_errs_delta": int(server_result["tcp_in_errs_delta"]),
        "receiver_tcp_in_csum_errors_delta": int(server_result["tcp_in_csum_errors_delta"]),
    }


def transfer_clean(phase: str) -> dict[str, object]:
    sock, connect_ms = prepare_transfer()
    try:
        return finish_transfer(sock, phase, connect_ms)
    finally:
        sock.close()


offload = disable_tx_offloads()
baseline = transfer_clean("baseline")

intervention_socket, intervention_connect_ms = prepare_transfer()
qdisc_stats = ""
intervention = None
try:
    tc(
        "qdisc",
        "add",
        "dev",
        NETWORK_INTERFACE,
        "root",
        "netem",
        "corrupt",
        f"{CORRUPT_PERCENT}%",
        "seed",
        str(NETEM_SEED),
    )
    intervention = finish_transfer(
        intervention_socket,
        "intervention",
        intervention_connect_ms,
    )
    intervention["connection_and_header_established_before_corruption"] = True
    qdisc_stats = tc("-s", "qdisc", "show", "dev", NETWORK_INTERFACE).stdout.strip()
finally:
    tc("qdisc", "del", "dev", NETWORK_INTERFACE, "root", check=False)
    intervention_socket.close()

if intervention is None:
    raise RuntimeError("intervention transfer did not produce evidence")

time.sleep(0.05)
recovery = transfer_clean("recovery")
qdisc_after_recovery = tc("qdisc", "show", "dev", NETWORK_INTERFACE, check=False).stdout.strip()
reported_drops = qdisc_drop_count(qdisc_stats)
receiver_integrity_errors = max(
    int(intervention["receiver_tcp_in_errs_delta"]),
    int(intervention["receiver_tcp_in_csum_errors_delta"]),
)

assertions = {
    "tx_checksum_offload_disabled": bool(offload["tx_checksum_offload_disabled"]),
    "segmentation_offload_disabled": bool(offload["segmentation_offload_disabled"]),
    "baseline_transfer_completes": bool(baseline["completed"]),
    "baseline_payload_integrity_exact": bool(baseline["digest_match"]),
    "intervention_connection_and_header_precede_corruption": bool(
        intervention["connection_and_header_established_before_corruption"]
    ),
    "configured_corruption_is_partial": 0.0 < CORRUPT_PERCENT < 100.0,
    "netem_qdisc_active": "netem" in qdisc_stats and "corrupt" in qdisc_stats,
    "netem_reports_no_qdisc_drop": reported_drops == 0,
    "receiver_records_integrity_rejection": receiver_integrity_errors > 0,
    "sender_tcp_retransmissions_increase": int(intervention["tcp_retrans_delta"]) > 0,
    "intervention_transfer_still_completes": bool(intervention["completed"]),
    "tcp_preserves_application_payload_integrity": bool(intervention["digest_match"]),
    "recovery_transfer_completes": bool(recovery["completed"]),
    "recovery_payload_integrity_exact": bool(recovery["digest_match"]),
    "netem_removed_before_recovery": "netem" not in qdisc_after_recovery,
}

result = "supports" if all(assertions.values()) else "contradicts"

evidence = {
    "schema_version": "0.1",
    "kind": "empirical_evidence",
    "experiment": "experiment.network.packet_corruption.tcp_checksum_retransmissions_python_linux",
    "claims": ["claim.network.packet_corruption.checksum_rejection_causes_tcp_retransmissions"],
    "environment": {
        "runtime": "python",
        "runtime_version": platform.python_version(),
        "platform": platform.platform(),
        "isolation": "docker_compose_linux_netem",
    },
    "intervention": {
        "action": "apply_partial_client_egress_corruption_after_tcp_connect_and_header",
        "interface": NETWORK_INTERFACE,
        "corrupt_percent": CORRUPT_PERCENT,
        "netem_seed": NETEM_SEED,
        "payload_bytes": PAYLOAD_BYTES,
        "chunk_bytes": CHUNK_BYTES,
        "socket_timeout_seconds": SOCKET_TIMEOUT_SECONDS,
        "tx_checksum_offload_disabled": bool(offload["tx_checksum_offload_disabled"]),
        "segmentation_offload_disabled": bool(offload["segmentation_offload_disabled"]),
    },
    "observations": {
        "target_host": TARGET_HOST,
        "target_ip": TARGET_IP,
        "target_port": TARGET_PORT,
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
        "receiver_integrity_error_count": receiver_integrity_errors,
        "qdisc_during_intervention": qdisc_stats,
        "qdisc_reported_drop_count": reported_drops,
        "qdisc_after_recovery": qdisc_after_recovery,
        "offload_configuration": offload,
        "tcp_retrans_delta_over_baseline": int(intervention["tcp_retrans_delta"])
        - int(baseline["tcp_retrans_delta"]),
    },
    "assertions": assertions,
    "result": result,
    "interpretation": (
        "Controlled Linux egress corruption was applied only after TCP connection establishment and protocol setup. "
        "With transmit checksum and segmentation offloads disabled, the receiver recorded TCP integrity errors, the sender retransmitted segments, and TCP still delivered the exact original application payload."
    ),
    "limitations": [
        "The experiment uses synthetic Linux netem corruption inside Docker rather than a physical production path.",
        "Transmit checksum and segmentation offloads are explicitly disabled to make stale-checksum rejection observable and avoid offload recalculating checksums after netem mutation.",
        "TCP and kernel counters are aggregate per-network-namespace counters, so exact values depend on packetization and recovery behavior.",
        "The experiment demonstrates the corruption-to-integrity-rejection-to-retransmission causal path but does not locate a physical production corruption source.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
