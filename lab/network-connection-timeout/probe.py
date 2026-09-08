import errno
import json
import os
import platform
import socket
import time

HOST = os.environ.get("TARGET_HOST", "server")
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "9000"))
DROPPED_PORT = int(os.environ.get("DROPPED_PORT", "9001"))
CONNECT_TIMEOUT_SECONDS = float(os.environ.get("CONNECT_TIMEOUT_SECONDS", "0.6"))

EXPERIMENT_ID = "experiment.network.connection_timeout.python_linux"
CLAIM_ID = "claim.network.connection_timeout.dropped_syn_expires_connect_deadline"


def normal_roundtrip():
    started = time.monotonic()
    with socket.create_connection((HOST, CONTROL_PORT), timeout=2.0) as connection:
        connect_ms = (time.monotonic() - started) * 1000.0
        connection.sendall(b"ping")
        payload = connection.recv(16)
    return {
        "connected": True,
        "connect_ms": connect_ms,
        "response": payload.decode("ascii", errors="replace"),
    }


def timed_out_connection():
    started = time.monotonic()
    try:
        connection = socket.create_connection(
            (HOST, DROPPED_PORT), timeout=CONNECT_TIMEOUT_SECONDS
        )
        elapsed_ms = (time.monotonic() - started) * 1000.0
        connection.close()
        return {
            "connected": True,
            "elapsed_ms": elapsed_ms,
            "phase": "connect",
            "is_timeout": False,
            "transport_error": None,
            "errno": None,
        }
    except OSError as exc:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        is_timeout = isinstance(exc, (TimeoutError, socket.timeout)) or exc.errno == errno.ETIMEDOUT
        return {
            "connected": False,
            "elapsed_ms": elapsed_ms,
            "phase": "connect",
            "is_timeout": is_timeout,
            "transport_error": errno.errorcode.get(exc.errno, type(exc).__name__),
            "errno": exc.errno,
            "error_class": type(exc).__name__,
            "message": str(exc),
        }


baseline = normal_roundtrip()
intervention = timed_out_connection()
recovery = normal_roundtrip()
minimum_deadline_ms = CONNECT_TIMEOUT_SECONDS * 1000.0 * 0.75

assertions = {
    "baseline_target_is_reachable": baseline.get("response") == "pong",
    "connection_does_not_establish_on_dropped_port": intervention.get("connected") is False,
    "failure_occurs_during_connect": intervention.get("phase") == "connect",
    "client_observes_connect_timeout": intervention.get("is_timeout") is True,
    "no_active_refusal_or_reset": intervention.get("errno") not in {errno.ECONNREFUSED, errno.ECONNRESET},
    "configured_deadline_materially_elapsed": intervention.get("elapsed_ms", 0) >= minimum_deadline_ms,
    "recovery_target_is_reachable": recovery.get("response") == "pong",
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
        "action": "silently_drop_inbound_tcp_to_target_port_with_netfilter",
        "target_host": HOST,
        "control_port": CONTROL_PORT,
        "dropped_port": DROPPED_PORT,
        "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
    },
    "observations": {
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "The same target remains reachable on a control port before and after intervention. TCP traffic to the dropped port receives no active refusal or successful handshake, so the client remains in the connect phase until its configured deadline expires.",
    "limitations": [
        "The silent drop is produced by a controlled Linux netfilter rule in Docker.",
        "Python may surface a socket deadline as TimeoutError without a numeric ETIMEDOUT errno.",
        "Real connect timeouts can originate from packet loss, routing, firewall policy, unreachable hosts, asymmetric paths, or infrastructure behavior.",
        "The experiment does not cover application read or request timeouts after TCP establishment.",
    ],
}
print(json.dumps(evidence))
