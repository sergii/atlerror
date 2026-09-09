#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import time
from typing import Any

TARGET_HOST = os.getenv("TARGET_HOST", "server")
TARGET_PORT = int(os.getenv("TARGET_PORT", "9000"))
INTERFACE = os.getenv("INTERFACE", "eth0")
NETEM_DELAY_MS = int(os.getenv("NETEM_DELAY_MS", "120"))
SAMPLES = int(os.getenv("SAMPLES", "5"))
SOCKET_TIMEOUT_SECONDS = float(os.getenv("SOCKET_TIMEOUT_SECONDS", "3"))

CLAIM_ID = "claim.network.transport_latency.netem_delay_increases_transport_timing"
EXPERIMENT_ID = "experiment.network.transport_latency.netem_python_linux"


def run_tc(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["tc", *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"tc {' '.join(args)} failed with {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed


def clear_netem() -> None:
    run_tc("qdisc", "del", "dev", INTERFACE, "root", check=False)


def install_netem() -> str:
    run_tc(
        "qdisc",
        "replace",
        "dev",
        INTERFACE,
        "root",
        "netem",
        "delay",
        f"{NETEM_DELAY_MS}ms",
    )
    return run_tc("qdisc", "show", "dev", INTERFACE).stdout.strip()


def measure_once(target_ip: str) -> dict[str, float]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT_SECONDS)

    connect_started = time.perf_counter()
    sock.connect((target_ip, TARGET_PORT))
    connect_ms = (time.perf_counter() - connect_started) * 1000.0

    exchange_started = time.perf_counter()
    sock.sendall(b"ping\n")
    with sock.makefile("rb") as stream:
        response_line = stream.readline()
    exchange_ms = (time.perf_counter() - exchange_started) * 1000.0
    sock.close()

    response = json.loads(response_line.decode("utf-8"))
    if response.get("status") != "pong":
        raise RuntimeError(f"unexpected target response: {response!r}")

    return {
        "connect_ms": connect_ms,
        "exchange_ms": exchange_ms,
        "total_ms": connect_ms + exchange_ms,
        "server_handler_ms": float(response["handler_ms"]),
    }


def summarize(samples: list[dict[str, float]]) -> dict[str, Any]:
    def median(field: str) -> float:
        return round(statistics.median(sample[field] for sample in samples), 3)

    return {
        "sample_count": len(samples),
        "connect_ms_median": median("connect_ms"),
        "exchange_ms_median": median("exchange_ms"),
        "total_ms_median": median("total_ms"),
        "server_handler_ms_median": median("server_handler_ms"),
        "samples": [
            {key: round(value, 3) for key, value in sample.items()}
            for sample in samples
        ],
    }


def measure_phase(target_ip: str) -> dict[str, Any]:
    return summarize([measure_once(target_ip) for _ in range(SAMPLES)])


def delta(after: dict[str, Any], before: dict[str, Any], field: str) -> float:
    return round(float(after[field]) - float(before[field]), 3)


def main() -> None:
    target_ip = socket.gethostbyname(TARGET_HOST)
    minimum_transport_delta_ms = max(60.0, NETEM_DELAY_MS * 0.6)

    clear_netem()
    try:
        baseline = measure_phase(target_ip)
        qdisc = install_netem()
        intervention = measure_phase(target_ip)
    finally:
        clear_netem()

    recovery = measure_phase(target_ip)

    deltas = {
        "intervention_connect_ms": delta(
            intervention, baseline, "connect_ms_median"
        ),
        "intervention_exchange_ms": delta(
            intervention, baseline, "exchange_ms_median"
        ),
        "intervention_total_ms": delta(
            intervention, baseline, "total_ms_median"
        ),
        "intervention_server_handler_ms": delta(
            intervention, baseline, "server_handler_ms_median"
        ),
        "recovery_connect_ms": delta(
            intervention, recovery, "connect_ms_median"
        ),
        "recovery_exchange_ms": delta(
            intervention, recovery, "exchange_ms_median"
        ),
        "recovery_total_ms": delta(
            intervention, recovery, "total_ms_median"
        ),
    }

    assertions = {
        "netem_qdisc_active": "netem" in qdisc,
        "tcp_connect_latency_increases": deltas["intervention_connect_ms"]
        >= minimum_transport_delta_ms,
        "request_exchange_latency_increases": deltas["intervention_exchange_ms"]
        >= minimum_transport_delta_ms,
        "end_to_end_latency_increases": deltas["intervention_total_ms"]
        >= minimum_transport_delta_ms * 2,
        "target_processing_remains_stable": abs(
            deltas["intervention_server_handler_ms"]
        )
        <= 20.0,
        "connect_latency_recovers": deltas["recovery_connect_ms"]
        >= minimum_transport_delta_ms,
        "exchange_latency_recovers": deltas["recovery_exchange_ms"]
        >= minimum_transport_delta_ms,
        "end_to_end_latency_recovers": deltas["recovery_total_ms"]
        >= minimum_transport_delta_ms * 2,
    }

    supported = all(assertions.values())
    evidence = {
        "schema_version": "0.1",
        "kind": "empirical_evidence",
        "experiment": EXPERIMENT_ID,
        "claims": [CLAIM_ID],
        "environment": {
            "runtime": "python",
            "runtime_version": platform.python_version(),
            "platform": platform.platform(),
            "isolation": "docker_compose_linux_netem",
        },
        "intervention": {
            "action": "add_client_egress_transport_delay",
            "interface": INTERFACE,
            "requested_delay_ms": NETEM_DELAY_MS,
            "samples_per_phase": SAMPLES,
        },
        "observations": {
            "target_host": TARGET_HOST,
            "target_ip": target_ip,
            "target_port": TARGET_PORT,
            "qdisc_during_intervention": qdisc,
            "baseline": baseline,
            "intervention": intervention,
            "recovery": recovery,
            "deltas": deltas,
            "minimum_transport_delta_ms": minimum_transport_delta_ms,
        },
        "assertions": assertions,
        "result": "supports" if supported else "contradicts",
        "interpretation": (
            "Linux netem delay on the client transport path raised TCP connection "
            "establishment and request exchange timing while target-side processing "
            "remained comparable, then timing recovered after the qdisc was removed."
            if supported
            else "The controlled netem intervention did not satisfy every transport-latency assertion."
        ),
        "limitations": [
            "This is a deterministic Linux/Docker traffic-control experiment rather than a measurement of a physical production path.",
            "Client-side egress delay isolates transport timing but does not identify which real network hop would be responsible.",
            "The experiment does not model packet loss, reordering, congestion, or asymmetric delay.",
        ],
    }

    print(json.dumps(evidence, sort_keys=True))
    if not supported:
        print(json.dumps({"failed_assertions": [key for key, value in assertions.items() if not value]}), file=sys.stderr)


if __name__ == "__main__":
    main()
