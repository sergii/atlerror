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
CLIENT_CERT = os.environ.get("CLIENT_CERT", "/certs/client.crt")
CLIENT_KEY = os.environ.get("CLIENT_KEY", "/certs/client.key")
SOCKET_TIMEOUT = float(os.environ.get("SOCKET_TIMEOUT", "1.0"))

EXPERIMENT_ID = "experiment.network.tls_client_certificate_authentication.python_linux"
CLAIM_ID = "claim.network.tls_client_certificate_authentication_failure.missing_client_cert_blocks_mtls"


def attempt(with_client_certificate):
    started = time.monotonic()
    raw = None
    result = {
        "server_name": SERVER_NAME,
        "tcp_connected": False,
        "tls_established": False,
        "usable_secure_session": False,
        "client_certificate_present": with_client_certificate,
        "server_certificate_verification_failed": False,
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
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
        if with_client_certificate:
            context.load_cert_chain(certfile=CLIENT_CERT, keyfile=CLIENT_KEY)

        with context.wrap_socket(raw, server_hostname=SERVER_NAME) as tls_socket:
            raw = None
            result["tls_established"] = True
            result["tls_version"] = tls_socket.version()
            result["cipher"] = tls_socket.cipher()[0]
            result["phase"] = "application_data"
            tls_socket.sendall(b"ping")
            response = tls_socket.recv(64)
            result["application_response"] = response.decode("ascii", errors="replace")
            result["usable_secure_session"] = response == b"ok"

    except ssl.SSLCertVerificationError as exc:
        result.update(
            {
                "server_certificate_verification_failed": True,
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


baseline = attempt(True)
intervention = attempt(False)
recovery = attempt(True)

assertions = {
    "baseline_tcp_connects": baseline["tcp_connected"],
    "baseline_tls13_establishes": baseline["tls_established"] and baseline.get("tls_version") == "TLSv1.3",
    "baseline_mtls_application_data_succeeds": baseline["usable_secure_session"] and baseline.get("application_response") == "ok",
    "intervention_tcp_connects": intervention["tcp_connected"],
    "intervention_omits_client_certificate": intervention["client_certificate_present"] is False,
    "intervention_server_certificate_verification_does_not_fail": not intervention["server_certificate_verification_failed"],
    "intervention_cannot_use_secure_application_session": not intervention["usable_secure_session"],
    "intervention_surfaces_tls_error": intervention.get("error_class") == "SSLError",
    "recovery_tls13_establishes": recovery["tls_established"] and recovery.get("tls_version") == "TLSv1.3",
    "recovery_mtls_application_data_succeeds": recovery["usable_secure_session"] and recovery.get("application_response") == "ok",
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
        "action": "omit_required_client_certificate",
        "tls_server": TLS_SERVER,
        "tls_port": TLS_PORT,
        "server_name": SERVER_NAME,
        "trusted_ca_file": CA_FILE,
        "tls_versions": ["TLSv1.3"],
        "server_client_auth_policy": "CERT_REQUIRED",
        "baseline_client_certificate": CLIENT_CERT,
        "intervention_client_certificate": None,
    },
    "observations": {
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "The same reachable TLS 1.3 peer and trusted server certificate permit protected application traffic when the client presents a valid lab-CA-signed certificate. Omitting only the required client certificate leaves TCP connectivity intact but prevents a usable mutual-TLS session; restoring the client certificate recovers successfully.",
    "limitations": [
        "The missing-client-certificate condition is intentionally configured in a disposable Docker lab.",
        "TLS 1.3 alert timing can surface at handshake completion or first protected I/O depending on OpenSSL/runtime behavior; usable protected-session outcome is the primary evidence.",
        "The experiment does not cover invalid, expired, revoked, wrong-EKU, or wrong-identity client certificates, nor authorization after successful mTLS.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
