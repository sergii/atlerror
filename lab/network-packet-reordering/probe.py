from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
from collections import Counter

TARGET_HOST = os.getenv("TARGET_HOST", "server")
TARGET_PORT = int(os.getenv("TARGET_PORT", "9000"))
NETWORK_INTERFACE = os.getenv("NETWORK_INTERFACE", "eth0")
REORDER_DELAY_MS = float(os.getenv("REORDER_DELAY_MS", "40"))
REORDER_GAP = int(os.getenv("REORDER_GAP", "5"))
BURSTS = int(os.getenv("BURSTS", "5"))
PACKETS_PER_BURST = int(os.getenv("PACKETS_PER_BURST", "30"))
SOCKET_TIMEOUT_SECONDS = float(os.getenv("SOCKET_TIMEOUT_SECONDS", "3"))

CLAIM_ID = "claim.network.packet_reordering.netem_reorder_changes_arrival_sequence_without_loss"
EXPERIMENT_ID = "experiment.network.packet_reordering.netem_python_linux"


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def qdisc_show() -> str:
    return run("tc", "-s", "qdisc", "show", "dev", NETWORK_INTERFACE).stdout.strip()


def clear_netem() -> None:
    run("tc", "qdisc", "del", "dev", NETWORK_INTERFACE, "root", check=False)


def apply_reordering() -> None:
    # netem needs non-zero delay for reordering to become observable.
    run(
        "tc",
        "qdisc",
        "replace",
        "dev",
        NETWORK_INTERFACE,
        "root",
        "netem",
        "delay",
        f"{REORDER_DELAY_MS:g}ms",
        "reorder",
        "100%",
        "gap",
        str(REORDER_GAP),
    )


def parse_drop_count(qdisc: str) -> int:
    marker = "dropped "
    if marker not in qdisc:
        return 0
    try:
        return int(qdisc.split(marker, 1)[1].split(",", 1)[0].strip())
    except ValueError:
        return -1


def inversion_count(values: list[int]) -> int:
    return sum(1 for i, left in enumerate(values) for right in values[i + 1 :] if left > right)


def measure_phase(phase: str) -> dict[str, object]:
    target_ip = socket.gethostbyname(TARGET_HOST)
    phase_inversions = 0
    reordered_bursts = 0
    received_total = 0
    duplicates_total = 0
    missing_total = 0
    burst_summaries: list[dict[str, object]] = []

    for burst in range(BURSTS):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(SOCKET_TIMEOUT_SECONDS)
        expected = list(range(PACKETS_PER_BURST))

        for sequence in expected:
            payload = f"{phase}:{burst}:{sequence}".encode()
            sock.sendto(payload, (target_ip, TARGET_PORT))

        arrivals: list[int] = []
        while len(arrivals) < PACKETS_PER_BURST:
            try:
                payload, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            try:
                returned_phase, returned_burst, returned_sequence = payload.decode().split(":", 2)
                if returned_phase != phase or int(returned_burst) != burst:
                    continue
                arrivals.append(int(returned_sequence))
            except (UnicodeDecodeError, ValueError):
                continue
        sock.close()

        counts = Counter(arrivals)
        unique = set(arrivals)
        missing = len(set(expected) - unique)
        duplicates = sum(max(0, count - 1) for count in counts.values())
        inversions = inversion_count(arrivals)
        displaced = sum(1 for index, value in enumerate(arrivals) if index < len(expected) and value != expected[index])

        received_total += len(arrivals)
        missing_total += missing
        duplicates_total += duplicates
        phase_inversions += inversions
        if inversions > 0:
            reordered_bursts += 1

        burst_summaries.append(
            {
                "burst": burst,
                "received": len(arrivals),
                "missing": missing,
                "duplicates": duplicates,
                "inversions": inversions,
                "displaced_positions": displaced,
                "arrival_prefix": arrivals[:12],
            }
        )

    sent_total = BURSTS * PACKETS_PER_BURST
    return {
        "phase": phase,
        "sent": sent_total,
        "received": received_total,
        "missing": missing_total,
        "duplicates": duplicates_total,
        "delivery_ratio": received_total / sent_total if sent_total else 0.0,
        "inversions": phase_inversions,
        "reordered_bursts": reordered_bursts,
        "bursts": BURSTS,
        "packets_per_burst": PACKETS_PER_BURST,
        "burst_summaries": burst_summaries,
    }


