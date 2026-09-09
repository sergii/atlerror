#!/usr/bin/env python3

from __future__ import annotations

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
LOSS_PERCENT = float(os.environ.get("LOSS_PERCENT", "5"))
PAYLOAD_BYTES = int(os.environ.get("PAYLOAD_BYTES", str(1024 * 1024)))
CHUNK_BYTES = int(os.environ.get("CHUNK_BYTES", "65536"))
SOCKET_TIMEOUT_SECONDS = float(os.environ.get("SOCKET_TIMEOUT_SECONDS", "20"))

TARGET_IP = socket.gethostbyname(TARGET_HOST)
TARGET = (TARGET_IP, TARGET_PORT)
PAYLOAD_CHUNK = b"x" * CHUNK_BYTES


def tc(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tc", *args],
        text=True,
        capture_output=True,
        check=check,
    )


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


def connect() -> tuple[socket.socket, float]:
    started = time.monotonic()
    sock = socket.create_connection(TARGET, timeout=SOCKET_TIMEOUT_SECONDS)
    sock.settimeout(SOCKET_TIMEOUT_SECONDS)
    elapsed_ms = (time.monotonic() - started) * 1000
    return sock, round(elapsed_ms, 3)


def transfer_on_socket(sock: socket.socket, phase: str) -> dict[str, object]:
    retrans_before = tcp_retrans_segs()
    started = time.monotonic()
    sock.sendall(struct.pack("!Q", PAYLOAD_BYTES))
    remaining = PAYLOAD_BYTES
    while remaining:
        chunk = PAYLOAD_CHUNK if remaining >= CHUNK_BYTES else PAYLOAD_CHUNK[:remaining]
        sock.sendall(chunk)
        remaining -= len(chunk)
    response = recv_exact(sock, 8)
    elapsed_ms = (time.monotonic() - started) * 1000
    retrans_after = tcp_retrans_segs()
    acknowledged_bytes = struct.unpack("!Q", response)[0]
    return {
        "phase": phase,
        "payload_bytes": PAYLOAD_BYTES,
        "acknowledged_bytes": acknowledged_bytes,
        "completed": acknowledged_bytes == PAYLOAD_BYTES,
        "elapsed_ms": round(elapsed_ms, 3),
        "tcp_retrans_before": retrans_before,
        "tcp_retrans_after": retrans_after,
        "tcp_retrans_delta": retrans_after - retrans_before,
    }


def transfer_new_connection(phase: str) -> dict[str, object]:
    sock, connect_ms = connect()
    try:
        result = transfer_on_socket(sock, phase)
        result["connect_ms"] = connect_ms
        return result
    finally:
        sock.close()


def qdisc_drop_count(stats: str) -> int | None:
    match = re.search(r"dropped\s+(\d+)", stats)
    return int(match.group(1)) if match else None


baseline = transfer_new_connection("baseline")

intervention_socket, intervention_connect_ms = connect()
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
        "loss",
        f"{LOSS_PERCENT}%",
    )
    intervention = transfer_on_socket(intervention_socket, "intervention")
    intervention["connect_ms"] = intervention_connect_ms
    intervention["connection_established_before_loss"] = True
    qdisc_stats = tc("-s", "qdisc", "show", "dev", NETWORK_INTERFACE).stdout.strip()
finally:
    tc("qdisc", "del", "dev", NETWORK_INTERFACE, "root", check=False)
    intervention_socket.close()

if intervention is None:
    raise RuntimeError("intervention transfer did not produce evidence")

time.sleep(0.05)
recovery = transfer_new_connection("recovery")
qdisc_after_recovery = tc("qdisc", "show", "dev", NETWORK_INTERFACE, check=False).stdout.strip()
reported_drops = qdisc_drop_count(qdisc_stats)

assertions = {
    "baseline_transfer_completes": bool(baseline["completed"]),
    "intervention_connection_established_before_loss": bool(
        intervention["connection_established_before_loss"]
    ),
    "intervention_transfer_still_completes": bool(intervention["completed"]),
    "configured_loss_is_partial": 0.0 < LOSS_PERCENT < 100.0,
    "netem_qdisc_active": "netem" in qdisc_stats and "loss" in qdisc_stats,
    "netem_reports_dropped_packets": reported_drops is not None and reported_drops > 0,
    "tcp_retransmissions_increase": int(intervention["tcp_retrans_delta"]) > 0,
    "transfer_completion_time_increases": float(intervention["elapsed_ms"]) > float(baseline["elapsed_ms"]),
    "recovery_transfer_completes": bool(recovery["completed"]),
    "netem_removed_before_recovery": "netem" not in qdisc_after_recovery,
}

result = "supports" if all(assertions.values()) else "contradicts"

evidence = {
    "schema_version": "0.1",
    "kind": "empirical_evidence",
    "experiment": "experiment.network.packet_loss.tcp_retransmissions_python_linux",
    "claims": ["claim.network.packet_loss.partial_loss_causes_tcp_retransmissions"],
    "environment": {
        "runtime": "python",
        "runtime_version": platform.python_version(),
        "platform": platform.platform(),
        "isolation": "docker_compose_linux_netem",
    },
    "intervention": {
        "action": "apply_partial_client_egress_packet_loss_after_tcp_connect",
        "interface": NETWORK_INTERFACE,
        "requested_loss_percent": LOSS_PERCENT,
        "payload_bytes": PAYLOAD_BYTES,
        "chunk_bytes": CHUNK_BYTES,
        "socket_timeout_seconds": SOCKET_TIMEOUT_SECONDS,
    },
    "observations": {
        "target_host": TARGET_HOST,
        "target_ip": TARGET_IP,
        "target_port": TARGET_PORT,
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
        "qdisc_during_intervention": qdisc_stats,
        "qdisc_reported_drop_count": reported_drops,
        "qdisc_after_recovery": qdisc_after_recovery,
        "elapsed_ms_delta": round(
            float(intervention["elapsed_ms"]) - float(baseline["elapsed_ms"]), 3
        ),
        "tcp_retrans_delta_over_baseline": int(intervention["tcp_retrans_delta"])
        - int(baseline["tcp_retrans_delta"]),
    },
    "assertions": assertions,
    "result": result,
    "interpretation": (
        "Controlled partial Linux egress loss was applied only after TCP establishment. The reliable transfer still completed, "
        "while the qdisc recorded packet drops, the client TCP retransmission counter increased, and transfer completion took longer than baseline."
    ),
    "limitations": [
        "Partial netem loss is probabilistic, so exact drop and retransmission counts can vary across runs.",
        "The experiment validates TCP recovery behavior inside Linux/Docker rather than locating a physical production loss point.",
        "Retransmissions support a missing-or-delayed-delivery diagnosis but require corroborating evidence to attribute the physical root cause.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
