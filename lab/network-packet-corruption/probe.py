from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys

TARGET_HOST = os.getenv("TARGET_HOST", "server")
TARGET_PORT = int(os.getenv("TARGET_PORT", "9000"))
NETWORK_INTERFACE = os.getenv("NETWORK_INTERFACE", "eth0")
CORRUPT_PERCENT = float(os.getenv("CORRUPT_PERCENT", "50"))
NETEM_SEED = int(os.getenv("NETEM_SEED", "424242"))
SAMPLES = int(os.getenv("SAMPLES", "80"))
PAYLOAD_BYTES = int(os.getenv("PAYLOAD_BYTES", "1100"))
SOCKET_TIMEOUT_SECONDS = float(os.getenv("SOCKET_TIMEOUT_SECONDS", "0.5"))
SO_NO_CHECK = getattr(socket, "SO_NO_CHECK", 11)

CLAIM_ID = "claim.network.packet_corruption.netem_corrupt_changes_udp_payload_integrity"
EXPERIMENT_ID = "experiment.network.packet_corruption.netem_python_linux"


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def qdisc_show() -> str:
    return run("tc", "-s", "qdisc", "show", "dev", NETWORK_INTERFACE).stdout.strip()


def clear_netem() -> None:
    run("tc", "qdisc", "del", "dev", NETWORK_INTERFACE, "root", check=False)


def apply_corruption() -> None:
    run(
        "tc",
        "qdisc",
        "replace",
        "dev",
        NETWORK_INTERFACE,
        "root",
        "netem",
        "corrupt",
        f"{CORRUPT_PERCENT:g}%",
        "seed",
        str(NETEM_SEED),
    )


def parse_drop_count(qdisc: str) -> int:
    marker = "dropped "
    if marker not in qdisc:
        return 0
    try:
        return int(qdisc.split(marker, 1)[1].split(",", 1)[0].strip())
    except ValueError:
        return -1


def payload_for(phase: str, sequence: int) -> bytes:
    header = f"atlerror:{phase}:{sequence}:".encode()
    body_length = max(0, PAYLOAD_BYTES - len(header))
    body = bytes((sequence * 17 + offset) % 251 for offset in range(body_length))
    return header + body


def measure_phase(phase: str, target_ip: str) -> dict[str, object]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, SO_NO_CHECK, 1)
    checksum_disabled = bool(sock.getsockopt(socket.SOL_SOCKET, SO_NO_CHECK))
    sock.settimeout(SOCKET_TIMEOUT_SECONDS)

    clean = 0
    corrupted = 0
    missing = 0
    sample_summaries: list[dict[str, object]] = []

    for sequence in range(SAMPLES):
        expected = payload_for(phase, sequence)
        sock.sendto(expected, (target_ip, TARGET_PORT))
        try:
            returned, _ = sock.recvfrom(max(4096, PAYLOAD_BYTES + 128))
        except socket.timeout:
            missing += 1
            if len(sample_summaries) < 12:
                sample_summaries.append({"sequence": sequence, "outcome": "missing"})
            continue

        if returned == expected:
            clean += 1
            outcome = "clean"
        else:
            corrupted += 1
            outcome = "corrupted"

        if len(sample_summaries) < 12:
            first_difference = next(
                (index for index, pair in enumerate(zip(expected, returned)) if pair[0] != pair[1]),
                min(len(expected), len(returned)) if len(expected) != len(returned) else None,
            )
            sample_summaries.append(
                {
                    "sequence": sequence,
                    "outcome": outcome,
                    "sent_bytes": len(expected),
                    "received_bytes": len(returned),
                    "first_difference_offset": first_difference,
                }
            )

    sock.close()
    received = clean + corrupted
    return {
        "phase": phase,
        "sent": SAMPLES,
        "received": received,
        "clean": clean,
        "corrupted": corrupted,
        "missing": missing,
        "delivery_ratio": received / SAMPLES if SAMPLES else 0.0,
        "corruption_ratio_of_received": corrupted / received if received else 0.0,
        "udp_checksum_disabled": checksum_disabled,
        "sample_summaries": sample_summaries,
    }


def main() -> int:
    target_ip = socket.gethostbyname(TARGET_HOST)
    clear_netem()
    baseline = measure_phase("baseline", target_ip)

    apply_corruption()
    qdisc_during = qdisc_show()
    intervention = measure_phase("intervention", target_ip)
    qdisc_during_after = qdisc_show()

    clear_netem()
    qdisc_after = qdisc_show()
    recovery = measure_phase("recovery", target_ip)

    drop_count = parse_drop_count(qdisc_during_after)
    baseline_corrupted = int(baseline["corrupted"])
    intervention_corrupted = int(intervention["corrupted"])
    recovery_corrupted = int(recovery["corrupted"])
    minimum_corrupted = max(10, int(SAMPLES * 0.20))
    minimum_received = max(1, int(SAMPLES * 0.75))

    assertions = {
        "udp_checksum_rejection_is_bypassed": bool(baseline["udp_checksum_disabled"]) and bool(intervention["udp_checksum_disabled"]) and bool(recovery["udp_checksum_disabled"]),
        "baseline_integrity_clean": baseline_corrupted == 0 and baseline["missing"] == 0,
        "intervention_mostly_deliverable": int(intervention["received"]) >= minimum_received,
        "payload_corruption_visible": intervention_corrupted >= minimum_corrupted,
        "corruption_increases": intervention_corrupted >= baseline_corrupted + minimum_corrupted,
        "corruption_affects_received_payloads": float(intervention["corruption_ratio_of_received"]) >= 0.20,
        "netem_qdisc_active": "netem" in qdisc_during and "corrupt" in qdisc_during,
        "netem_reports_no_packet_loss": drop_count == 0,
        "recovery_integrity_clean": recovery_corrupted == 0 and recovery["missing"] == 0,
        "corruption_recovers": recovery_corrupted <= baseline_corrupted,
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
            "action": "apply_seeded_client_egress_packet_corruption",
            "interface": NETWORK_INTERFACE,
            "corrupt_percent": CORRUPT_PERCENT,
            "netem_seed": NETEM_SEED,
            "samples": SAMPLES,
            "payload_bytes": PAYLOAD_BYTES,
            "udp_checksum_disabled": True,
        },
        "observations": {
            "target_host": TARGET_HOST,
            "target_ip": target_ip,
            "target_port": TARGET_PORT,
            "baseline": baseline,
            "intervention": intervention,
            "recovery": recovery,
            "corrupted_payload_delta_over_baseline": intervention_corrupted - baseline_corrupted,
            "qdisc_during_intervention": qdisc_during_after,
            "qdisc_reported_drop_count": drop_count,
            "qdisc_after_recovery": qdisc_after,
        },
        "assertions": assertions,
        "interpretation": "Controlled Linux egress corruption changed returned UDP payload bytes while most probes remained deliverable because sender UDP checksum rejection was intentionally bypassed, and exact payload integrity returned after netem was removed.",
        "limitations": [
            "The experiment uses synthetic Linux netem packet corruption inside Docker rather than a physical production path.",
            "The probe intentionally disables sender UDP checksums so content corruption is application-visible; normal integrity enforcement can instead discard corrupted packets and make the symptom appear as packet loss.",
            "The experiment does not model TCP retransmission, link-layer FCS rejection, or production NIC checksum-offload behavior.",
        ],
    }

    print(json.dumps(evidence, sort_keys=True))
    return 0 if result == "supports" else 1


if __name__ == "__main__":
    sys.exit(main())