def main() -> int:
    clear_netem()
    baseline = measure_phase("baseline")

    apply_reordering()
    qdisc_during = qdisc_show()
    intervention = measure_phase("intervention")
    qdisc_during_after = qdisc_show()

    clear_netem()
    qdisc_after = qdisc_show()
    recovery = measure_phase("recovery")

    drop_count = parse_drop_count(qdisc_during_after)
    baseline_inversions = int(baseline["inversions"])
    intervention_inversions = int(intervention["inversions"])
    recovery_inversions = int(recovery["inversions"])

    assertions = {
        "baseline_delivery_complete": baseline["missing"] == 0 and baseline["duplicates"] == 0,
        "intervention_delivery_complete": intervention["missing"] == 0 and intervention["duplicates"] == 0,
        "recovery_delivery_complete": recovery["missing"] == 0 and recovery["duplicates"] == 0,
        "baseline_order_preserved": baseline_inversions == 0,
        "reordering_increases": intervention_inversions >= max(5, baseline_inversions + 5),
        "multiple_bursts_reordered": int(intervention["reordered_bursts"]) >= max(2, BURSTS // 2),
        "ordering_recovers": recovery_inversions <= baseline_inversions,
        "netem_qdisc_active": "netem" in qdisc_during and "reorder" in qdisc_during,
        "netem_reports_no_packet_loss": drop_count == 0,
        "netem_removed_before_recovery": "netem" not in qdisc_after,
    }

    result = "supports" if all(assertions.values()) else "does_not_support"
    evidence = {
        "schema_version": "0.1",
        "kind": "empirical_evidence",
        "experiment": EXPERIMENT_ID,
        "claims": [CLAIM_ID],
        "result": result,
        "environment": {
            "runtime": "python",
            "runtime_version": platform.python_version(),
            "platform": platform.platform(),
            "isolation": "docker_compose_linux_netem",
        },
        "intervention": {
            "action": "apply_deterministic_client_egress_reordering",
            "interface": NETWORK_INTERFACE,
            "delay_ms": REORDER_DELAY_MS,
            "reorder_percent": 100,
            "gap": REORDER_GAP,
            "bursts": BURSTS,
            "packets_per_burst": PACKETS_PER_BURST,
        },
        "observations": {
            "target_host": TARGET_HOST,
            "target_ip": socket.gethostbyname(TARGET_HOST),
            "target_port": TARGET_PORT,
            "baseline": baseline,
            "intervention": intervention,
            "recovery": recovery,
            "inversion_delta_over_baseline": intervention_inversions - baseline_inversions,
            "qdisc_during_intervention": qdisc_during_after,
            "qdisc_reported_drop_count": drop_count,
            "qdisc_after_recovery": qdisc_after,
        },
        "assertions": assertions,
        "interpretation": "Controlled Linux egress reordering changed UDP arrival sequence while delivery remained complete, and ordered delivery returned after netem was removed.",
        "limitations": [
            "The experiment uses synthetic Linux netem reordering inside Docker rather than a physical production path.",
            "UDP exposes arrival order directly; TCP normally hides packet reordering behind ordered byte-stream delivery and transport recovery.",
            "The exact reordered sequence can vary with scheduler and queue timing, so the lab asserts an ordering pattern rather than an exact inversion count.",
        ],
    }

    print(json.dumps(evidence, sort_keys=True))
    return 0 if result == "supports" else 1


if __name__ == "__main__":
    sys.exit(main())
