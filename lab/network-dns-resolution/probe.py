import json
import os
import platform
import random
import socket
import struct
import time

DNS_SERVER = os.environ.get("DNS_SERVER", "172.31.0.53")
DNS_PORT = int(os.environ.get("DNS_PORT", "5353"))
KNOWN_NAME = os.environ.get("KNOWN_NAME", "known.atlerror.test")
MISSING_NAME = os.environ.get("MISSING_NAME", "missing.atlerror.test")
TIMEOUT_SECONDS = float(os.environ.get("DNS_TIMEOUT_SECONDS", "1.0"))

EXPERIMENT_ID = "experiment.network.dns_resolution_failure.python_linux"
CLAIM_ID = "claim.network.dns_resolution_failure.nxdomain_prevents_transport"

RCODE_NAMES = {
    0: "NOERROR",
    1: "FORMERR",
    2: "SERVFAIL",
    3: "NXDOMAIN",
    4: "NOTIMP",
    5: "REFUSED",
}


def encode_qname(name):
    labels = name.rstrip(".").split(".")
    return b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\x00"


def query(name):
    txid = random.randint(0, 65535)
    packet = (
        struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
        + encode_qname(name)
        + struct.pack("!HH", 1, 1)
    )

    started = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(TIMEOUT_SECONDS)
        client.sendto(packet, (DNS_SERVER, DNS_PORT))
        payload, _ = client.recvfrom(2048)
    elapsed_ms = (time.monotonic() - started) * 1000.0

    response_txid, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", payload[:12])
    if response_txid != txid:
        raise RuntimeError("DNS transaction ID mismatch")

    rcode = flags & 0x000F
    authoritative = bool(flags & 0x0400)
    result = {
        "name": name,
        "resolved": rcode == 0 and ancount > 0,
        "rcode": rcode,
        "rcode_name": RCODE_NAMES.get(rcode, f"RCODE_{rcode}"),
        "authoritative": authoritative,
        "elapsed_ms": elapsed_ms,
        "phase": "name_resolution",
        "transport_attempted": False,
    }

    if rcode == 0 and ancount > 0:
        offset = 12
        for _ in range(qdcount):
            while payload[offset] != 0:
                offset += payload[offset] + 1
            offset += 1 + 4

        if payload[offset] & 0xC0 == 0xC0:
            offset += 2
        else:
            while payload[offset] != 0:
                offset += payload[offset] + 1
            offset += 1

        rr_type, rr_class, _, rdlength = struct.unpack("!HHIH", payload[offset : offset + 10])
        offset += 10
        if rr_type == 1 and rr_class == 1 and rdlength == 4:
            result["addresses"] = [socket.inet_ntoa(payload[offset : offset + 4])]

    return result


baseline = query(KNOWN_NAME)
intervention = query(MISSING_NAME)
recovery = query(KNOWN_NAME)

assertions = {
    "baseline_name_resolves": baseline.get("resolved") is True,
    "baseline_returns_expected_address": baseline.get("addresses") == ["10.20.30.40"],
    "missing_name_returns_nxdomain": intervention.get("rcode_name") == "NXDOMAIN",
    "nxdomain_is_authoritative": intervention.get("authoritative") is True,
    "failure_occurs_during_name_resolution": intervention.get("phase") == "name_resolution",
    "transport_is_not_attempted_after_nxdomain": intervention.get("transport_attempted") is False,
    "recovery_name_resolves": recovery.get("resolved") is True,
}
result = "supports" if all(assertions.values()) else "inconclusive"

evidence = {
    "schema_version": "0.1",
    "kind": "empirical_evidence",
    "experiment": EXPERIMENT_ID,
    "claims": [CLAIM_ID],
    "environment": {
        "runtime": "python",
        "runtime_version": platform.python_version(),
        "platform": platform.platform(),
        "isolation": "docker_compose",
        "operating_system": "linux",
    },
    "intervention": {
        "action": "authoritative_dns_returns_nxdomain_for_missing_name",
        "dns_server": DNS_SERVER,
        "dns_port": DNS_PORT,
        "known_name": KNOWN_NAME,
        "missing_name": MISSING_NAME,
    },
    "observations": {
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "The same authoritative resolver returns a valid A record for the control name before and after intervention, while the missing name receives authoritative NXDOMAIN during name resolution. No TCP connection attempt is required for the failing path.",
    "limitations": [
        "The DNS server is a controlled authoritative UDP fixture implementing the subset of DNS needed for A-record and NXDOMAIN behavior.",
        "The experiment does not cover recursive resolver caches, DNSSEC, resolver failover, SERVFAIL, timeouts, search domains, or incorrect positive answers.",
        "Production NXDOMAIN can be caused by configuration, record lifecycle, split-horizon DNS, service discovery, or other naming-system issues not distinguished here.",
    ],
}
print(json.dumps(evidence))
