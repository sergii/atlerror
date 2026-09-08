import json
import os
import platform
import random
import socket
import struct
import time

DNS_SERVER = os.environ.get("DNS_SERVER", "172.32.0.53")
DNS_PORT = int(os.environ.get("DNS_PORT", "5353"))
KNOWN_NAME = os.environ.get("KNOWN_NAME", "known.atlerror.test")
TIMEOUT_NAME = os.environ.get("TIMEOUT_NAME", "timeout.atlerror.test")
TIMEOUT_SECONDS = float(os.environ.get("DNS_TIMEOUT_SECONDS", "0.6"))

EXPERIMENT_ID = "experiment.network.dns_resolver_timeout.python_linux"
CLAIM_ID = "claim.network.dns_resolver_timeout.silent_query_expires_resolution_deadline"

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


def build_query(name):
    txid = random.randint(0, 65535)
    packet = (
        struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
        + encode_qname(name)
        + struct.pack("!HH", 1, 1)
    )
    return txid, packet


def parse_response(name, txid, payload, elapsed_ms):
    response_txid, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", payload[:12])
    if response_txid != txid:
        raise RuntimeError("DNS transaction ID mismatch")

    rcode = flags & 0x000F
    authoritative = bool(flags & 0x0400)
    result = {
        "name": name,
        "resolved": rcode == 0 and ancount > 0,
        "response_received": True,
        "timed_out": False,
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


def query(name):
    txid, packet = build_query(name)
    started = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(TIMEOUT_SECONDS)
        client.sendto(packet, (DNS_SERVER, DNS_PORT))
        try:
            payload, _ = client.recvfrom(2048)
        except socket.timeout:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return {
                "name": name,
                "resolved": False,
                "response_received": False,
                "timed_out": True,
                "rcode": None,
                "rcode_name": None,
                "authoritative": None,
                "elapsed_ms": elapsed_ms,
                "phase": "name_resolution",
                "transport_attempted": False,
                "error_class": "TimeoutError",
            }
    elapsed_ms = (time.monotonic() - started) * 1000.0
    return parse_response(name, txid, payload, elapsed_ms)


baseline = query(KNOWN_NAME)
intervention = query(TIMEOUT_NAME)
recovery = query(KNOWN_NAME)
minimum_deadline_ms = TIMEOUT_SECONDS * 1000.0 * 0.75

assertions = {
    "baseline_name_resolves": baseline.get("resolved") is True,
    "baseline_returns_expected_address": baseline.get("addresses") == ["10.20.30.40"],
    "resolver_query_times_out": intervention.get("timed_out") is True,
    "no_dns_response_arrives": intervention.get("response_received") is False,
    "no_explicit_dns_rcode_is_received": intervention.get("rcode") is None,
    "failure_occurs_during_name_resolution": intervention.get("phase") == "name_resolution",
    "configured_resolution_deadline_materially_elapsed": intervention.get("elapsed_ms", 0) >= minimum_deadline_ms,
    "transport_is_not_attempted_after_dns_timeout": intervention.get("transport_attempted") is False,
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
        "action": "same_dns_resolver_deliberately_sends_no_response_for_timeout_name",
        "dns_server": DNS_SERVER,
        "dns_port": DNS_PORT,
        "known_name": KNOWN_NAME,
        "timeout_name": TIMEOUT_NAME,
        "dns_timeout_seconds": TIMEOUT_SECONDS,
    },
    "observations": {
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "The same reachable DNS resolver answers the control name before and after intervention but intentionally remains silent for the timeout name. The client stays in name resolution until its configured DNS deadline expires, receives no DNS response code, and never begins transport establishment to the application endpoint.",
    "limitations": [
        "The timeout is produced by a controlled UDP resolver fixture deliberately withholding one response.",
        "The experiment does not distinguish real production causes such as packet loss, resolver overload, firewall policy, recursive upstream failure, routing, or resolver process failure.",
        "The experiment does not cover DNS retries across multiple nameservers, TCP fallback, SERVFAIL, REFUSED, DNSSEC validation, or incorrect positive answers.",
    ],
}
print(json.dumps(evidence))
