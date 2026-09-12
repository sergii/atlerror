#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_evidence import format_timestamp

SESSION_ID_PATTERN = re.compile(r"^probe-session\.[0-9a-f]{16}$")
SESSION_SUFFIX = ".json"
BINDING_SUFFIX = ".binding.json"
ABANDONMENT_SUFFIX = ".abandoned.json"
RECONCILIATION_SUFFIX = ".reconciled.json"


class ProbeWorkflowPartialStateError(ValueError):
    pass


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _session_id_from_path(path: Path) -> str | None:
    name = path.name
    if name.endswith(BINDING_SUFFIX):
        candidate = name[: -len(BINDING_SUFFIX)]
    elif name.endswith(ABANDONMENT_SUFFIX):
        candidate = name[: -len(ABANDONMENT_SUFFIX)]
    elif name.endswith(RECONCILIATION_SUFFIX):
        candidate = name[: -len(RECONCILIATION_SUFFIX)]
    elif name.endswith(SESSION_SUFFIX):
        candidate = name[: -len(SESSION_SUFFIX)]
    else:
        return None
    return candidate if SESSION_ID_PATTERN.fullmatch(candidate) is not None else None


def _load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _incident_id(path: Path) -> str | None:
    document = _load_json_object(path)
    if document is None:
        return None
    incident_id = document.get("incident_id")
    return incident_id if isinstance(incident_id, str) and incident_id else None


