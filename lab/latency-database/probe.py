import http.client
import json
import os
import platform
import statistics
import time

APP_HOST = os.environ.get("APP_HOST", "app")
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
SAMPLES = int(os.environ.get("SAMPLES", "12"))
EXPERIMENT_ID = "experiment.latency.database.query_delay_python_postgres"
CLAIM_ID = "claim.latency.database.query_delay_propagates_upstream"


def one_request(delay_ms: int):
    started = time.monotonic()
    connection = http.client.HTTPConnection(APP_HOST, APP_PORT, timeout=10)
    connection.request("GET", f"/work?delay_ms={delay_ms}")
    response = connection.getresponse()
    body = response.read()
    connection.close()
    elapsed_ms = (time.monotonic() - started) * 1000.0
    if response.status != 200:
        raise RuntimeError(f"app returned {response.status}: {body.decode(errors='replace')}")
    payload = json.loads(body)
    return elapsed_ms, float(payload["database_query_latency_ms"])


def phase(delay_ms: int):
    request_latencies = []
    database_latencies = []
    for _ in range(SAMPLES):
        request_ms, db_ms = one_request(delay_ms)
        request_latencies.append(request_ms)
        database_latencies.append(db_ms)
    return {
        "configured_database_delay_ms": delay_ms,
        "database_query_p50_ms": statistics.median(database_latencies),
        "request_p50_ms": statistics.median(request_latencies),
        "samples": SAMPLES,
    }


baseline = phase(0)
intervention = phase(120)
recovery = phase(0)

db_delta = intervention["database_query_p50_ms"] - baseline["database_query_p50_ms"]
request_delta = intervention["request_p50_ms"] - baseline["request_p50_ms"]
propagation_error = abs(request_delta - db_delta)
recovery_request_delta = abs(recovery["request_p50_ms"] - baseline["request_p50_ms"])

thresholds = {
    "min_database_delta_ms": 80.0,
    "min_request_delta_ms": 80.0,
    "max_propagation_error_ms": 35.0,
    "max_recovery_request_delta_ms": 35.0,
}
assertions = {
    "database_latency_increased": db_delta >= thresholds["min_database_delta_ms"],
    "upstream_latency_increased": request_delta >= thresholds["min_request_delta_ms"],
    "database_delay_explains_upstream_delta": propagation_error <= thresholds["max_propagation_error_ms"],
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
        "action": "increase_postgresql_query_delay",
        "mechanism": "pg_sleep",
        "baseline_delay_ms": 0,
        "intervention_delay_ms": 120,
        "recovery_delay_ms": 0,
    },
    "observations": {
        "baseline": baseline,
        "intervention": intervention,
        "recovery": recovery,
        "derived": {
            "database_latency_delta_ms": db_delta,
            "request_latency_delta_ms": request_delta,
            "propagation_error_ms": propagation_error,
            "recovery_request_delta_ms": recovery_request_delta,
        },
        "thresholds": thresholds,
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "A controlled delay added only to a synchronous PostgreSQL query should appear in both measured database-call latency and upstream end-to-end request latency, then disappear when the query delay is removed.",
    "limitations": [
        "The database and application are synthetic services on one Docker host.",
        "pg_sleep validates database-side query delay propagation but not execution-plan, lock, storage, or network causes.",
        "The app launches psql per request, adding connection and process startup overhead to every phase.",
    ],
}
print(json.dumps(evidence))
