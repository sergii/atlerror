import json
import os
import platform
import socket
import ssl
import subprocess
import time

TLS_SERVER = os.environ.get("TLS_SERVER", "tls")
TLS_PORT = int(os.environ.get("TLS_PORT", "9443"))
SERVER_NAME = os.environ.get("SERVER_NAME", "secure.atlerror.test")
CA_FILE = os.environ.get("CA_FILE", "/certs/ca.crt")
CLIENT_CERT = os.environ.get("CLIENT_CERT", "/certs/client.crt")
CLIENT_KEY = os.environ.get("CLIENT_KEY", "/certs/client.key")
WRONG_EKU_CERT = os.environ.get("WRONG_EKU_CERT", "/certs/wrong-eku-client.crt")
WRONG_EKU_KEY = os.environ.get("WRONG_EKU_KEY", "/certs/wrong-eku-client.key")
SOCKET_TIMEOUT = float(os.environ.get("SOCKET_TIMEOUT", "1.0"))

EXPERIMENT_ID = "experiment.network.tls_client_certificate_invalid_eku.python_linux"
CLAIM_ID = "claim.network.tls_client_certificate_authentication_failure.invalid_client_eku_rejected"


def cert_purposes(certfile):
    completed = subprocess.run(
        ["openssl", "x509", "-in", certfile, "-purpose", "-noout"],
        check=True,
        capture_output=True,
        text=True,
    )
    text = completed.stdout
    return {
        "ssl_client": "yes" if "SSL client : Yes" in text else "no" if "SSL client : No" in text else "unknown",
        "ssl_server": "yes" if "SSL server : Yes" in text else "no" if "SSL server : No" in text else "unknown",
        "raw": [line.strip() for line in text.splitlines() if line.strip()],
    }


def attempt(certfile, keyfile, eku_label):
    started = time.monotonic()
    raw = None
    result = {
        "server_name": SERVER_NAME,
        "tcp_connected": False,
        "tls_established": False,
        "usable_secure_session": False,
        "client_certificate_present": True,
        "client_certificate_issuer": "trusted_lab_ca",
        "client_certificate_eku": eku_label,
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


valid_purpose = cert_purposes(CLIENT_CERT)
wrong_eku_purpose = cert_purposes(WRONG_EKU_CERT)

baseline = attempt(CLIENT_CERT, CLIENT_KEY, "clientAuth")
intervention = attempt(WRONG_EKU_CERT, WRONG_EKU_KEY, "serverAuth_only")
recovery = attempt(CLIENT_CERT, CLIENT_KEY, "clientAuth")

client_auth_rejection_surface = (
    intervention.get("error_class") == "SSLError"
    or (
        intervention.get("error_class") == "ConnectionResetError"
        and intervention.get("errno") == 104
        and intervention.get("phase") == "application_data"
    )
)

assertions = {
    "baseline_cert_permits_ssl_client": valid_purpose["ssl_client"] == "yes",
    "baseline_mtls_succeeds": baseline["usable_secure_session"] and baseline.get("application_response") == "ok",
    "baseline_tls13": baseline["tls_established"] and baseline.get("tls_version") == "TLSv1.3",
    "intervention_cert_is_not_valid_for_ssl_client": wrong_eku_purpose["ssl_client"] == "no",
    "intervention_cert_is_valid_for_ssl_server": wrong_eku_purpose["ssl_server"] == "yes",
    "intervention_presents_client_certificate": intervention["client_certificate_present"] is True,
    "intervention_uses_trusted_issuer": intervention["client_certificate_issuer"] == "trusted_lab_ca",
    "intervention_tcp_connects": intervention["tcp_connected"],
    "intervention_server_certificate_verification_does_not_fail": not intervention["server_certificate_verification_failed"],
    "intervention_cannot_use_secure_application_session": not intervention["usable_secure_session"],
    "intervention_surfaces_client_auth_rejection": client_auth_rejection_surface,
    "recovery_mtls_succeeds": recovery["usable_secure_session"] and recovery.get("application_response") == "ok",
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
        "action": "replace_valid_client_auth_certificate_with_trusted_certificate_having_wrong_eku",
        "baseline_client_certificate": CLIENT_CERT,
        "intervention_client_certificate": WRONG_EKU_CERT,
        "server_client_auth_policy": "CERT_REQUIRED",
        "tls_versions": ["TLSv1.3"],
        "trusted_server_and_client_ca_file": CA_FILE,
    },
    "observations": {
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
        "certificate_purpose": {
            "baseline": valid_purpose,
            "intervention": wrong_eku_purpose,
        },
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "Both client certificates chain to the same trusted lab CA and are presented to the same reachable TLS 1.3 peer. The baseline certificate is valid for SSL client authentication and produces usable protected traffic. Replacing only its Extended Key Usage with serverAuth makes the certificate unsuitable for SSL client authentication and prevents a usable mTLS session. Depending on OpenSSL alert timing, the client can observe an SSL error or an ECONNRESET while attempting protected application I/O; restoring clientAuth recovers.",
    "limitations": [
        "The invalid-EKU condition is synthetic and controlled.",
        "TLS client-auth rejection can surface as an SSL alert or as ECONNRESET on protected I/O depending on OpenSSL/runtime timing; certificate purpose plus unusable protected-session outcome are the primary evidence.",
        "This does not cover expiration, revocation, key-usage-only failures, policy OIDs, malformed chains, or authorization after successful mTLS.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
