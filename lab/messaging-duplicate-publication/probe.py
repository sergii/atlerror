import json
import os
import platform

import redis

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
GROUP = "atlerror-consumers"
CONSUMER = "consumer-1"

EXPERIMENT_ID = "experiment.messaging.duplicate_publication.redis_streams_python"
CLAIM_ID = "claim.messaging.duplicate_publication.distinct_messages_same_event_can_duplicate_effect"

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


def read_new(stream, count):
    rows = r.xreadgroup(GROUP, CONSUMER, {stream: ">"}, count=count, block=1000)
    if not rows:
        return []
    _, messages = rows[0]
    return messages


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


def delivery_counts(snapshot):
    return {
        entry["message_id"]: entry.get("times_delivered") or 0
        for entry in snapshot["entries"]
    }


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
    return int(
        IDEMPOTENT_EFFECT_SCRIPT(
            keys=[f"processed:{event_id}", effect_key],
            args=["1"],
        )
    )


# Baseline: one business event is published once, delivered once, and applies one effect.
baseline_stream = "atlerror:duplicate-publication:baseline"
baseline_effect_key = "effect:duplicate-publication:baseline"
r.delete(baseline_effect_key)
create_stream(baseline_stream)
baseline_event_id = "baseline-event"
baseline_entry_id = add_message(baseline_stream, baseline_event_id)
baseline_deliveries = read_new(baseline_stream, 1)
baseline_pending_before_ack = pending_snapshot(baseline_stream)
baseline_effect_count = naive_effect(baseline_effect_key)
baseline_ack_count = r.xack(baseline_stream, GROUP, baseline_entry_id)
baseline_pending_after_ack = pending_snapshot(baseline_stream)

# Intervention: publish the same business event twice as two distinct broker entries.
intervention_stream = "atlerror:duplicate-publication:intervention"
intervention_effect_key = "effect:duplicate-publication:intervention"
r.delete(intervention_effect_key)
create_stream(intervention_stream)
intervention_event_id = "shared-business-event"
intervention_entry_one = add_message(intervention_stream, intervention_event_id)
intervention_entry_two = add_message(intervention_stream, intervention_event_id)
intervention_deliveries = read_new(intervention_stream, 2)
intervention_pending_before_ack = pending_snapshot(intervention_stream)
intervention_effect_after_first = naive_effect(intervention_effect_key)
intervention_effect_after_second = naive_effect(intervention_effect_key)
intervention_ack_count = r.xack(
    intervention_stream,
    GROUP,
    intervention_entry_one,
    intervention_entry_two,
)
intervention_pending_after_ack = pending_snapshot(intervention_stream)

# Recovery: keep the duplicate publications, but deduplicate by stable business event identity.
recovery_stream = "atlerror:duplicate-publication:recovery"
recovery_effect_key = "effect:duplicate-publication:recovery"
recovery_event_id = "recovery-business-event"
r.delete(recovery_effect_key, f"processed:{recovery_event_id}")
create_stream(recovery_stream)
recovery_entry_one = add_message(recovery_stream, recovery_event_id)
recovery_entry_two = add_message(recovery_stream, recovery_event_id)
recovery_deliveries = read_new(recovery_stream, 2)
recovery_pending_before_ack = pending_snapshot(recovery_stream)
recovery_first_applied = idempotent_effect(recovery_event_id, recovery_effect_key)
recovery_second_applied = idempotent_effect(recovery_event_id, recovery_effect_key)
recovery_effect_count = int(r.get(recovery_effect_key) or 0)
recovery_ack_count = r.xack(
    recovery_stream,
    GROUP,
    recovery_entry_one,
    recovery_entry_two,
)
recovery_pending_after_ack = pending_snapshot(recovery_stream)


def delivered_ids(deliveries):
    return [message_id for message_id, _fields in deliveries]


def delivered_event_ids(deliveries):
    return [fields.get("event_id") for _message_id, fields in deliveries]


baseline_counts = delivery_counts(baseline_pending_before_ack)
intervention_counts = delivery_counts(intervention_pending_before_ack)
recovery_counts = delivery_counts(recovery_pending_before_ack)

intervention_ids = delivered_ids(intervention_deliveries)
recovery_ids = delivered_ids(recovery_deliveries)

