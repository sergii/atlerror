import json
import os
import platform

import redis

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
GROUP = "atlerror-consumers"
CONSUMER_ONE = "consumer-1"
CONSUMER_TWO = "consumer-2"

EXPERIMENT_ID = "experiment.messaging.acknowledged_before_durable_effect.redis_streams_python"
CLAIM_ID = "claim.messaging.acknowledged_before_durable_effect.ack_suppresses_redelivery_before_effect"

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


def read_new(stream, consumer, block=1000):
    rows = r.xreadgroup(GROUP, consumer, {stream: ">"}, count=1, block=block)
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
    return int(
        IDEMPOTENT_EFFECT_SCRIPT(
            keys=[f"processed:{event_id}", effect_key],
            args=["1"],
        )
    )


# Baseline: make the business effect durable, then acknowledge the delivery.
baseline_stream = "atlerror:q22:baseline"
baseline_effect_key = "effect:q22:baseline"
r.delete(baseline_effect_key, "processed:q22-baseline-event")
create_stream(baseline_stream)
baseline_entry_id = add_message(baseline_stream, "q22-baseline-event")
baseline_delivery = read_new(baseline_stream, CONSUMER_ONE)
baseline_effect_applied = idempotent_effect("q22-baseline-event", baseline_effect_key)
baseline_ack_count = r.xack(baseline_stream, GROUP, baseline_entry_id)
baseline_pending = pending_snapshot(baseline_stream)
baseline_effect_count = int(r.get(baseline_effect_key) or 0)

# Intervention: acknowledge first, then represent a crash/omission before the effect becomes durable.
intervention_stream = "atlerror:q22:intervention"
intervention_effect_key = "effect:q22:intervention"
r.delete(intervention_effect_key, "processed:q22-intervention-event")
create_stream(intervention_stream)
intervention_entry_id = add_message(intervention_stream, "q22-intervention-event")
intervention_delivery = read_new(intervention_stream, CONSUMER_ONE)
intervention_pending_before_ack = pending_snapshot(intervention_stream)
intervention_ack_count = r.xack(intervention_stream, GROUP, intervention_entry_id)
intervention_pending_after_ack = pending_snapshot(intervention_stream)
# The business effect is intentionally omitted after ACK.
intervention_effect_count = int(r.get(intervention_effect_key) or 0)
intervention_claim_after_ack = claim(intervention_stream, intervention_entry_id, CONSUMER_TWO)
intervention_new_read_after_ack = read_new(intervention_stream, CONSUMER_TWO, block=100)
intervention_stream_entry = r.xrange(intervention_stream, min=intervention_entry_id, max=intervention_entry_id)

# Recovery: make an idempotent effect durable before ACK. Simulate a crash before ACK,
# accept redelivery, suppress the duplicate effect, then acknowledge.
recovery_stream = "atlerror:q22:recovery"
recovery_effect_key = "effect:q22:recovery"
r.delete(recovery_effect_key, "processed:q22-recovery-event")
create_stream(recovery_stream)
recovery_entry_id = add_message(recovery_stream, "q22-recovery-event")
recovery_first_delivery = read_new(recovery_stream, CONSUMER_ONE)
recovery_first_effect_applied = idempotent_effect("q22-recovery-event", recovery_effect_key)
recovery_pending_before_claim = pending_snapshot(recovery_stream)
recovery_second_delivery = claim(recovery_stream, recovery_entry_id, CONSUMER_TWO)
recovery_pending_after_claim = pending_snapshot(recovery_stream)
recovery_second_effect_applied = idempotent_effect("q22-recovery-event", recovery_effect_key)
recovery_effect_count = int(r.get(recovery_effect_key) or 0)
recovery_ack_count = r.xack(recovery_stream, GROUP, recovery_entry_id)
recovery_pending_after_ack = pending_snapshot(recovery_stream)

baseline_same_identity = baseline_delivery is not None and baseline_delivery[0] == baseline_entry_id
intervention_same_identity = intervention_delivery is not None and intervention_delivery[0] == intervention_entry_id
recovery_same_identity = (
    recovery_first_delivery is not None
    and recovery_second_delivery is not None
    and recovery_first_delivery[0] == recovery_entry_id
    and recovery_second_delivery[0] == recovery_entry_id
)

recovery_delivery_count = 0
if recovery_pending_after_claim["entries"]:
    recovery_delivery_count = recovery_pending_after_claim["entries"][0].get("times_delivered") or 0

