#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from causal_projection import ROOT
from probe_filesystem_claim import (
    ProbeFilesystemClaimError,
    acquire_probe_filesystem_claim,
)
from runtime_evidence import format_timestamp, parse_timestamp

JOURNAL_FILENAME = "probe-workflow.journal.jsonl"
EVENT_SCHEMA_PATH = ROOT / "schema" / "probe-workflow-event.schema.json"
JOURNAL_CLAIM_PURPOSE = "workflow_journal"
EVENT_TYPES = ("begin", "finish", "abandon", "reconcile")


def journal_path(session_dir: Path) -> Path:
    return session_dir / JOURNAL_FILENAME


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_event_schema(path: Path = EVENT_SCHEMA_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return schema


def validate_probe_workflow_event(
    event: dict[str, Any],
    *,
    schema_path: Path = EVENT_SCHEMA_PATH,
) -> None:
    validator = Draft202012Validator(
        _load_event_schema(schema_path),
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(event), key=lambda error: list(error.path))
    if errors:
        raise ValueError(
            "probe workflow event schema validation failed: "
            + "; ".join(error.message for error in errors)
        )


def _event_hash(event_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(event_without_hash).encode("utf-8")).hexdigest()


def _read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if not path.is_file():
        raise ValueError(f"probe workflow journal path is not a file: {path}")

    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\r\n")
                if not line:
                    raise ValueError(
                        f"probe workflow journal contains an empty line at {line_number}"
                    )
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"probe workflow journal line {line_number} is invalid JSON: {exc}"
                    ) from exc
                if not isinstance(event, dict):
                    raise ValueError(
                        f"probe workflow journal line {line_number} must be a JSON object"
                    )
                events.append(event)
    except OSError as exc:
        raise ValueError(f"cannot read probe workflow journal {path}: {exc}") from exc
    return events


def verify_probe_workflow_journal(
    session_dir: Path,
    *,
    incident_id: str | None = None,
) -> dict[str, Any]:
    path = journal_path(session_dir)
    events = _read_lines(path)
    previous_hash: str | None = None

    for index, event in enumerate(events, start=1):
        validate_probe_workflow_event(event)
        parse_timestamp(event["recorded_at"], "probe workflow event recorded_at")
        if event["sequence"] != index:
            raise ValueError(
                f"probe workflow journal sequence mismatch at line {index}: "
                f"expected {index}, got {event['sequence']}"
            )
        if event["previous_hash"] != previous_hash:
            raise ValueError(
                f"probe workflow journal previous_hash mismatch at sequence {index}"
            )
        unsigned = {key: copy.deepcopy(value) for key, value in event.items() if key != "event_hash"}
        expected_hash = _event_hash(unsigned)
        if event["event_hash"] != expected_hash:
            raise ValueError(
                f"probe workflow journal event_hash mismatch at sequence {index}"
            )
        previous_hash = event["event_hash"]

    selected = [
        copy.deepcopy(event)
        for event in events
        if incident_id is None or event["incident_id"] == incident_id
    ]
    return {
        "schema_version": "0.1",
        "kind": "probe_workflow_journal",
        "path": str(path),
        "verified": True,
        "event_count": len(selected),
        "total_event_count": len(events),
        "head_hash": previous_hash,
        "events": selected,
    }


def _semantic_identity(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": event["event_type"],
        "incident_id": event["incident_id"],
        "session_id": event["session_id"],
        "data": copy.deepcopy(event["data"]),
    }


def append_probe_workflow_event(
    session_dir: Path,
    *,
    event_type: str,
    incident_id: str,
    session_id: str,
    data: dict[str, Any],
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported probe workflow event type: {event_type}")
    if not incident_id:
        raise ValueError("probe workflow event incident_id must not be empty")
    if not isinstance(data, dict):
        raise ValueError("probe workflow event data must be an object")

    recorded_at = (recorded_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        with acquire_probe_filesystem_claim(
            session_dir,
            purpose=JOURNAL_CLAIM_PURPOSE,
            identity={"journal": JOURNAL_FILENAME},
            acquired_at=recorded_at,
        ):
            projection = verify_probe_workflow_journal(session_dir)
            events = projection["events"]
            proposed_identity = {
                "event_type": event_type,
                "incident_id": incident_id,
                "session_id": session_id,
                "data": copy.deepcopy(data),
            }
            same_transition = [
                event
                for event in events
                if event["event_type"] == event_type and event["session_id"] == session_id
            ]
            if same_transition:
                if len(same_transition) != 1:
                    raise ValueError(
                        f"probe workflow journal contains duplicate {event_type} events for {session_id}"
                    )
                existing = same_transition[0]
                if _semantic_identity(existing) != proposed_identity:
                    raise ValueError(
                        f"probe workflow journal already contains a conflicting {event_type} event "
                        f"for {session_id}"
                    )
                return {**copy.deepcopy(existing), "already_recorded": True}

            previous_hash = events[-1]["event_hash"] if events else None
            event_without_hash = {
                "schema_version": "0.1",
                "kind": "probe_workflow_event",
                "sequence": len(events) + 1,
                "event_type": event_type,
                "recorded_at": format_timestamp(recorded_at),
                "incident_id": incident_id,
                "session_id": session_id,
                "data": copy.deepcopy(data),
                "previous_hash": previous_hash,
            }
            event = {
                **event_without_hash,
                "event_hash": _event_hash(event_without_hash),
            }
            validate_probe_workflow_event(event)

            path = journal_path(session_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(_canonical_json(event) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise ValueError(f"cannot append probe workflow journal {path}: {exc}") from exc
            return {**copy.deepcopy(event), "already_recorded": False}
    except ProbeFilesystemClaimError as exc:
        raise ValueError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and inspect the append-only Atlerror probe workflow journal."
    )
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--incident-id")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        document = verify_probe_workflow_journal(
            args.session_dir,
            incident_id=args.incident_id,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(document, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
