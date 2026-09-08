import json
import os
import platform
import threading
import time

import psycopg
from psycopg import errors

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_USER = os.environ.get("DB_USER", "atlerror")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "atlerror")
DB_NAME = os.environ.get("DB_NAME", "atlerror")
EXPERIMENT_ID = "experiment.database.deadlock.python_postgres"
CLAIM_ID = "claim.database.deadlock.cycle_aborts_transaction"


def connect(autocommit=False):
    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME,
        autocommit=autocommit,
    )


def setup_database():
    with connect(autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS deadlock_items")
        conn.execute("CREATE TABLE deadlock_items (id integer PRIMARY KEY, value integer NOT NULL)")
        conn.execute("INSERT INTO deadlock_items(id, value) VALUES (1, 0), (2, 0)")


def reset_values():
    with connect(autocommit=True) as conn:
        conn.execute("UPDATE deadlock_items SET value = 0")


def deadlock_count():
    with connect(autocommit=True) as conn:
        row = conn.execute(
            "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
        ).fetchone()
        return int(row[0])


def run_pair(order_a, order_b, force_cycle=False):
    start_barrier = threading.Barrier(2)
    first_lock_barrier = threading.Barrier(2) if force_cycle else None
    outcomes = []
    outcomes_lock = threading.Lock()

    def worker(name, order):
        conn = connect()
        started = None
        outcome = None
        try:
            conn.execute("SET deadlock_timeout = '100ms'")
            start_barrier.wait(timeout=5)
            started = time.monotonic()
            conn.execute(
                "UPDATE deadlock_items SET value = value + 1 WHERE id = %s",
                (order[0],),
            )
            if first_lock_barrier is not None:
                first_lock_barrier.wait(timeout=5)
            conn.execute(
                "UPDATE deadlock_items SET value = value + 1 WHERE id = %s",
                (order[1],),
            )
            conn.commit()
            outcome = {"worker": name, "outcome": "committed", "sqlstate": None}
        except errors.DeadlockDetected as error:
            conn.rollback()
            outcome = {
                "worker": name,
                "outcome": "deadlock_aborted",
                "sqlstate": error.sqlstate,
            }
        except Exception as error:
            conn.rollback()
            outcome = {
                "worker": name,
                "outcome": "unexpected_error",
                "sqlstate": getattr(error, "sqlstate", None),
                "error": type(error).__name__,
            }
        finally:
            outcome["elapsed_ms"] = (
                (time.monotonic() - started) * 1000.0 if started is not None else None
            )
            with outcomes_lock:
                outcomes.append(outcome)
            conn.close()

    threads = [
        threading.Thread(target=worker, args=("A", order_a), daemon=True),
        threading.Thread(target=worker, args=("B", order_b), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError("transaction worker did not finish")

    return {
        "committed": sum(1 for item in outcomes if item["outcome"] == "committed"),
        "deadlock_aborted": sum(
            1 for item in outcomes if item["outcome"] == "deadlock_aborted"
        ),
        "unexpected_errors": sum(
            1 for item in outcomes if item["outcome"] == "unexpected_error"
        ),
        "sqlstates": sorted(
            item["sqlstate"] for item in outcomes if item.get("sqlstate")
        ),
        "transactions": sorted(outcomes, key=lambda item: item["worker"]),
    }


setup_database()
initial_deadlocks = deadlock_count()

baseline = run_pair((1, 2), (1, 2), force_cycle=False)
after_baseline_deadlocks = deadlock_count()

reset_values()
intervention = run_pair((1, 2), (2, 1), force_cycle=True)
time.sleep(0.05)
after_intervention_deadlocks = deadlock_count()

reset_values()
recovery = run_pair((1, 2), (1, 2), force_cycle=False)
after_recovery_deadlocks = deadlock_count()

deadlocks_from_baseline = after_baseline_deadlocks - initial_deadlocks
deadlocks_from_intervention = after_intervention_deadlocks - after_baseline_deadlocks
deadlocks_from_recovery = after_recovery_deadlocks - after_intervention_deadlocks

assertions = {
    "baseline_both_transactions_commit": baseline["committed"] == 2
    and baseline["deadlock_aborted"] == 0,
    "cyclic_ordering_produces_deadlock_victim": intervention["deadlock_aborted"] == 1
    and intervention["committed"] == 1,
    "postgres_returns_sqlstate_40P01": "40P01" in intervention["sqlstates"],
    "postgres_server_deadlock_counter_increased": deadlocks_from_intervention >= 1,
    "baseline_did_not_increment_deadlock_counter": deadlocks_from_baseline == 0,
    "consistent_order_recovery_commits_both": recovery["committed"] == 2
    and recovery["deadlock_aborted"] == 0,
    "recovery_did_not_add_deadlock": deadlocks_from_recovery == 0,
    "no_unexpected_errors": baseline["unexpected_errors"] == 0
    and intervention["unexpected_errors"] == 0
    and recovery["unexpected_errors"] == 0,
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
    },
    "intervention": {
        "action": "create_cyclic_postgresql_row_lock_dependency",
        "mechanism": "two transactions acquire rows in opposing order",
        "deadlock_timeout_ms": 100,
        "recovery": "repeat concurrent transactions with consistent lock ordering",
    },
    "observations": {
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
        "server_deadlock_counter": {
            "initial": initial_deadlocks,
            "after_baseline": after_baseline_deadlocks,
            "after_intervention": after_intervention_deadlocks,
            "after_recovery": after_recovery_deadlocks,
            "baseline_delta": deadlocks_from_baseline,
            "intervention_delta": deadlocks_from_intervention,
            "recovery_delta": deadlocks_from_recovery,
        },
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "Opposing row-lock acquisition creates a PostgreSQL wait-for cycle. The server should detect the deadlock, abort one participant with SQLSTATE 40P01, increment its deadlock counter, and allow the peer to commit. Repeating the concurrent work with a consistent lock order should avoid the cycle and let both transactions commit.",
    "limitations": [
        "This reproduces a two-transaction, two-row PostgreSQL 17 deadlock on one Docker host.",
        "The synthetic sessions lower deadlock_timeout to 100 ms to keep CI fast; production values can differ.",
        "The experiment validates cycle detection and victim abort, not every lock mode, distributed transaction, or retry policy.",
    ],
}

print(json.dumps(evidence))
