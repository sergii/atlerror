import json
import os
import platform
import sys
import threading
import time

import psycopg

EXPERIMENT_ID = "experiment.database.isolation_anomaly.write_skew_python_postgres"
CLAIM_ID = "claim.database.isolation_anomaly.repeatable_read_allows_write_skew"
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


def prepare():
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS doctors (id text PRIMARY KEY, on_call boolean NOT NULL)")
        conn.execute("INSERT INTO doctors(id, on_call) VALUES ('A', true), ('B', true) ON CONFLICT (id) DO UPDATE SET on_call = EXCLUDED.on_call")


def on_call_count():
    with connect() as conn:
        return conn.execute("SELECT count(*) FROM doctors WHERE on_call").fetchone()[0]


def transaction_off_call(doctor, isolation, barrier=None):
    started = time.perf_counter()
    conn = connect()
    read_count = None
    try:
        conn.execute(f"BEGIN ISOLATION LEVEL {isolation}")
        read_count = conn.execute("SELECT count(*) FROM doctors WHERE on_call").fetchone()[0]
        if barrier is not None:
            barrier.wait(timeout=3)
        if read_count > 1:
            conn.execute("UPDATE doctors SET on_call = false WHERE id = %s", (doctor,))
        conn.commit()
        return {
            "worker": doctor,
            "outcome": "committed",
            "sqlstate": None,
            "read_on_call": read_count,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }
    except psycopg.Error as exc:
        conn.rollback()
        outcome = "serialization_aborted" if exc.sqlstate == "40001" else "error"
        return {
            "worker": doctor,
            "outcome": outcome,
            "sqlstate": exc.sqlstate,
            "read_on_call": read_count,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }
    except threading.BrokenBarrierError:
        conn.rollback()
        return {
            "worker": doctor,
            "outcome": "barrier_error",
            "sqlstate": None,
            "read_on_call": read_count,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }
    finally:
        conn.close()


def run_concurrent(isolation):
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def worker(doctor):
        result = transaction_off_call(doctor, isolation, barrier)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker, args=(doctor,)) for doctor in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError("concurrent transaction threads did not finish")
    return sorted(results, key=lambda item: item["worker"])


def baseline_phase():
    prepare()
    first = transaction_off_call("A", "REPEATABLE READ")
    second = transaction_off_call("B", "REPEATABLE READ")
    return {"isolation": "REPEATABLE READ", "transactions": [first, second], "final_on_call": on_call_count()}


def intervention_phase():
    prepare()
    transactions = run_concurrent("REPEATABLE READ")
    return {"isolation": "REPEATABLE READ", "transactions": transactions, "final_on_call": on_call_count()}


def recovery_phase():
    prepare()
    transactions = run_concurrent("SERIALIZABLE")
    return {"isolation": "SERIALIZABLE", "transactions": transactions, "final_on_call": on_call_count()}


def counts(phase):
    outcomes = [tx["outcome"] for tx in phase["transactions"]]
    sqlstates = [tx["sqlstate"] for tx in phase["transactions"] if tx["sqlstate"]]
    return {
        "committed": outcomes.count("committed"),
        "serialization_aborted": outcomes.count("serialization_aborted"),
        "unexpected": sum(outcome not in {"committed", "serialization_aborted"} for outcome in outcomes),
        "sqlstates": sqlstates,
    }


def main():
    baseline = baseline_phase()
    intervention = intervention_phase()
    recovery = recovery_phase()
    baseline_counts = counts(baseline)
    intervention_counts = counts(intervention)
    recovery_counts = counts(recovery)

    assertions = {
        "baseline_transactions_commit": baseline_counts["committed"] == 2,
        "baseline_preserves_invariant": baseline["final_on_call"] == 1,
        "repeatable_read_transactions_share_two_on_call_snapshot": all(tx["read_on_call"] == 2 for tx in intervention["transactions"]),
        "repeatable_read_both_transactions_commit": intervention_counts["committed"] == 2,
        "repeatable_read_write_skew_violates_invariant": intervention["final_on_call"] == 0,
        "serializable_prevents_invalid_committed_state": recovery["final_on_call"] >= 1,
        "serializable_commits_one_transaction": recovery_counts["committed"] == 1,
        "serializable_aborts_one_transaction": recovery_counts["serialization_aborted"] == 1,
        "serializable_abort_is_40001": recovery_counts["sqlstates"] == ["40001"],
        "no_unexpected_errors": baseline_counts["unexpected"] == 0 and intervention_counts["unexpected"] == 0 and recovery_counts["unexpected"] == 0,
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
            "action": "run_concurrent_cross_row_decisions_from_shared_snapshot",
            "baseline": "sequential_repeatable_read",
            "intervention": "concurrent_repeatable_read",
            "recovery": "same_concurrent_schedule_under_serializable",
            "business_invariant": "at_least_one_doctor_remains_on_call",
        },
        "observations": {
            "baseline": {**baseline, **baseline_counts, "invariant_violated": baseline["final_on_call"] < 1},
            "intervention": {**intervention, **intervention_counts, "invariant_violated": intervention["final_on_call"] < 1},
            "recovery": {**recovery, **recovery_counts, "invariant_violated": recovery["final_on_call"] < 1},
        },
        "assertions": assertions,
        "result": result,
        "interpretation": "Two PostgreSQL REPEATABLE READ transactions can each observe two doctors on call, update different rows, and both commit, leaving zero doctors on call. Repeating the same concurrent decision pattern under SERIALIZABLE should abort one transaction with SQLSTATE 40001 and preserve the invariant.",
        "limitations": [
            "This reproduces one classic two-row write-skew anomaly on PostgreSQL 17 in Docker.",
            "It does not imply every REPEATABLE READ workload is unsafe or enumerate all isolation anomalies.",
            "The SERIALIZABLE phase demonstrates prevention of this invalid history, not a complete application retry policy.",
        ],
    }
    print(json.dumps(evidence, sort_keys=True))
    if result != "supports":
        sys.exit(1)


if __name__ == "__main__":
    main()