assertions = {
    "baseline_one_broker_message": len(baseline_deliveries) == 1 and delivered_ids(baseline_deliveries) == [baseline_entry_id],
    "baseline_delivered_once": baseline_counts.get(baseline_entry_id) == 1,
    "baseline_effect_once": baseline_effect_count == 1,
    "baseline_ack_clears_pending": baseline_ack_count == 1 and baseline_pending_after_ack["count"] == 0,
    "intervention_two_distinct_broker_messages": intervention_entry_one != intervention_entry_two and set(intervention_ids) == {intervention_entry_one, intervention_entry_two},
    "intervention_same_business_event_identity": delivered_event_ids(intervention_deliveries) == [intervention_event_id, intervention_event_id],
    "intervention_each_message_delivered_once": intervention_counts.get(intervention_entry_one) == 1 and intervention_counts.get(intervention_entry_two) == 1,
    "intervention_no_broker_redelivery": all(count == 1 for count in intervention_counts.values()) and len(intervention_counts) == 2,
    "intervention_naive_handler_duplicates_effect": intervention_effect_after_first == 1 and intervention_effect_after_second == 2,
    "intervention_ack_clears_pending": intervention_ack_count == 2 and intervention_pending_after_ack["count"] == 0,
    "recovery_two_distinct_broker_messages": recovery_entry_one != recovery_entry_two and set(recovery_ids) == {recovery_entry_one, recovery_entry_two},
    "recovery_same_business_event_identity": delivered_event_ids(recovery_deliveries) == [recovery_event_id, recovery_event_id],
    "recovery_each_message_delivered_once": recovery_counts.get(recovery_entry_one) == 1 and recovery_counts.get(recovery_entry_two) == 1,
    "recovery_first_effect_applied": recovery_first_applied == 1,
    "recovery_event_id_idempotency_suppresses_second_effect": recovery_second_applied == 0,
    "recovery_effect_exactly_once": recovery_effect_count == 1,
    "recovery_ack_clears_pending": recovery_ack_count == 2 and recovery_pending_after_ack["count"] == 0,
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
        "action": "publish_same_business_event_twice_as_distinct_broker_entries",
        "identity_model": "stable_business_event_id_distinct_broker_message_ids",
        "recovery": "deduplicate_business_effect_by_stable_event_identity",
    },
    "observations": {
        "baseline": {
            "business_event_id": baseline_event_id,
            "broker_message_ids": [baseline_entry_id],
            "deliveries": baseline_deliveries,
            "pending_before_ack": baseline_pending_before_ack,
            "effect_count": baseline_effect_count,
            "ack_count": baseline_ack_count,
            "pending_after_ack": baseline_pending_after_ack,
        },
        "intervention": {
            "business_event_id": intervention_event_id,
            "broker_message_ids": [intervention_entry_one, intervention_entry_two],
            "broker_message_ids_distinct": intervention_entry_one != intervention_entry_two,
            "deliveries": intervention_deliveries,
            "pending_before_ack": intervention_pending_before_ack,
            "effect_count_after_first_delivery": intervention_effect_after_first,
            "effect_count_after_second_delivery": intervention_effect_after_second,
            "ack_count": intervention_ack_count,
            "pending_after_ack": intervention_pending_after_ack,
        },
        "recovery": {
            "business_event_id": recovery_event_id,
            "broker_message_ids": [recovery_entry_one, recovery_entry_two],
            "broker_message_ids_distinct": recovery_entry_one != recovery_entry_two,
            "deliveries": recovery_deliveries,
            "pending_before_ack": recovery_pending_before_ack,
            "first_effect_applied": recovery_first_applied,
            "second_effect_applied": recovery_second_applied,
            "effect_count": recovery_effect_count,
            "ack_count": recovery_ack_count,
            "pending_after_ack": recovery_pending_after_ack,
        },
    },
    "assertions": assertions,
    "result": result,
    "interpretation": "The same stable business event was published as two distinct Redis Stream entries. Each broker entry was delivered exactly once, so the duplicate business effect in the naive handler is not broker redelivery. Deduplicating by the stable business event identity accepts both distinct messages while applying the protected effect exactly once.",
    "limitations": [
        "The experiment uses Redis Streams consumer groups in Docker and does not claim identical producer or message-identity semantics across Kafka, NATS JetStream, SQS, RabbitMQ, or other systems.",
        "The duplicate publications are created deterministically rather than reproducing a specific producer retry, acknowledgement-loss, outbox replay, or upstream duplication failure.",
        "The recovery idempotency guard and synthetic side effect are colocated in Redis; production systems may require database uniqueness, transactional inboxes, or provider-specific idempotency contracts.",
    ],
}

print(json.dumps(evidence, sort_keys=True))
