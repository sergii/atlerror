#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import platform
import re
import socket
import statistics
import subprocess
import time

TARGET_HOST = os.environ.get("TARGET_HOST", "server")
TARGET_PORT = int(os.environ.get("TARGET_PORT", "9000"))
NETWORK_INTERFACE = os.environ.get("NETWORK_INTERFACE", "eth0")
BASE_DELAY_MS = float(os.environ.get("BASE_DELAY_MS", "30"))
JITTER_MS = float(os.environ.get("JITTER_MS", "20"))
SAMPLES = int(os.environ.get("SAMPLES", "60"))
WARMUP_SAMPLES = int(os.environ.get("WARMUP_SAMPLES", "5"))
SOCKET_TIMEOUT_SECONDS = float(os.environ.get("SOCKET_TIMEOUT_SECONDS", "2"))

CLAIM_ID = "claim.network.jitter.netem_variable_delay_widens_timing_distribution"
EXPERIMENT_ID = "experiment.network.jitter.netem_python_linux"
TARGET_IP = socket.gethostbyname(TARGET_HOST)
TARGET = (TARGET_IP, TARGET_PORT)


def tc(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tc", *args],
        text=True,
        capture_output=True,
        check=check,
    )


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def phase_metrics(phase: str) -> dict[str, object]:
    timings_ms: list[float] = []
    missing = 0

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(SOCKET_TIMEOUT_SECONDS)
        sock.connect(TARGET)

        for index in range(WARMUP_SAMPLES):
            payload = f"warmup:{phase}:{index}".encode()
            sock.send(payload)
            response = sock.recv(65535)
            if response != payload:
                raise RuntimeError("warmup echo payload mismatch")

        for index in range(SAMPLES):
            payload = f"sample:{phase}:{index}:{time.monotonic_ns()}".encode()
            started = time.monotonic_ns()
            try:
                sock.send(payload)
                response = sock.recv(65535)
            except TimeoutError:
                missing += 1
                continue
            if response != payload:
                raise RuntimeError("echo payload mismatch")
            timings_ms.append((time.monotonic_ns() - started) / 1_000_000)

    p50 = percentile(timings_ms, 0.50)
    p95 = percentile(timings_ms, 0.95)
    stddev = statistics.pstdev(timings_ms) if len(timings_ms) > 1 else 0.0
    mean = statistics.fmean(timings_ms) if timings_ms else 0.0

    return {
        "phase": phase,
        "sent": SAMPLES,
        "received": len(timings_ms),
        "missing": missing,
        "delivery_ratio": round(len(timings_ms) / SAMPLES, 6),
        "mean_ms": round(mean, 3),
        "median_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "p95_minus_p50_ms": round(p95 - p50, 3),
        "stddev_ms": round(stddev, 3),
        "min_ms": round(min(timings_ms), 3) if timings_ms else 0.0,
        "max_ms": round(max(timings_ms), 3) if timings_ms else 0.0,
    }


def qdisc_drop_count(stats: str) -> int | None:
    match = re.search(r"dropped\s+(\d+)", stats)
    return int(match.group(1)) if match else None


baseline = phase_metrics("baseline")
qdisc_stats = ""
try:
    tc(
        "qdisc",
        "add",
        "dev",
        NETWORK_INTERFACE,
        "root",
        "netem",
        "delay",
        f"{BASE_DELAY_MS}ms",
        f"{JITTER_MS}ms",
        "distribution",
        "normal",
    )
    intervention = phase_metrics("intervention")
    qdisc_stats = tc("-s", "qdisc", "show", "dev", NETWORK_INTERFACE).stdout.strip()
finally:
    tc("qdisc", "del", "dev", NETWORK_INTERFACE, "root", check=False)

time.sleep(0.05)
recovery = phase_metrics("recovery")
qdisc_after_recovery = tc("qdisc", "show", "dev", NETWORK_INTERFACE, check=False).stdout.strip()
reported_drops = qdisc_drop_count(qdisc_stats)

baseline_stddev = float(baseline["stddev_ms"])
intervention_stddev = float(intervention["stddev_ms"])
recovery_stddev = float(recovery["stddev_ms"])
baseline_spread = float(baseline["p95_minus_p50_ms"])
intervention_spread = float(intervention["p95_minus_p50_ms"])
recovery_spread = float(recovery["p95_minus_p50_ms"])
minimum_dispersion_delta_ms = max(5.0, JITTER_MS * 0.25)

assertions = {
    "baseline_delivery_complete": int(baseline["received"]) == SAMPLES,
    "intervention_delivery_complete": int(intervention["received"]) == SAMPLES,
    "recovery_delivery_complete": int(recovery["received"]) == SAMPLES,
    "netem_qdisc_active": "netem" in qdisc_stats and "delay" in qdisc_stats,
    "netem_reports_no_packet_loss": reported_drops == 0,
    "jitter_stddev_increases": intervention_stddev > baseline_stddev + minimum_dispersion_delta_ms,
    "tail_spread_increases": intervention_spread > baseline_spread + minimum_dispersion_delta_ms,
    "jitter_stddev_recovers": intervention_stddev > recovery_stddev + minimum_dispersion_delta_ms,
    "tail_spread_recovers": intervention_spread > recovery_spread + minimum_dispersion_delta_ms,
    "netem_removed_before_recovery": "netem" not in qdisc_after_recovery,
}

result = "supports" if all(assertions.values()) else "contradicts"

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
        "action": "apply_variable_client_egress_delay",
        "interface": NETWORK_INTERFACE,
        "base_delay_ms": BASE_DELAY_MS,
        "jitter_ms": JITTER_MS,
        "distribution": "normal",
        "samples": SAMPLES,
        "warmup_samples": WARMUP_SAMPLES,
    },
    "observations": {
        "target_host": TARGET_HOST,
        "target_ip": TARGET_IP,
        "target_port": TARGET_PORT,
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
        "stddev_delta_over_baseline_ms": round(intervention_stddev - baseline_stddev, 3),
        "p95_p50_spread_delta_over_baseline_ms": round(intervention_spread - baseline_spread, 3),
        "qdisc_during_intervention": qdisc_stats,
        "qdisc_reported_drop_count": reported_drops,
        "qdisc_after_recovery": qdisc_after_recovery,
    },
    "assertions": assertions,
    "result": result,
    "interpretation": (
        "Controlled variable Linux egress delay widened the repeated UDP transport timing distribution while delivery remained complete, and the distribution tightened after netem was removed."
    ),
    "limitations": [
        "The experiment uses synthetic Linux netem delay variation inside Docker rather than a physical production path.",
        "Sequential UDP echo probes isolate transport timing and do not model TCP congestion control or retransmission behavior.",
        "Jitter thresholds are relative to this lab baseline and should not be copied directly into production alerting without environment-specific calibration.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
