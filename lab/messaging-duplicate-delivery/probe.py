import json
import os
import platform
import time

import redis

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
GROUP = "atlerror-consumers"
CONSUMER_ONE = "consumer-1"
CONSUMER_TWO = "consumer-2"

EXPERIMENT_ID = "experiment.messaging.duplicate_delivery.redis_streams_python"
CLAIM_ID = "claim.messaging.duplicate_delivery.unacked_message_is_redelivered"

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
r.ping()


def create_stream(name):
    r.delete(name)
    try:
        r.xgroup_destroy(name, GROUP)
    except redis.ResponseError:
        pass
    r.xgroup_create(name, GROUP, id="0-0", mkstream=True)


def add_message(stream, event_id):
    return r.xadd(stream, {"event_id": event_id, "payload": "1"})


def read_new(stream, consumer):
    rows = r.xreadgroup(GROUP, consumer, {stream: ">"}, count=1, block=1000)
    if not rows:
        return None
    _, messages = rows[0]
    return messages[0]


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


def claim(stream, message_id, consumer):
    claimed = r.xclaim(stream, GROUP, consumer, min_idle_time=0, message_ids=[message_id])
    return claimed[0] if claimed else None


def naive_effect(key):
    return int(r.incr(key))


IDEMPOTENT_EFFECT_SCRIPT = r.register_script(
    """
    if redis.call('SETNX', KEYS[1], ARGV[1]) == 1 then
      redis.call('INCR', KEYS[2])
      return 1
    end
    return 0
    """
)


def idempotent_effect(event_id, effect_key):
    applied = int(
        IDEMPOTENT_EFFECT_SCRIPT(
            keys=[f"processed:{event_id}", effect_key],
            args=["1"],
        )
    )
    return applied


# Baseline: one delivery, side effect once, durable ACK.
baseline_stream = "atlerror:baseline"
baseline_effect_key = "effect:baseline"
r.delete(baseline_effect_key)
create_stream(baseline_stream)
baseline_entry_id = add_message(baseline_stream, "baseline-event")
baseline_delivery = read_new(baseline_stream, CONSUMER_ONE)
baseline_effect_count = naive_effect(baseline_effect_key)
baseline_ack_count = r.xack(baseline_stream, GROUP, baseline_entry_id)
baseline_pending = pending_snapshot(baseline_stream)

# Intervention: apply effect, omit ACK, then another consumer claims the same entry and applies effect again.
intervention_stream = "atlerror:intervention"
intervention_effect_key = "effect:intervention"
r.delete(intervention_effect_key)
create_stream(intervention_stream)
intervention_entry_id = add_message(intervention_stream, "intervention-event")
intervention_first_delivery = read_new(intervention_stream, CONSUMER_ONE)
intervention_effect_after_first = naive_effect(intervention_effect_key)
intervention_pending_before_claim = pending_snapshot(intervention_stream)
intervention_second_delivery = claim(intervention_stream, intervention_entry_id, CONSUMER_TWO)
intervention_pending_after_claim = pending_snapshot(intervention_stream)
intervention_effect_after_second = naive_effect(intervention_effect_key)
intervention_ack_count = r.xack(intervention_stream, GROUP, intervention_entry_id)
intervention_pending_after_ack = pending_snapshot(intervention_stream)

# Recovery: keep the same redelivery pattern, but make the business effect idempotent.
recovery_stream = "atlerror:recovery"
recovery_effect_key = "effect:recovery"
r.delete(recovery_effect_key, "processed:recovery-event")
create_stream(recovery_stream)
recovery_entry_id = add_message(recovery_stream, "recovery-event")
recovery_first_delivery = read_new(recovery_stream, CONSUMER_ONE)
recovery_first_applied = idempotent_effect("recovery-event", recovery_effect_key)
recovery_pending_before_claim = pending_snapshot(recovery_stream)
recovery_second_delivery = claim(recovery_stream, recovery_entry_id, CONSUMER_TWO)
recovery_pending_after_claim = pending_snapshot(recovery_stream)
recovery_second_applied = idempotent_effect("recovery-event", recovery_effect_key)
recovery_effect_count = int(r.get(recovery_effect_key) or 0)
recovery_ack_count = r.xack(recovery_stream, GROUP, recovery_entry_id)
recovery_pending_after_ack = pending_snapshot(recovery_stream)

