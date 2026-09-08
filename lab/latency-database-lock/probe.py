import http.client
import json
import os
import platform
import statistics
import time

APP_HOST = os.environ.get("APP_HOST", "app")
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
SAMPLES = int(os.environ.get("SAMPLES", "6"))
EXPERIMENT_ID = "experiment.latency.database.lock_wait_python_postgres"
CLAIM_ID = "claim.database.lock_contention.row_lock_wait_propagates_upstream"


def one_request(hold_ms: int):
    started = time.monotonic()
    connection = http.client.HTTPConnection(APP_HOST, APP_PORT, timeout=10)
    connection.request("GET", f"/work?hold_ms={hold_ms}")
    response = connection.getresponse()
    body = response.read()
    connection.close()
    elapsed_ms = (time.monotonic() - started) * 1000.0
    if response.status != 200:
        raise RuntimeError(f"app returned {response.status}: {body.decode(errors='replace')}")
    payload = json.loads(body)
    return elapsed_ms, float(payload["database_lock_wait_ms"]), bool(payload["lock_wait_event_observed"])


def phase(hold_ms: int):
    request_latencies = []
    lock_wait_latencies = []
    observed_events = 0
    for _ in range(SAMPLES):
        request_ms, lock_wait_ms, lock_event = one_request(hold_ms)
        request_latencies.append(request_ms)
        lock_wait_latencies.append(lock_wait_ms)
        observed_events += int(lock_event)
    return {
        "configured_lock_hold_ms": hold_ms,
        "database_lock_wait_p50_ms": statistics.median(lock_wait_latencies),
        "request_p50_ms": statistics.median(request_latencies),
        "lock_wait_event_observed_samples": observed_events,
        "samples": SAMPLES,
    }


baseline = phase(0)
intervention = phase(300)
recovery = phase(0)

lock_delta = intervention["database_lock_wait_p50_ms"] - baseline["database_lock_wait_p50_ms"]
request_delta = intervention["request_p50_ms"] - baseline["request_p50_ms"]
propagation_error = abs(request_delta - lock_delta)
recovery_request_delta = abs(recovery["request_p50_ms"] - baseline["request_p50_ms"])

thresholds = {
    "min_lock_wait_delta_ms": 180.0,
    "min_request_delta_ms": 180.0,
    "max_propagation_error_ms": 60.0,
    "min_lock_event_samples": max(1, SAMPLES // 2),
    "max_recovery_request_delta_ms": 50.0,
}
assertions = {
    "lock_wait_increased": lock_delta >= thresholds["min_lock_wait_delta_ms"],
    "upstream_latency_increased": request_delta >= thresholds["min_request_delta_ms"],
    "lock_wait_explains_upstream_delta": propagation_error <= thresholds["max_propagation_error_ms"],
    "postgres_reported_lock_wait": intervention["lock_wait_event_observed_samples"] >= thresholds["min_lock_event_samples"],
    "baseline_has_no_lock_wait_event": baseline["lock_wait_event_observed_samples"] == 0,
    "recovery_returned_toward_baseline": recovery_request_delta <= thresholds["max_recovery_request_delta_ms"],
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
        "action": "hold_postgresql_row_lock_while_conflicting_update_runs",
        "mechanism": "SELECT FOR UPDATE plus conflicting UPDATE",
        "baseline_lock_hold_ms": 0,
        "intervention_lock_hold_ms": 300,
        "recovery_lock_hold_ms": 0,
    },
    "observations": {
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
        "derived": {
            "database_lock_wait_delta_ms": lock_delta,
            "request_latency_delta_ms": request_delta,
            "propagation_error_ms": propagation_error,
            "recovery_request_delta_ms": recovery_request_delta,
        },
        "thresholds": thresholds,
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "A PostgreSQL transaction that holds a row lock should cause a conflicting synchronous update to enter a database-reported Lock wait, add material wait time, and propagate a similar delay to the upstream request; removing the blocker should restore latency toward baseline.",
    "limitations": [
        "This reproduces one row-lock contention pattern using PostgreSQL 17 and two synthetic sessions.",
        "Wait-event sampling can miss very short waits; the intervention is intentionally long enough to make the state observable.",
        "The measured waiter duration includes connection and psql process overhead, which is present in baseline and recovery as well.",
        "This is not a deadlock experiment and does not validate every PostgreSQL lock type.",
    ],
}
print(json.dumps(evidence))
