import json
import os
import platform
import statistics
import threading
import time
import urllib.request

APP_HOST = os.environ.get("APP_HOST", "app")
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
BASE_URL = f"http://{APP_HOST}:{APP_PORT}"
BASELINE_SAMPLES = int(os.environ.get("BASELINE_SAMPLES", "5"))
INTERVENTION_SAMPLES = int(os.environ.get("INTERVENTION_SAMPLES", "3"))
HOLD_MS = int(os.environ.get("HOLD_MS", "300"))
EXPERIMENT_ID = "experiment.database.connection_pool_exhaustion.python_postgres"
CLAIM_ID = "claim.database.connection_pool_exhaustion.checkout_wait_drives_latency"


def request_json(path, timeout=10):
    started = time.monotonic()
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=timeout) as response:
        payload = json.loads(response.read())
    external_ms = (time.monotonic() - started) * 1000.0
    return payload, external_ms


def work_sample():
    payload, external_ms = request_json("/work")
    return {
        "checkout_wait_ms": float(payload["checkout_wait_ms"]),
        "database_query_latency_ms": float(payload["database_query_latency_ms"]),
        "request_latency_ms": external_ms,
        "server_request_latency_ms": float(payload["request_latency_ms"]),
    }


def summarize(samples):
    return {
        "checkout_wait_p50_ms": statistics.median(s["checkout_wait_ms"] for s in samples),
        "database_query_p50_ms": statistics.median(s["database_query_latency_ms"] for s in samples),
        "request_p50_ms": statistics.median(s["request_latency_ms"] for s in samples),
        "server_request_p50_ms": statistics.median(s["server_request_latency_ms"] for s in samples),
        "samples": len(samples),
    }


def wait_for_holder():
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        status, _ = request_json("/status")
        if status["holder_active"]:
            return status
        time.sleep(0.005)
    raise RuntimeError("holder did not acquire the application pool connection")


def intervention_sample():
    holder_error = []

    def hold_connection():
        try:
            request_json(f"/hold?ms={HOLD_MS}", timeout=10)
        except Exception as exc:
            holder_error.append(repr(exc))

    holder = threading.Thread(target=hold_connection)
    holder.start()
    status = wait_for_holder()
    direct, _ = request_json("/direct-control")
    work = work_sample()
    holder.join(timeout=10)
    if holder.is_alive():
        raise RuntimeError("holder request did not finish")
    if holder_error:
        raise RuntimeError(holder_error[0])
    work.update(
        {
            "pool_utilization": float(status["pool_utilization"]),
            "pool_busy": int(status["pool_busy"]),
            "pool_capacity": int(status["pool_capacity"]),
            "direct_database_query_latency_ms": float(direct["database_query_latency_ms"]),
            "direct_database_total_latency_ms": float(direct["direct_total_latency_ms"]),
        }
    )
    return work


baseline_samples = [work_sample() for _ in range(BASELINE_SAMPLES)]
baseline = summarize(baseline_samples)

intervention_samples = [intervention_sample() for _ in range(INTERVENTION_SAMPLES)]
intervention = summarize(intervention_samples)
intervention.update(
    {
        "pool_utilization_p50": statistics.median(s["pool_utilization"] for s in intervention_samples),
        "direct_database_query_p50_ms": statistics.median(
            s["direct_database_query_latency_ms"] for s in intervention_samples
        ),
        "direct_database_total_p50_ms": statistics.median(
            s["direct_database_total_latency_ms"] for s in intervention_samples
        ),
    }
)

recovery_samples = [work_sample() for _ in range(BASELINE_SAMPLES)]
recovery = summarize(recovery_samples)

wait_delta = intervention["checkout_wait_p50_ms"] - baseline["checkout_wait_p50_ms"]
request_delta = intervention["request_p50_ms"] - baseline["request_p50_ms"]
query_delta = intervention["database_query_p50_ms"] - baseline["database_query_p50_ms"]
explanation_error = abs(request_delta - wait_delta)
recovery_wait_delta = abs(recovery["checkout_wait_p50_ms"] - baseline["checkout_wait_p50_ms"])
recovery_request_delta = abs(recovery["request_p50_ms"] - baseline["request_p50_ms"])

thresholds = {
    "min_pool_wait_delta_ms": 180.0,
    "min_request_delta_ms": 180.0,
    "max_query_delta_ms": 25.0,
    "max_wait_explanation_error_ms": 50.0,
    "min_pool_utilization": 1.0,
    "max_direct_database_total_ms": 150.0,
    "max_recovery_wait_delta_ms": 25.0,
    "max_recovery_request_delta_ms": 50.0,
}

assertions = {
    "pool_checkout_wait_increased": wait_delta >= thresholds["min_pool_wait_delta_ms"],
    "request_latency_increased": request_delta >= thresholds["min_request_delta_ms"],
    "query_latency_stayed_near_baseline": abs(query_delta) <= thresholds["max_query_delta_ms"],
    "checkout_wait_explains_request_delta": explanation_error <= thresholds["max_wait_explanation_error_ms"],
    "application_pool_was_at_capacity": intervention["pool_utilization_p50"] >= thresholds["min_pool_utilization"],
    "database_still_accepts_direct_connections": intervention["direct_database_total_p50_ms"] <= thresholds["max_direct_database_total_ms"],
    "recovery_checkout_wait_returned_to_baseline": recovery_wait_delta <= thresholds["max_recovery_wait_delta_ms"],
    "recovery_request_latency_returned_to_baseline": recovery_request_delta <= thresholds["max_recovery_request_delta_ms"],
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
        "application_pool": "psycopg_pool",
        "application_pool_max_size": 1,
    },
    "intervention": {
        "action": "occupy_all_application_database_pool_slots",
        "holder_behavior": "sleep_in_application_while_holding_connection",
        "hold_ms": HOLD_MS,
        "database_query": "SELECT 1",
    },
    "observations": {
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
        "derived": {
            "pool_wait_delta_ms": wait_delta,
            "request_latency_delta_ms": request_delta,
            "database_query_latency_delta_ms": query_delta,
            "checkout_wait_explanation_error_ms": explanation_error,
            "recovery_pool_wait_delta_ms": recovery_wait_delta,
            "recovery_request_latency_delta_ms": recovery_request_delta,
        },
        "thresholds": thresholds,
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "Occupying the only slot in an application-side psycopg connection pool should make a concurrent HTTP request wait for checkout. The subsequent SELECT 1 should remain fast, while a separate direct PostgreSQL connection should still succeed quickly. Releasing the holder should remove the added wait.",
    "limitations": [
        "The pool has max_size=1 to make checkout contention deterministic; production pools are usually larger.",
        "The holder sleeps in application code while retaining a connection, isolating pool wait from slow-query time.",
        "This validates application pool exhaustion, not PostgreSQL max_connections exhaustion or a universal sizing rule.",
    ],
}
print(json.dumps(evidence))
