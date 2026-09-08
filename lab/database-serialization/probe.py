import json
import os
import platform
import sys
import time

import psycopg

EXPERIMENT_ID = "experiment.database.serialization_failure.python_postgres"
CLAIM_ID = "claim.database.serialization_failure.concurrent_serializable_update_aborts_transaction"
DSN = (
    f"host={os.environ.get('DB_HOST', 'db')} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"user={os.environ.get('DB_USER', 'atlerror')} "
    f"password={os.environ.get('DB_PASSWORD', 'atlerror')} "
    f"dbname={os.environ.get('DB_NAME', 'atlerror')}"
)


def connect():
    conn = psycopg.connect(DSN)
    conn.execute("SET statement_timeout = '5s'")
    return conn


def prepare(value=0):
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS counters (id integer PRIMARY KEY, value integer NOT NULL)")
        conn.execute("INSERT INTO counters(id, value) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value", (value,))


def read_value():
    with connect() as conn:
        return conn.execute("SELECT value FROM counters WHERE id = 1").fetchone()[0]


def serializable_increment(label):
    started = time.perf_counter()
    conn = connect()
    try:
        conn.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
        value = conn.execute("SELECT value FROM counters WHERE id = 1").fetchone()[0]
        conn.execute("UPDATE counters SET value = %s WHERE id = 1", (value + 1,))
        conn.commit()
        return {"worker": label, "outcome": "committed", "sqlstate": None, "read_value": value, "elapsed_ms": (time.perf_counter() - started) * 1000}
    except psycopg.Error as exc:
        conn.rollback()
        return {"worker": label, "outcome": "error", "sqlstate": exc.sqlstate, "read_value": None, "elapsed_ms": (time.perf_counter() - started) * 1000}
    finally:
        conn.close()


def baseline_phase():
    prepare(0)
    first = serializable_increment("A")
    second = serializable_increment("B")
    return {"transactions": [first, second], "final_value": read_value()}


def intervention_phase():
    prepare(0)
    stale = connect()
    winner = connect()
    stale_read = None
    winner_result = None
    stale_result = None
    try:
        stale.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
        stale_read = stale.execute("SELECT value FROM counters WHERE id = 1").fetchone()[0]

        winner_started = time.perf_counter()
        winner.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
        winner_read = winner.execute("SELECT value FROM counters WHERE id = 1").fetchone()[0]
        winner.execute("UPDATE counters SET value = %s WHERE id = 1", (winner_read + 1,))
        winner.commit()
        winner_result = {"worker": "winner", "outcome": "committed", "sqlstate": None, "read_value": winner_read, "elapsed_ms": (time.perf_counter() - winner_started) * 1000}

        stale_started = time.perf_counter()
        try:
            stale.execute("UPDATE counters SET value = %s WHERE id = 1", (stale_read + 1,))
            stale.commit()
            stale_result = {"worker": "stale", "outcome": "committed", "sqlstate": None, "read_value": stale_read, "elapsed_ms": (time.perf_counter() - stale_started) * 1000}
        except psycopg.Error as exc:
            stale.rollback()
            outcome = "serialization_aborted" if exc.sqlstate == "40001" else "error"
            stale_result = {"worker": "stale", "outcome": outcome, "sqlstate": exc.sqlstate, "read_value": stale_read, "elapsed_ms": (time.perf_counter() - stale_started) * 1000}
    finally:
        stale.close()
        winner.close()

    return {"transactions": [winner_result, stale_result], "stale_snapshot_value": stale_read, "final_value": read_value()}


def recovery_phase():
    retry = serializable_increment("retry")
    return {"transaction": retry, "final_value": read_value()}


def main():
    baseline = baseline_phase()
    intervention = intervention_phase()
    recovery = recovery_phase()

    intervention_states = [tx.get("sqlstate") for tx in intervention["transactions"] if tx]
    intervention_outcomes = [tx.get("outcome") for tx in intervention["transactions"] if tx]

    assertions = {
        "baseline_both_serializable_transactions_commit": all(tx["outcome"] == "committed" for tx in baseline["transactions"]),
        "baseline_final_value_is_two": baseline["final_value"] == 2,
        "concurrent_winner_commits": intervention["transactions"][0]["outcome"] == "committed",
        "stale_serializable_transaction_is_aborted": intervention["transactions"][1]["outcome"] == "serialization_aborted",
        "postgres_returns_sqlstate_40001": "40001" in intervention_states,
        "intervention_is_not_deadlock_40P01": "40P01" not in intervention_states,
        "aborted_stale_write_does_not_change_value": intervention["final_value"] == 1,
        "fresh_retry_commits": recovery["transaction"]["outcome"] == "committed",
        "recovery_final_value_is_two": recovery["final_value"] == 2,
        "no_unexpected_intervention_error": "error" not in intervention_outcomes,
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
            "action": "commit_conflicting_update_after_stale_serializable_snapshot",
            "isolation_level": "SERIALIZABLE",
            "recovery": "retry_failed_work_in_fresh_serializable_transaction",
        },
        "observations": {"baseline": baseline, "intervention": intervention, "recovery": recovery},
        "assertions": assertions,
        "result": result,
        "interpretation": "A PostgreSQL SERIALIZABLE transaction that reads a row before a concurrent transaction commits a conflicting update should be rejected with SQLSTATE 40001 when it later attempts its stale update. Retrying the work in a fresh transaction should observe current state and commit.",
        "limitations": [
            "This reproduces one PostgreSQL concurrent-update serialization failure on one Docker host.",
            "It does not reproduce every SSI dangerous-structure pattern or define a universal retry policy.",
            "The synthetic retry is safe here because the operation is controlled; production retry safety depends on transaction boundaries and side effects.",
        ],
    }
    print(json.dumps(evidence, sort_keys=True))
    if result != "supports":
        sys.exit(1)


if __name__ == "__main__":
    main()