baseline_same_identity = baseline_delivery is not None and baseline_delivery[0] == baseline_entry_id
intervention_same_identity = (
    intervention_first_delivery is not None
    and intervention_second_delivery is not None
    and intervention_first_delivery[0] == intervention_entry_id
    and intervention_second_delivery[0] == intervention_entry_id
)
recovery_same_identity = (
    recovery_first_delivery is not None
    and recovery_second_delivery is not None
    and recovery_first_delivery[0] == recovery_entry_id
    and recovery_second_delivery[0] == recovery_entry_id
)

intervention_delivery_count = 0
if intervention_pending_after_claim["entries"]:
    intervention_delivery_count = intervention_pending_after_claim["entries"][0].get("times_delivered") or 0

recovery_delivery_count = 0
if recovery_pending_after_claim["entries"]:
    recovery_delivery_count = recovery_pending_after_claim["entries"][0].get("times_delivered") or 0

assertions = {
    "baseline_message_delivered_once": baseline_same_identity,
    "baseline_effect_applied_once": baseline_effect_count == 1,
    "baseline_acknowledged": baseline_ack_count == 1 and baseline_pending["count"] == 0,
    "intervention_first_delivery_applies_effect": intervention_effect_after_first == 1,
    "intervention_missing_ack_leaves_message_pending": intervention_pending_before_claim["count"] == 1,
    "intervention_same_message_is_redelivered": intervention_same_identity,
    "intervention_broker_records_multiple_deliveries": intervention_delivery_count >= 2,
    "intervention_naive_handler_duplicates_effect": intervention_effect_after_second == 2,
    "intervention_ack_clears_pending": intervention_ack_count == 1 and intervention_pending_after_ack["count"] == 0,
    "recovery_first_delivery_applies_effect": recovery_first_applied == 1,
    "recovery_missing_ack_still_allows_redelivery": recovery_same_identity and recovery_delivery_count >= 2,
    "recovery_idempotency_suppresses_second_effect": recovery_second_applied == 0,
    "recovery_effect_applied_exactly_once": recovery_effect_count == 1,
    "recovery_ack_clears_pending": recovery_ack_count == 1 and recovery_pending_after_ack["count"] == 0,
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
        "action": "apply_side_effect_then_omit_ack_and_claim_pending_message_from_second_consumer",
        "delivery_semantics": "at_least_once_redelivery_of_unacknowledged_entry",
        "recovery": "repeat_redelivery_with_atomic_idempotency_guard_before_business_effect",
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
            "stream_entry_id": intervention_entry_id,
            "first_delivery": intervention_first_delivery,
            "second_delivery": intervention_second_delivery,
            "same_message_identity": intervention_same_identity,
            "pending_before_claim": intervention_pending_before_claim,
            "pending_after_claim": intervention_pending_after_claim,
            "effect_count_after_first_delivery": intervention_effect_after_first,
            "effect_count_after_second_delivery": intervention_effect_after_second,
            "ack_count": intervention_ack_count,
            "pending_after_ack": intervention_pending_after_ack,
        },
        "recovery": {
            "stream_entry_id": recovery_entry_id,
            "first_delivery": recovery_first_delivery,
            "second_delivery": recovery_second_delivery,
            "same_message_identity": recovery_same_identity,
            "pending_before_claim": recovery_pending_before_claim,
            "pending_after_claim": recovery_pending_after_claim,
            "first_effect_applied": recovery_first_applied,
            "second_effect_applied": recovery_second_applied,
            "effect_count": recovery_effect_count,
            "ack_count": recovery_ack_count,
            "pending_after_ack": recovery_pending_after_ack,
        },
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "Redis Streams redelivers the same pending stream entry when processing is not acknowledged. The naive handler applies the business effect twice for two deliveries of one stable message identity. Repeating the same redelivery pattern with an atomic idempotency guard preserves two delivery attempts while reducing the business effect to exactly one application.",
    "limitations": [
        "The experiment uses Redis Streams consumer groups in Docker and does not claim identical broker internals across Kafka, NATS JetStream, SQS, RabbitMQ, or other systems.",
        "The crash window is represented deterministically by omitting XACK after the first side effect rather than terminating a worker process nondeterministically.",
        "The recovery idempotency guard and synthetic side effect are colocated in Redis; production systems may need transactional inbox/outbox patterns or provider-specific idempotency contracts when the side effect lives elsewhere.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