def _fingerprint(issue_kind: str, files: list[Path]) -> str:
    payload = {
        "issue_kind": issue_kind,
        "files": [
            {
                "name": path.name,
                "sha256": _sha256(path),
            }
            for path in sorted(files, key=lambda item: item.name)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reconciliation_path(session_dir: Path, session_id: str) -> Path:
    if SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ValueError("invalid probe session id")
    return session_dir / f"{session_id}{RECONCILIATION_SUFFIX}"


def _load_reconciliation(path: Path) -> dict[str, Any] | None:
    document = _load_json_object(path)
    if document is None:
        return None
    required = {
        "schema_version",
        "kind",
        "session_id",
        "incident_id",
        "issue_kind",
        "fingerprint",
        "resolution",
        "reconciled_at",
        "files",
    }
    if set(document) != required:
        return None
    if document["schema_version"] != "0.1" or document["kind"] != "mcp_probe_workflow_reconciliation":
        return None
    if document["resolution"] != "discard_partial_state":
        return None
    if not isinstance(document["fingerprint"], str) or not document["fingerprint"]:
        return None
    return document


def scan_partial_probe_workflows(
    session_dir: Path,
    *,
    incident_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return unresolved legacy partial session/binding pairs without mutating workflow state."""
    if not session_dir.exists():
        return []
    if not session_dir.is_dir():
        raise ValueError(f"probe session path is not a directory: {session_dir}")

    sessions: dict[str, Path] = {}
    bindings: dict[str, Path] = {}

    for path in sorted(session_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        session_id = _session_id_from_path(path)
        if session_id is None:
            continue
        if path.name.endswith(BINDING_SUFFIX):
            bindings[session_id] = path
        elif (
            path.name.endswith(SESSION_SUFFIX)
            and not path.name.endswith(ABANDONMENT_SUFFIX)
            and not path.name.endswith(RECONCILIATION_SUFFIX)
        ):
            sessions[session_id] = path

    issues: list[dict[str, Any]] = []
    for session_id in sorted(set(sessions) | set(bindings)):
        session_path = sessions.get(session_id)
        binding_path = bindings.get(session_id)
        if session_path is not None and binding_path is not None:
            continue

        if session_path is not None:
            issue_kind = "orphan_session"
            files = [session_path]
            detected_incident_id = _incident_id(session_path)
        else:
            issue_kind = "orphan_binding"
            files = [binding_path] if binding_path is not None else []
            detected_incident_id = _incident_id(binding_path) if binding_path is not None else None

        if incident_id is not None and detected_incident_id not in (None, incident_id):
            continue

        fingerprint = _fingerprint(issue_kind, files)
        marker_path = reconciliation_path(session_dir, session_id)
        marker = _load_reconciliation(marker_path) if marker_path.exists() else None
        resolved = (
            marker is not None
            and marker.get("session_id") == session_id
            and marker.get("issue_kind") == issue_kind
            and marker.get("fingerprint") == fingerprint
            and marker.get("incident_id") == detected_incident_id
        )
        if resolved:
            continue

        issues.append(
            {
                "session_id": session_id,
                "incident_id": detected_incident_id,
                "issue_kind": issue_kind,
                "fingerprint": fingerprint,
                "files": [path.name for path in files],
                "recovery": "discard_partial_state",
            }
        )

    return issues


def raise_on_partial_probe_workflows(
    session_dir: Path,
    *,
    incident_id: str,
) -> None:
    issues = scan_partial_probe_workflows(session_dir, incident_id=incident_id)
    if not issues:
        return
    first = issues[0]
    suffix = "" if len(issues) == 1 else f" and {len(issues) - 1} more"
    raise ProbeWorkflowPartialStateError(
        "partial probe workflow state requires reconciliation: "
        f"{first['session_id']} ({first['issue_kind']}){suffix}; "
        "run scripts/probe_workflow_reconciliation.py status and discard the partial state before continuing"
    )


def reconcile_partial_probe_workflow(
    session_dir: Path,
    session_id: str,
    *,
    reconciled_at: datetime | None = None,
) -> dict[str, Any]:
    if SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ValueError("invalid probe session id")
    issues = scan_partial_probe_workflows(session_dir)
    matches = [issue for issue in issues if issue["session_id"] == session_id]
    if not matches:
        existing_path = reconciliation_path(session_dir, session_id)
        existing = _load_reconciliation(existing_path) if existing_path.exists() else None
        if existing is not None:
            return {**existing, "already_reconciled": True}
        raise ValueError(f"no unresolved partial probe workflow exists for {session_id}")
    if len(matches) != 1:
        raise ValueError(f"multiple partial workflow issues exist for {session_id}")

    issue = matches[0]
    reconciled_at = (reconciled_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    marker = {
        "schema_version": "0.1",
        "kind": "mcp_probe_workflow_reconciliation",
        "session_id": session_id,
        "incident_id": issue["incident_id"],
        "issue_kind": issue["issue_kind"],
        "fingerprint": issue["fingerprint"],
        "resolution": "discard_partial_state",
        "reconciled_at": format_timestamp(reconciled_at),
        "files": issue["files"],
    }
    _write_json_atomic(reconciliation_path(session_dir, session_id), marker)
    return {**marker, "already_reconciled": False}


def build_reconciliation_report(
    session_dir: Path,
    *,
    incident_id: str | None = None,
) -> dict[str, Any]:
    issues = scan_partial_probe_workflows(session_dir, incident_id=incident_id)
    return {
        "schema_version": "0.1",
        "kind": "probe_workflow_reconciliation_report",
        "session_dir": str(session_dir),
        "incident_id": incident_id,
        "recovery_required": bool(issues),
        "issue_count": len(issues),
        "issues": issues,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and safely reconcile partial persisted MCP probe workflow state."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Report unresolved partial workflow state.")
    status.add_argument("--session-dir", type=Path, required=True)
    status.add_argument("--incident-id")
    status.add_argument("--pretty", action="store_true")

    discard = subparsers.add_parser(
        "discard",
        help="Mark one partial workflow as discarded without deleting files or creating evidence.",
    )
    discard.add_argument("--session-dir", type=Path, required=True)
    discard.add_argument("--session-id", required=True)
    discard.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "status":
        document = build_reconciliation_report(
            args.session_dir,
            incident_id=args.incident_id,
        )
    else:
        document = reconcile_partial_probe_workflow(
            args.session_dir,
            args.session_id,
        )

    if args.pretty:
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
