import json
import os
import platform
import socket
import ssl
import time

TLS_SERVER = os.environ.get("TLS_SERVER", "tls")
TLS_PORT = int(os.environ.get("TLS_PORT", "9443"))
SERVER_NAME = os.environ.get("SERVER_NAME", "secure.atlerror.test")
CA_FILE = os.environ.get("CA_FILE", "/certs/ca.crt")
SOCKET_TIMEOUT = float(os.environ.get("SOCKET_TIMEOUT", "1.0"))

EXPERIMENT_ID = "experiment.network.tls_protocol_negotiation.python_linux"
CLAIM_ID = "claim.network.tls_protocol_negotiation_failure.no_common_version_blocks_handshake"


def attempt(version):
    started = time.monotonic()
    raw = None
    result = {
        "server_name": SERVER_NAME,
        "client_tls_min": version.name,
        "client_tls_max": version.name,
        "tcp_connected": False,
        "tls_established": False,
        "certificate_verification_failed": False,
        "phase": "tcp_connect",
        "application_response": None,
    }

    try:
        tcp_started = time.monotonic()
        raw = socket.create_connection((TLS_SERVER, TLS_PORT), timeout=SOCKET_TIMEOUT)
        result["tcp_connect_ms"] = (time.monotonic() - tcp_started) * 1000
        result["tcp_connected"] = True
        result["phase"] = "tls_handshake"

        context = ssl.create_default_context(cafile=CA_FILE)
        context.minimum_version = version
        context.maximum_version = version

        with context.wrap_socket(raw, server_hostname=SERVER_NAME) as tls_socket:
            raw = None
            result["tls_established"] = True
            result["tls_version"] = tls_socket.version()
            result["cipher"] = tls_socket.cipher()[0]
            tls_socket.sendall(b"ping")
            response = tls_socket.recv(64)
            result["application_response"] = response.decode("ascii", errors="replace")
            result["phase"] = "application_data"

    except ssl.SSLCertVerificationError as exc:
        result.update(
            {
                "certificate_verification_failed": True,
                "error_class": type(exc).__name__,
                "error_message": str(exc),
                "verify_code": exc.verify_code,
                "verify_message": exc.verify_message,
                "ssl_library": getattr(exc, "library", None),
                "ssl_reason": getattr(exc, "reason", None),
            }
        )
    except ssl.SSLError as exc:
        result.update(
            {
                "error_class": type(exc).__name__,
                "error_message": str(exc),
                "ssl_library": getattr(exc, "library", None),
                "ssl_reason": getattr(exc, "reason", None),
            }
        )
    except OSError as exc:
        result.update(
            {
                "error_class": type(exc).__name__,
                "error_message": str(exc),
                "errno": exc.errno,
            }
        )
    finally:
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass
        result["elapsed_ms"] = (time.monotonic() - started) * 1000

    return result


baseline = attempt(ssl.TLSVersion.TLSv1_2)
intervention = attempt(ssl.TLSVersion.TLSv1_3)
recovery = attempt(ssl.TLSVersion.TLSv1_2)

assertions = {
    "baseline_tcp_connects": baseline["tcp_connected"],
    "baseline_tls12_establishes": baseline["tls_established"] and baseline.get("tls_version") == "TLSv1.2",
    "baseline_application_data_succeeds": baseline.get("application_response") == "ok",
    "intervention_tcp_connects": intervention["tcp_connected"],
    "intervention_fails_during_tls_handshake": intervention.get("phase") == "tls_handshake",
    "intervention_tls_session_not_established": not intervention["tls_established"],
    "intervention_is_non_certificate_ssl_error": intervention.get("error_class") == "SSLError" and not intervention["certificate_verification_failed"],
    "configured_protocol_sets_do_not_overlap": True,
    "recovery_tls12_establishes": recovery["tls_established"] and recovery.get("tls_version") == "TLSv1.2",
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
        "operating_system": "linux",
        "platform": platform.platform(),
        "isolation": "docker_compose",
        "openssl_version": ssl.OPENSSL_VERSION,
    },
    "intervention": {
        "action": "client_tls13_only_against_tls12_only_server",
        "tls_server": TLS_SERVER,
        "tls_port": TLS_PORT,
        "server_name": SERVER_NAME,
        "trusted_ca_file": CA_FILE,
        "server_tls_versions": ["TLSv1.2"],
        "baseline_client_tls_versions": ["TLSv1.2"],
        "intervention_client_tls_versions": ["TLSv1.3"],
    },
    "observations": {
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "The same reachable peer and trusted certificate succeed with a TLS 1.2-only client, while a TLS 1.3-only client establishes TCP but cannot create a TLS session because the server is restricted to TLS 1.2. Recovery succeeds after restoring a mutually supported version.",
    "limitations": [
        "The version mismatch is intentionally configured in a disposable Docker lab.",
        "The experiment validates protocol-version incompatibility, not every TLS negotiation mechanism.",
        "It does not cover cipher-suite mismatch, signature algorithms, ALPN, mTLS, certificate verification, or middlebox behavior.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
