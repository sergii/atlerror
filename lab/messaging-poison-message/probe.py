import json
import os
import platform

import redis

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
GROUP = "atlerror-consumers"
CONSUMERS = ["consumer-1", "consumer-2", "consumer-3"]
RETRY_THRESHOLD = 3

EXPERIMENT_ID = "experiment.messaging.poison_message.redis_streams_python"
CLAIM_ID = "claim.messaging.poison_message.repeated_failure_reaches_dead_letter_policy"

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
r.ping()


def create_stream(name):
    r.delete(name)
    try:
        r.xgroup_destroy(name, GROUP)
    except redis.ResponseError:
        pass
    r.xgroup_create(name, GROUP, id="0-0", mkstream=True)


def add_message(stream, event_id, kind="credit", amount="1"):
    return r.xadd(stream, {"event_id": event_id, "kind": kind, "amount": amount})


def read_new(stream, consumer):
    rows = r.xreadgroup(GROUP, consumer, {stream: ">"}, count=1, block=1000)
    if not rows:
        return None
    _, messages = rows[0]
    return messages[0]


def claim(stream, message_id, consumer):
    claimed = r.xclaim(stream, GROUP, consumer, min_idle_time=0, message_ids=[message_id])
    return claimed[0] if claimed else None


def pending_snapshot(stream):
    summary = r.xpending(stream, GROUP)
    entries = r.xpending_range(stream, GROUP, "-", "+", 10)
    return {
        "count": summary.get("pending", 0),
        "entries": [
            {
                "message_id": entry.get("message_id"),
                "consumer": entry.get("consumer"),
                "times_delivered": entry.get("times_delivered"),
                "time_since_delivered_ms": entry.get("time_since_delivered"),
            }
            for entry in entries
        ],
    }


def process_message(delivery, effect_key):
    if delivery is None:
        raise RuntimeError("missing delivery")
    _, fields = delivery
    if fields.get("kind") != "credit":
        raise ValueError(f"unsupported message kind: {fields.get('kind')}")
    amount = int(fields.get("amount", "0"))
    return int(r.incrby(effect_key, amount))


def capture_failure(delivery, effect_key):
    try:
        process_message(delivery, effect_key)
    except Exception as exc:
        return {"error_class": type(exc).__name__, "error_message": str(exc)}
    return {"error_class": None, "error_message": None}


def delivery_count(snapshot):
    if not snapshot["entries"]:
        return 0
    return int(snapshot["entries"][0].get("times_delivered") or 0)


# Baseline: a valid message produces one durable effect and is acknowledged.
baseline_stream = "atlerror:poison:baseline"
baseline_effect_key = "effect:poison:baseline"
r.delete(baseline_effect_key)
create_stream(baseline_stream)
baseline_entry_id = add_message(baseline_stream, "baseline-event")
baseline_delivery = read_new(baseline_stream, CONSUMERS[0])
baseline_effect_count = process_message(baseline_delivery, baseline_effect_key)
baseline_ack_count = r.xack(baseline_stream, GROUP, baseline_entry_id)
baseline_pending = pending_snapshot(baseline_stream)

# Intervention: one deterministic poison payload fails on three deliveries of the same broker entry.
stream = "atlerror:poison:active"
dlq_stream = "atlerror:poison:dlq"
poison_effect_key = "effect:poison:intervention"
recovery_effect_key = "effect:poison:recovery"
r.delete(dlq_stream, poison_effect_key, recovery_effect_key)
create_stream(stream)

poison_entry_id = add_message(stream, "poison-event", kind="unsupported", amount="1")
first_delivery = read_new(stream, CONSUMERS[0])
first_failure = capture_failure(first_delivery, poison_effect_key)
pending_after_first = pending_snapshot(stream)

second_delivery = claim(stream, poison_entry_id, CONSUMERS[1])
second_failure = capture_failure(second_delivery, poison_effect_key)
pending_after_second = pending_snapshot(stream)

third_delivery = claim(stream, poison_entry_id, CONSUMERS[2])
third_failure = capture_failure(third_delivery, poison_effect_key)
pending_after_third = pending_snapshot(stream)

poison_effect_count = int(r.get(poison_effect_key) or 0)
failures = [first_failure, second_failure, third_failure]
all_same_failure = len({(f["error_class"], f["error_message"]) for f in failures}) == 1
same_poison_identity = all(
    delivery is not None and delivery[0] == poison_entry_id
    for delivery in [first_delivery, second_delivery, third_delivery]
)

# Containment after retry threshold: preserve failure context in DLQ, then ACK the active entry.
last_failure = third_failure
dlq_entry_id = r.xadd(
    dlq_stream,
    {
        "original_stream": stream,
        "original_entry_id": poison_entry_id,
        "event_id": "poison-event",
        "error_class": last_failure["error_class"],
        "error_message": last_failure["error_message"],
        "delivery_attempts": str(delivery_count(pending_after_third)),
    },
)
poison_ack_count = r.xack(stream, GROUP, poison_entry_id)
pending_after_dead_letter = pending_snapshot(stream)
claim_after_ack = r.xclaim(stream, GROUP, "consumer-after-dlq", min_idle_time=0, message_ids=[poison_entry_id])
new_after_ack = read_new(stream, "consumer-after-dlq")
dlq_rows = r.xrange(dlq_stream, "-", "+")
original_row = r.xrange(stream, min=poison_entry_id, max=poison_entry_id)

