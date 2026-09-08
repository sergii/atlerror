import json
import os
import platform
import time

import psycopg

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "atlerror")
ADMIN_USER = os.environ.get("ADMIN_USER", "atlerror_admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "atlerror")
APP_USER = os.environ.get("APP_USER", "atlerror_app")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "atlerror_app")
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "20"))

EXPERIMENT_ID = "experiment.database.connection_admission_exhaustion.python_postgres"
CLAIM_ID = "claim.database.connection_admission_exhaustion.postgres_rejects_new_sessions"


def conninfo(user, password):
    return (
        f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
        f"user={user} password={password} connect_timeout=3"
    )


admin = psycopg.connect(conninfo(ADMIN_USER, ADMIN_PASSWORD), autocommit=True)
held = []
rejection = None
recovery = None
baseline = {}
capacity = {}

try:
    with admin.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {APP_USER} LOGIN PASSWORD %s", (APP_PASSWORD,))
        cursor.execute("SHOW max_connections")
        max_connections = int(cursor.fetchone()[0])
        cursor.execute("SHOW superuser_reserved_connections")
        superuser_reserved_connections = int(cursor.fetchone()[0])

    baseline_started = time.monotonic()
    baseline_connection = psycopg.connect(conninfo(APP_USER, APP_PASSWORD))
    baseline_connect_ms = (time.monotonic() - baseline_started) * 1000.0
    with baseline_connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        baseline_value = cursor.fetchone()[0]
    baseline_connection.close()
    baseline = {
        "ordinary_connection_succeeded": True,
        "connect_ms": baseline_connect_ms,
        "select_1": baseline_value,
    }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            connection = psycopg.connect(conninfo(APP_USER, APP_PASSWORD))
            held.append(connection)
        except psycopg.Error as exc:
            rejection = {
                "attempt": attempt,
                "sqlstate": exc.sqlstate,
                "error_class": type(exc).__name__,
                "message": str(exc).strip(),
            }
            break

    with admin.cursor() as cursor:
        cursor.execute("SELECT 1")
        admin_select = cursor.fetchone()[0]
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        )
        active_sessions = int(cursor.fetchone()[0])
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND usename = %s",
            (APP_USER,),
        )
        app_sessions = int(cursor.fetchone()[0])

    capacity = {
        "max_connections": max_connections,
        "superuser_reserved_connections": superuser_reserved_connections,
        "held_ordinary_connections": len(held),
        "active_sessions_in_database": active_sessions,
        "app_sessions_in_database": app_sessions,
        "existing_admin_session_select_1": admin_select,
    }

    if held:
        held.pop().close()

    recovery_started = time.monotonic()
    try:
        recovery_connection = psycopg.connect(conninfo(APP_USER, APP_PASSWORD))
        recovery_connect_ms = (time.monotonic() - recovery_started) * 1000.0
        with recovery_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            recovery_value = cursor.fetchone()[0]
        recovery_connection.close()
        recovery = {
            "ordinary_connection_succeeded": True,
            "connect_ms": recovery_connect_ms,
            "select_1": recovery_value,
            "sqlstate": None,
        }
    except psycopg.Error as exc:
        recovery = {
            "ordinary_connection_succeeded": False,
            "connect_ms": (time.monotonic() - recovery_started) * 1000.0,
            "select_1": None,
            "sqlstate": exc.sqlstate,
            "message": str(exc).strip(),
        }
finally:
    for connection in held:
        try:
            connection.close()
        except Exception:
            pass
    admin.close()

assertions = {
    "baseline_ordinary_connection_succeeds": baseline.get("ordinary_connection_succeeded") is True,
    "ordinary_connections_can_fill_capacity": capacity.get("held_ordinary_connections", 0) > 0,
    "new_ordinary_connection_is_rejected": rejection is not None,
    "rejection_is_sqlstate_53300": rejection is not None and rejection.get("sqlstate") == "53300",
    "existing_admin_session_remains_usable": capacity.get("existing_admin_session_select_1") == 1,
    "releasing_one_session_restores_admission": recovery.get("ordinary_connection_succeeded") is True,
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
        "database": "PostgreSQL 17",
    },
    "intervention": {
        "action": "open_ordinary_postgresql_sessions_until_server_rejects_new_admission",
        "configured_max_connections": capacity.get("max_connections"),
        "configured_superuser_reserved_connections": capacity.get("superuser_reserved_connections"),
        "max_attempts": MAX_ATTEMPTS,
    },
    "observations": {
        "baseline": baseline,
        "intervention": {
            "rejection": rejection,
            "capacity": capacity,
        },
        "recovery": recovery,
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "Ordinary PostgreSQL sessions are opened until a new ordinary connection is rejected by server-side admission capacity. The expected discriminator is SQLSTATE 53300 while an already established administrative session remains healthy. Closing one ordinary session should restore ordinary connection admission.",
    "limitations": [
        "PostgreSQL is configured with a deliberately small max_connections value for deterministic reproduction.",
        "The experiment validates server-side session admission, not safe production max_connections sizing.",
        "Managed services, proxies, PgBouncer, role-specific reservations, and network layers can add different admission limits or error surfaces.",
    ],
}
print(json.dumps(evidence))
