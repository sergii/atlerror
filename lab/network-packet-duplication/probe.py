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
DUPLICATE_PERCENT = float(os.getenv("DUPLICATE_PERCENT", "100"))
BURSTS = int(os.getenv("BURSTS", "5"))
PACKETS_PER_BURST = int(os.getenv("PACKETS_PER_BURST", "20"))
RECEIVE_IDLE_TIMEOUT_SECONDS = float(os.getenv("RECEIVE_IDLE_TIMEOUT_SECONDS", "0.25"))

CLAIM_ID = "claim.network.packet_duplication.netem_duplicate_repeats_udp_identity_without_loss"
EXPERIMENT_ID = "experiment.network.packet_duplication.netem_python_linux"


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def qdisc_show() -> str:
    return run("tc", "-s", "qdisc", "show", "dev", NETWORK_INTERFACE).stdout.strip()


def clear_netem() -> None:
    run("tc", "qdisc", "del", "dev", NETWORK_INTERFACE, "root", check=False)


def apply_duplication() -> None:
    run(
        "tc",
        "qdisc",
        "replace",
        "dev",
        NETWORK_INTERFACE,
        "root",
        "netem",
        "duplicate",
        f"{DUPLICATE_PERCENT:g}%",
    )


def parse_drop_count(qdisc: str) -> int:
    marker = "dropped "
    if marker not in qdisc:
        return 0
    try:
        return int(qdisc.split(marker, 1)[1].split(",", 1)[0].strip())
    except ValueError:
        return -1


def measure_phase(phase: str) -> dict[str, object]:
    target_ip = socket.gethostbyname(TARGET_HOST)
    received_total = 0
    missing_total = 0
    duplicates_total = 0
    duplicate_identities_total = 0
    burst_summaries: list[dict[str, object]] = []

    for burst in range(BURSTS):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(RECEIVE_IDLE_TIMEOUT_SECONDS)
        expected = list(range(PACKETS_PER_BURST))

        for sequence in expected:
            payload = f"{phase}:{burst}:{sequence}".encode()
            sock.sendto(payload, (target_ip, TARGET_PORT))

        arrivals: list[int] = []
        while True:
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
        expected_set = set(expected)
        unique = set(arrivals)
        missing = len(expected_set - unique)
        duplicates = sum(max(0, count - 1) for count in counts.values())
        duplicate_identities = sum(1 for sequence in expected if counts[sequence] > 1)
        max_copies = max(counts.values(), default=0)

        received_total += len(arrivals)
        missing_total += missing
        duplicates_total += duplicates
        duplicate_identities_total += duplicate_identities

        burst_summaries.append(
            {
                "burst": burst,
                "received": len(arrivals),
                "unique_received": len(unique & expected_set),
                "missing": missing,
                "duplicate_copies": duplicates,
                "duplicate_identities": duplicate_identities,
                "max_copies_per_identity": max_copies,
                "arrival_prefix": arrivals[:16],
            }
        )

    sent_total = BURSTS * PACKETS_PER_BURST
    unique_received_total = sent_total - missing_total
    return {
        "phase": phase,
        "sent": sent_total,
        "received": received_total,
        "unique_received": unique_received_total,
        "missing": missing_total,
        "duplicate_copies": duplicates_total,
        "duplicate_identities": duplicate_identities_total,
        "unique_delivery_ratio": unique_received_total / sent_total if sent_total else 0.0,
        "copy_ratio": received_total / sent_total if sent_total else 0.0,
        "bursts": BURSTS,
        "packets_per_burst": PACKETS_PER_BURST,
        "burst_summaries": burst_summaries,
    }


def main() -> int:
    clear_netem()
    baseline = measure_phase("baseline")

    apply_duplication()
    qdisc_during = qdisc_show()
    intervention = measure_phase("intervention")
    qdisc_during_after = qdisc_show()

    clear_netem()
    qdisc_after = qdisc_show()
    recovery = measure_phase("recovery")

    drop_count = parse_drop_count(qdisc_during_after)
    sent_total = int(intervention["sent"])
    duplicate_threshold = max(BURSTS, sent_total // 2)
    baseline_duplicates = int(baseline["duplicate_copies"])
    intervention_duplicates = int(intervention["duplicate_copies"])
    recovery_duplicates = int(recovery["duplicate_copies"])

    assertions = {
        "baseline_unique_delivery_complete": baseline["missing"] == 0,
        "baseline_has_no_duplicate_copies": baseline_duplicates == 0,
        "intervention_unique_delivery_complete": intervention["missing"] == 0,
        "duplicate_copies_increase": intervention_duplicates >= baseline_duplicates + duplicate_threshold,
        "many_identities_are_duplicated": int(intervention["duplicate_identities"]) >= duplicate_threshold,
        "received_copy_count_exceeds_sent_count": int(intervention["received"]) >= sent_total + duplicate_threshold,
        "recovery_unique_delivery_complete": recovery["missing"] == 0,
        "duplication_recovers": recovery_duplicates <= baseline_duplicates,
        "netem_qdisc_active": "netem" in qdisc_during and "duplicate" in qdisc_during,
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
            "action": "apply_deterministic_client_egress_packet_duplication",
            "interface": NETWORK_INTERFACE,
            "duplicate_percent": DUPLICATE_PERCENT,
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
            "duplicate_copy_delta_over_baseline": intervention_duplicates - baseline_duplicates,
            "qdisc_during_intervention": qdisc_during_after,
            "qdisc_reported_drop_count": drop_count,
            "qdisc_after_recovery": qdisc_after,
        },
        "assertions": assertions,
        "interpretation": "Controlled Linux egress duplication caused the same UDP sequence identities to be observed multiple times while every unique identity remained deliverable, and duplicate delivery disappeared after netem was removed.",
        "limitations": [
            "The experiment uses synthetic Linux netem packet duplication inside Docker rather than a physical production path.",
            "UDP exposes duplicate datagrams directly; TCP normally suppresses duplicate segment payload before application delivery.",
            "The exact duplicate ratio can vary under queue or socket-buffer pressure, so the lab asserts a strong duplicate pattern rather than exact two-copy delivery.",
        ],
    }

    print(json.dumps(evidence, sort_keys=True))
    return 0 if result == "supports" else 1


if __name__ == "__main__":
    sys.exit(main())