assertions = {
    "baseline_message_delivered": baseline_same_identity,
    "baseline_effect_durable_before_ack": baseline_effect_applied == 1 and baseline_effect_count == 1,
    "baseline_ack_clears_pending": baseline_ack_count == 1 and baseline_pending["count"] == 0,
    "intervention_message_delivered": intervention_same_identity,
    "intervention_was_pending_before_ack": intervention_pending_before_ack["count"] == 1,
    "intervention_ack_clears_pending_before_effect": intervention_ack_count == 1 and intervention_pending_after_ack["count"] == 0,
    "intervention_expected_effect_is_missing": intervention_effect_count == 0,
    "intervention_acknowledged_entry_is_not_claimable": intervention_claim_after_ack is None,
    "intervention_acknowledged_entry_is_not_redelivered_as_new": intervention_new_read_after_ack is None,
    "intervention_payload_can_remain_physically_stored": len(intervention_stream_entry) == 1,
    "recovery_effect_becomes_durable_before_ack": recovery_first_effect_applied == 1,
    "recovery_missing_ack_leaves_redeliverable_work": recovery_pending_before_claim["count"] == 1,
    "recovery_same_message_is_redelivered": recovery_same_identity and recovery_delivery_count >= 2,
    "recovery_idempotency_suppresses_duplicate_effect": recovery_second_effect_applied == 0,
    "recovery_effect_exists_exactly_once": recovery_effect_count == 1,
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
        "action": "acknowledge_delivery_then_omit_business_effect",
        "failure_window": "after_durable_ack_before_durable_effect",
        "recovery": "durable_atomic_idempotency_guard_and_effect_before_ack_then_safe_redelivery",
    },
    "observations": {
        "baseline": {
            "stream_entry_id": baseline_entry_id,
            "delivery": baseline_delivery,
            "effect_applied": baseline_effect_applied,
            "effect_count": baseline_effect_count,
            "ack_count": baseline_ack_count,
            "pending_after_ack": baseline_pending,
            "ordering": ["delivery", "durable_effect", "ack"],
        },
        "intervention": {
            "stream_entry_id": intervention_entry_id,
            "delivery": intervention_delivery,
            "pending_before_ack": intervention_pending_before_ack,
            "ack_count": intervention_ack_count,
            "pending_after_ack": intervention_pending_after_ack,
            "effect_count": intervention_effect_count,
            "claim_after_ack": intervention_claim_after_ack,
            "new_read_after_ack": intervention_new_read_after_ack,
            "stream_entry_retained": len(intervention_stream_entry) == 1,
            "ordering": ["delivery", "ack", "effect_omitted"],
        },
        "recovery": {
            "stream_entry_id": recovery_entry_id,
            "first_delivery": recovery_first_delivery,
            "first_effect_applied": recovery_first_effect_applied,
            "pending_before_claim": recovery_pending_before_claim,
            "second_delivery": recovery_second_delivery,
            "pending_after_claim": recovery_pending_after_claim,
            "second_effect_applied": recovery_second_effect_applied,
            "effect_count": recovery_effect_count,
            "ack_count": recovery_ack_count,
            "pending_after_ack": recovery_pending_after_ack,
            "ordering": ["delivery", "durable_idempotent_effect", "ack_omitted", "redelivery", "dedup", "ack"],
        },
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "Redis Streams consumer-group acknowledgement can remove a delivery from pending/redelivery eligibility before the expected business effect exists. In the intervention, XACK succeeds, pending becomes empty, the same entry cannot be claimed or read again as new work, and the effect remains absent even though the stream payload is still physically retained. In recovery, making an idempotent effect durable before ACK leaves an unacknowledged delivery recoverable; the same entry is redelivered, the duplicate effect is suppressed, and ACK then completes processing.",
    "limitations": [
        "The experiment uses Redis Streams consumer groups in Docker and does not claim identical acknowledgement internals across Kafka, NATS JetStream, SQS, RabbitMQ, or other brokers.",
        "The post-ACK crash window is represented deterministically by omitting the business effect rather than killing a worker process at an arbitrary instruction boundary.",
        "The recovery idempotency marker and synthetic effect are colocated in Redis and updated atomically; production systems may require a transactional inbox, database transaction, outbox, or provider-specific idempotency contract when the business effect lives elsewhere.",
        "The acknowledged Redis stream entry remains physically stored; the demonstrated loss is effective loss from this consumer group's redelivery path, not payload deletion.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