# Recovery/continuity: a subsequent valid message on the same active stream still processes normally.
recovery_entry_id = add_message(stream, "recovery-event", kind="credit", amount="1")
recovery_delivery = read_new(stream, CONSUMERS[0])
recovery_effect_count = process_message(recovery_delivery, recovery_effect_key)
recovery_ack_count = r.xack(stream, GROUP, recovery_entry_id)
recovery_pending = pending_snapshot(stream)

assertions = {
    "baseline_valid_message_processes": baseline_delivery is not None and baseline_delivery[0] == baseline_entry_id and baseline_effect_count == 1,
    "baseline_ack_clears_pending": baseline_ack_count == 1 and baseline_pending["count"] == 0,
    "poison_same_message_identity_retried": same_poison_identity,
    "poison_failure_is_deterministic": all_same_failure and first_failure["error_class"] == "ValueError",
    "poison_first_attempt_recorded": delivery_count(pending_after_first) == 1,
    "poison_second_attempt_recorded": delivery_count(pending_after_second) == 2,
    "poison_reaches_retry_threshold": delivery_count(pending_after_third) == RETRY_THRESHOLD,
    "poison_produces_no_business_effect": poison_effect_count == 0,
    "dead_letter_preserves_identity_and_failure": len(dlq_rows) == 1 and dlq_rows[0][0] == dlq_entry_id and dlq_rows[0][1].get("original_entry_id") == poison_entry_id and dlq_rows[0][1].get("event_id") == "poison-event" and dlq_rows[0][1].get("error_class") == "ValueError" and int(dlq_rows[0][1].get("delivery_attempts", "0")) == RETRY_THRESHOLD,
    "dead_letter_ack_removes_active_pending_state": poison_ack_count == 1 and pending_after_dead_letter["count"] == 0,
    "dead_lettered_message_is_not_claimable_again": claim_after_ack == [],
    "dead_lettered_message_is_not_new_group_work": new_after_ack is None,
    "dead_letter_does_not_require_payload_deletion": len(original_row) == 1 and original_row[0][0] == poison_entry_id,
    "subsequent_valid_message_processes": recovery_delivery is not None and recovery_delivery[0] == recovery_entry_id and recovery_effect_count == 1,
    "subsequent_valid_message_acknowledged": recovery_ack_count == 1 and recovery_pending["count"] == 0,
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
        "broker": "redis_streams",
        "broker_version": r.info("server").get("redis_version"),
    },
    "intervention": {
        "action": "publish_deterministically_invalid_message_retry_same_pending_entry_three_times_then_dead_letter_and_ack",
        "retry_threshold": RETRY_THRESHOLD,
        "failure_mode": "unsupported_message_kind",
        "containment": "consumer_managed_dead_letter_stream_then_xack_original",
    },
    "observations": {
        "baseline": {
            "stream_entry_id": baseline_entry_id,
            "delivery": baseline_delivery,
            "effect_count": baseline_effect_count,
            "ack_count": baseline_ack_count,
            "pending_after_ack": baseline_pending,
        },
        "intervention": {
            "stream_entry_id": poison_entry_id,
            "deliveries": [first_delivery, second_delivery, third_delivery],
            "same_message_identity": same_poison_identity,
            "failures": failures,
            "same_failure_each_attempt": all_same_failure,
            "pending_after_first": pending_after_first,
            "pending_after_second": pending_after_second,
            "pending_after_third": pending_after_third,
            "effect_count": poison_effect_count,
        },
        "dead_letter": {
            "dlq_stream": dlq_stream,
            "dlq_entry_id": dlq_entry_id,
            "dlq_rows": dlq_rows,
            "original_ack_count": poison_ack_count,
            "pending_after_dead_letter": pending_after_dead_letter,
            "claim_after_ack": claim_after_ack,
            "new_group_delivery_after_ack": new_after_ack,
            "original_payload_still_in_stream": original_row,
        },
        "recovery": {
            "stream_entry_id": recovery_entry_id,
            "delivery": recovery_delivery,
            "effect_count": recovery_effect_count,
            "ack_count": recovery_ack_count,
            "pending_after_ack": recovery_pending,
        },
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "A single Redis Streams entry with deterministic invalid content failed with the same application error across three deliveries while producing no business effect. After the retry threshold, the consumer preserved the message identity and failure context in a dead-letter stream and acknowledged the original, removing it from the active pending/redelivery path. A subsequent valid message on the same stream then processed normally.",
    "limitations": [
        "Redis Streams has no native DLQ policy in this lab; dead-letter transfer and the retry threshold are implemented by the synthetic consumer.",
        "The poison condition is deterministic invalid input and does not represent transient downstream failures, timeouts, or nondeterministic bugs.",
        "The original stream entry remains physically stored after XACK; the experiment demonstrates removal from active consumer-group redelivery, not payload deletion.",
        "Production dead-letter transfer and acknowledgement may require stronger atomicity guarantees than the sequential XADD-to-DLQ then XACK used here.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
