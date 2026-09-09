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
TRUSTED_CLIENT_CERT = os.environ.get("TRUSTED_CLIENT_CERT", "/certs/client.crt")
TRUSTED_CLIENT_KEY = os.environ.get("TRUSTED_CLIENT_KEY", "/certs/client.key")
WRONG_CA_CLIENT_CERT = os.environ.get("WRONG_CA_CLIENT_CERT", "/certs/wrong-ca-client.crt")
WRONG_CA_CLIENT_KEY = os.environ.get("WRONG_CA_CLIENT_KEY", "/certs/wrong-ca-client.key")
SOCKET_TIMEOUT = float(os.environ.get("SOCKET_TIMEOUT", "1.0"))

EXPERIMENT_ID = "experiment.network.tls_client_certificate_wrong_ca.python_linux"
CLAIM_ID = "claim.network.tls_client_certificate_authentication_failure.wrong_ca_client_cert_rejected"


def attempt(certfile, keyfile, issuer_label):
    started = time.monotonic()
    raw = None
    result = {
        "server_name": SERVER_NAME,
        "tcp_connected": False,
        "tls_established": False,
        "usable_secure_session": False,
        "client_certificate_present": True,
        "client_certificate_issuer": issuer_label,
        "server_certificate_verification_failed": False,
        "phase": "tcp_connect",
        "application_response": None,
    }

    try:
        raw = socket.create_connection((TLS_SERVER, TLS_PORT), timeout=SOCKET_TIMEOUT)
        result["tcp_connected"] = True
        result["phase"] = "tls_handshake"

        context = ssl.create_default_context(cafile=CA_FILE)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)

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
        result.update({
            "server_certificate_verification_failed": True,
            "error_class": type(exc).__name__,
            "error_message": str(exc),
            "verify_code": exc.verify_code,
            "verify_message": exc.verify_message,
            "ssl_library": getattr(exc, "library", None),
            "ssl_reason": getattr(exc, "reason", None),
        })
    except ssl.SSLError as exc:
        result.update({
            "error_class": type(exc).__name__,
            "error_message": str(exc),
            "ssl_library": getattr(exc, "library", None),
            "ssl_reason": getattr(exc, "reason", None),
        })
    except OSError as exc:
        result.update({
            "error_class": type(exc).__name__,
            "error_message": str(exc),
            "errno": exc.errno,
        })
    finally:
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass
        result["elapsed_ms"] = (time.monotonic() - started) * 1000

    return result


baseline = attempt(TRUSTED_CLIENT_CERT, TRUSTED_CLIENT_KEY, "trusted_lab_ca")
intervention = attempt(WRONG_CA_CLIENT_CERT, WRONG_CA_CLIENT_KEY, "rogue_lab_ca")
recovery = attempt(TRUSTED_CLIENT_CERT, TRUSTED_CLIENT_KEY, "trusted_lab_ca")

assertions = {
    "baseline_mtls_succeeds": baseline["tcp_connected"] and baseline["usable_secure_session"],
    "baseline_tls13": baseline.get("tls_version") == "TLSv1.3",
    "intervention_tcp_connects": intervention["tcp_connected"],
    "intervention_presents_client_certificate": intervention["client_certificate_present"],
    "intervention_uses_wrong_ca": intervention["client_certificate_issuer"] == "rogue_lab_ca",
    "intervention_server_certificate_verification_does_not_fail": not intervention["server_certificate_verification_failed"],
    "intervention_cannot_use_secure_application_session": not intervention["usable_secure_session"],
    "intervention_surfaces_tls_error": intervention.get("error_class") == "SSLError",
    "recovery_mtls_succeeds": recovery["tcp_connected"] and recovery["usable_secure_session"],
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
        "action": "replace_trusted_client_certificate_with_certificate_signed_by_untrusted_ca",
        "server_client_auth_policy": "CERT_REQUIRED",
        "trusted_server_ca_file": CA_FILE,
        "baseline_client_certificate": TRUSTED_CLIENT_CERT,
        "intervention_client_certificate": WRONG_CA_CLIENT_CERT,
        "tls_versions": ["TLSv1.3"],
    },
    "observations": {"baseline": baseline, "intervention": intervention, "recovery": recovery},
    "assertions": assertions,
    "result": result,
    "interpretation": "A valid clientAuth certificate from the server's trusted CA produces a usable mTLS session. Replacing only that client credential with an otherwise valid clientAuth certificate signed by a different CA leaves TCP and server trust intact but prevents usable protected traffic; restoring the trusted client certificate recovers.",
    "limitations": [
        "The wrong-CA condition is synthetic and controlled.",
        "TLS alert timing can surface at handshake completion or first protected I/O depending on OpenSSL/runtime behavior.",
        "This does not cover expiration, revocation, wrong EKU, malformed chains, or authorization after successful mTLS.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
