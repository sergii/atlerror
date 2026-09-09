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
LOSS_PERCENT = int(os.environ.get("LOSS_PERCENT", "100"))
SAMPLES_PER_PHASE = int(os.environ.get("SAMPLES_PER_PHASE", "8"))
SOCKET_TIMEOUT_SECONDS = float(os.environ.get("SOCKET_TIMEOUT_SECONDS", "0.20"))

TARGET_IP = socket.gethostbyname(TARGET_HOST)
TARGET = (TARGET_IP, TARGET_PORT)


def tc(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tc", *args],
        text=True,
        capture_output=True,
        check=check,
    )


def probe_once(phase: str, sequence: int) -> dict[str, object]:
    payload = f"atlerror:{phase}:{sequence}".encode()
    started = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(SOCKET_TIMEOUT_SECONDS)
    try:
        sock.sendto(payload, TARGET)
        response, address = sock.recvfrom(65535)
        elapsed_ms = (time.monotonic() - started) * 1000
        return {
            "sequence": sequence,
            "delivered": response == payload and address[0] == TARGET_IP,
            "timed_out": False,
            "elapsed_ms": round(elapsed_ms, 3),
            "error": None,
        }
    except socket.timeout:
        elapsed_ms = (time.monotonic() - started) * 1000
        return {
            "sequence": sequence,
            "delivered": False,
            "timed_out": True,
            "elapsed_ms": round(elapsed_ms, 3),
            "error": None,
        }
    except OSError as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        return {
            "sequence": sequence,
            "delivered": False,
            "timed_out": False,
            "elapsed_ms": round(elapsed_ms, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        sock.close()


def measure_phase(phase: str) -> dict[str, object]:
    samples = [probe_once(phase, index) for index in range(SAMPLES_PER_PHASE)]
    delivered = sum(1 for sample in samples if sample["delivered"])
    timed_out = sum(1 for sample in samples if sample["timed_out"])
    errors = [sample["error"] for sample in samples if sample["error"]]
    elapsed = [float(sample["elapsed_ms"]) for sample in samples]
    sent = len(samples)
    return {
        "sent": sent,
        "delivered": delivered,
        "timed_out": timed_out,
        "errors": errors,
        "delivery_ratio": round(delivered / sent, 4),
        "loss_ratio": round((sent - delivered) / sent, 4),
        "elapsed_ms_median": round(statistics.median(elapsed), 3),
        "samples": samples,
    }


def qdisc_drop_count(stats: str) -> int | None:
    match = re.search(r"dropped\s+(\d+)", stats)
    return int(match.group(1)) if match else None


baseline = measure_phase("baseline")
qdisc_stats = ""

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
    intervention = measure_phase("intervention")
    qdisc_stats = tc("-s", "qdisc", "show", "dev", NETWORK_INTERFACE).stdout.strip()
finally:
    tc("qdisc", "del", "dev", NETWORK_INTERFACE, "root", check=False)

time.sleep(0.05)
recovery = measure_phase("recovery")
reported_drops = qdisc_drop_count(qdisc_stats)

assertions = {
    "baseline_delivery_complete": baseline["delivered"] == SAMPLES_PER_PHASE,
    "intervention_delivery_zero": intervention["delivered"] == 0,
    "intervention_loss_ratio_is_one": intervention["loss_ratio"] == 1.0,
    "intervention_uses_receive_deadlines_not_explicit_transport_errors": (
        intervention["timed_out"] == SAMPLES_PER_PHASE and not intervention["errors"]
    ),
    "netem_qdisc_active": "netem" in qdisc_stats,
    "netem_reports_dropped_packets": reported_drops is not None and reported_drops >= SAMPLES_PER_PHASE,
    "recovery_delivery_complete": recovery["delivered"] == SAMPLES_PER_PHASE,
    "recovery_has_no_probe_errors": not recovery["errors"],
}

result = "supports" if all(assertions.values()) else "contradicts"

evidence = {
    "schema_version": "0.1",
    "kind": "empirical_evidence",
    "experiment": "experiment.network.packet_loss.netem_python_linux",
    "claims": ["claim.network.packet_loss.netem_drop_reduces_delivery"],
    "environment": {
        "runtime": "python",
        "runtime_version": platform.python_version(),
        "platform": platform.platform(),
        "isolation": "docker_compose_linux_netem",
    },
    "intervention": {
        "action": "drop_client_egress_packets",
        "interface": NETWORK_INTERFACE,
        "requested_loss_percent": LOSS_PERCENT,
        "samples_per_phase": SAMPLES_PER_PHASE,
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
        "delivery_ratio_delta": round(
            float(intervention["delivery_ratio"]) - float(baseline["delivery_ratio"]), 4
        ),
    },
    "assertions": assertions,
    "result": result,
    "interpretation": (
        "Linux netem egress loss removed all controlled UDP probe delivery while the target remained healthy, "
        "the qdisc reported dropped packets, and delivery returned after the loss rule was removed."
    ),
    "limitations": [
        "This deterministic experiment uses 100 percent UDP egress loss rather than a realistic partial-loss distribution.",
        "UDP exposes missing delivery directly; TCP may retransmit and transform transient loss into additional latency before an application-visible failure.",
        "The synthetic qdisc proves packet loss on the controlled container path but does not identify a physical production hop or root cause.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
