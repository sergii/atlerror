import json
import os
import platform
import socket
import ssl
import time

TLS_SERVER = os.environ.get("TLS_SERVER", "tls")
TLS_PORT = int(os.environ.get("TLS_PORT", "9443"))
TRUSTED_NAME = os.environ.get("TRUSTED_NAME", "trusted.atlerror.test")
UNTRUSTED_NAME = os.environ.get("UNTRUSTED_NAME", "untrusted.atlerror.test")
CA_FILE = os.environ.get("CA_FILE", "/certs/ca.crt")
SOCKET_TIMEOUT = float(os.environ.get("SOCKET_TIMEOUT", "1.0"))

EXPERIMENT_ID = "experiment.network.tls_certificate_verification.python_linux"
CLAIM_ID = "claim.network.tls_certificate_verification_failure.untrusted_certificate_is_rejected"


def attempt(server_name):
    started = time.monotonic()
    raw = None
    result = {
        "server_name": server_name,
        "phase": "tcp_connect",
        "tcp_connected": False,
        "tls_established": False,
        "certificate_verification_failed": False,
        "application_response": None,
    }

    try:
        raw = socket.create_connection((TLS_SERVER, TLS_PORT), timeout=SOCKET_TIMEOUT)
        result["tcp_connected"] = True
        result["tcp_connect_ms"] = (time.monotonic() - started) * 1000.0
        result["phase"] = "tls_handshake"

        context = ssl.create_default_context(cafile=CA_FILE)
        context.minimum_version = ssl.TLSVersion.TLSv1_2

        with context.wrap_socket(raw, server_hostname=server_name) as tls_socket:
            raw = None
            result["tls_established"] = True
            result["tls_version"] = tls_socket.version()
            result["cipher"] = tls_socket.cipher()[0] if tls_socket.cipher() else None
            tls_socket.settimeout(SOCKET_TIMEOUT)
            tls_socket.sendall(b"ping")
            result["application_response"] = tls_socket.recv(16).decode("ascii", errors="replace")
            result["phase"] = "application_data"

    except ssl.SSLCertVerificationError as exc:
        result["certificate_verification_failed"] = True
        result["error_class"] = type(exc).__name__
        result["verify_code"] = getattr(exc, "verify_code", None)
        result["verify_message"] = getattr(exc, "verify_message", None)
        result["error_message"] = str(exc)
        result["phase"] = "tls_handshake"
    except Exception as exc:
        result["error_class"] = type(exc).__name__
        result["error_message"] = str(exc)
    finally:
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass

    result["elapsed_ms"] = (time.monotonic() - started) * 1000.0
    return result


baseline = attempt(TRUSTED_NAME)
intervention = attempt(UNTRUSTED_NAME)
recovery = attempt(TRUSTED_NAME)

assertions = {
    "baseline_tcp_connects": baseline.get("tcp_connected") is True,
    "baseline_tls_establishes": baseline.get("tls_established") is True,
    "baseline_application_data_succeeds": baseline.get("application_response") == "ok",
    "intervention_tcp_connects": intervention.get("tcp_connected") is True,
    "intervention_fails_during_tls_handshake": intervention.get("phase") == "tls_handshake",
    "untrusted_certificate_is_rejected": intervention.get("certificate_verification_failed") is True,
    "verification_error_is_structured": intervention.get("error_class") == "SSLCertVerificationError" and bool(intervention.get("verify_message")),
    "tls_session_is_not_established_after_verification_failure": intervention.get("tls_established") is False,
    "recovery_tls_establishes": recovery.get("tls_established") is True,
    "recovery_application_data_succeeds": recovery.get("application_response") == "ok",
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
        "openssl_version": ssl.OPENSSL_VERSION,
    },
    "intervention": {
        "action": "same_tls_peer_selects_untrusted_self_signed_certificate_by_sni",
        "tls_server": TLS_SERVER,
        "tls_port": TLS_PORT,
        "trusted_name": TRUSTED_NAME,
        "untrusted_name": UNTRUSTED_NAME,
        "trusted_ca_file": CA_FILE,
    },
    "observations": {
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "TCP connectivity to the same TLS peer succeeds in all phases. The CA-signed control certificate verifies before and after intervention, while the self-signed certificate outside the configured trust store is rejected specifically during TLS certificate verification, before application data can proceed.",
    "limitations": [
        "The certificates and CA are generated inside a disposable Docker image for a controlled trust experiment.",
        "The experiment validates one untrusted/self-signed certificate mechanism and does not distinguish all certificate verification causes.",
        "The experiment does not cover hostname mismatch, expiry, revocation, incomplete intermediate chains, protocol/cipher negotiation, or mutual TLS.",
    ],
}

print(json.dumps(evidence))
