#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms.
    fcntl = None  # type: ignore[assignment]

CLAIM_DIRECTORY_NAME = ".claims"
CLAIM_SCHEMA_VERSION = "0.1"


class ProbeFilesystemClaimError(ValueError):
    pass


class ProbeFilesystemClaimBusy(ProbeFilesystemClaimError):
    pass


class ProbeFilesystemClaimUnavailable(ProbeFilesystemClaimError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def claim_path(
    session_dir: Path,
    *,
    purpose: str,
    identity: dict[str, Any],
) -> Path:
    if not purpose or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in purpose):
        raise ProbeFilesystemClaimError("probe filesystem claim purpose is invalid")
    digest = hashlib.sha256(
        _canonical_json({"purpose": purpose, "identity": identity}).encode("utf-8")
    ).hexdigest()[:24]
    return session_dir / CLAIM_DIRECTORY_NAME / f"{purpose}.{digest}.lock"


def target_scope_claim_identity(
    *,
    incident_id: str,
    target: str,
    scope: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "incident_id": incident_id,
        "target": target,
        "scope": scope,
    }


def session_claim_identity(
    *,
    incident_id: str,
    session_id: str,
) -> dict[str, Any]:
    return {
        "incident_id": incident_id,
        "session_id": session_id,
    }


def incident_mutation_claim_identity(*, incident_id: str) -> dict[str, Any]:
    return {"incident_id": incident_id}


def _read_owner_metadata(handle: Any) -> dict[str, Any] | None:
    try:
        handle.seek(0)
        raw = handle.read()
        if not raw.strip():
            return None
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _write_owner_metadata(
    handle: Any,
    *,
    purpose: str,
    identity: dict[str, Any],
    acquired_at: datetime,
) -> dict[str, Any]:
    document = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "kind": "probe_filesystem_claim",
        "purpose": purpose,
        "identity": identity,
        "owner": {"pid": os.getpid()},
        "acquired_at": _format_timestamp(acquired_at),
    }
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
    return document


@contextmanager
def acquire_probe_filesystem_claim(
    session_dir: Path,
    *,
    purpose: str,
    identity: dict[str, Any],
    acquired_at: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Acquire a non-blocking process-safe claim backed by a persistent local lock file."""
    if fcntl is None:
        raise ProbeFilesystemClaimUnavailable(
            "probe filesystem claims require POSIX advisory file locking"
        )
    if not isinstance(identity, dict) or not identity:
        raise ProbeFilesystemClaimError("probe filesystem claim identity must be a non-empty object")

    path = claim_path(session_dir, purpose=purpose, identity=identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise ProbeFilesystemClaimUnavailable(
            f"cannot open probe filesystem claim {path}: {exc}"
        ) from exc

    locked = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            owner = _read_owner_metadata(handle)
            owner_pid = owner.get("owner", {}).get("pid") if isinstance(owner, dict) else None
            suffix = f" held by pid {owner_pid}" if isinstance(owner_pid, int) else ""
            raise ProbeFilesystemClaimBusy(
                f"probe filesystem claim is busy for {purpose}{suffix}"
            ) from exc
        except OSError as exc:
            raise ProbeFilesystemClaimUnavailable(
                f"cannot acquire probe filesystem claim {path}: {exc}"
            ) from exc

        metadata = _write_owner_metadata(
            handle,
            purpose=purpose,
            identity=identity,
            acquired_at=acquired_at or datetime.now(timezone.utc),
        )
        yield metadata
    finally:
        if locked:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


@contextmanager
def acquire_probe_filesystem_claims(
    session_dir: Path,
    claims: list[tuple[str, dict[str, Any]]],
    *,
    acquired_at: datetime | None = None,
) -> Iterator[list[dict[str, Any]]]:
    """Acquire several claims in deterministic path order to avoid lock-order ambiguity."""
    if not claims:
        yield []
        return

    prepared: list[tuple[Path, str, dict[str, Any]]] = []
    seen_paths: set[Path] = set()
    for purpose, identity in claims:
        path = claim_path(session_dir, purpose=purpose, identity=identity)
        if path in seen_paths:
            raise ProbeFilesystemClaimError(f"duplicate probe filesystem claim requested: {path.name}")
        seen_paths.add(path)
        prepared.append((path, purpose, identity))

    prepared.sort(key=lambda item: str(item[0]))
    acquired: list[dict[str, Any]] = []
    with ExitStack() as stack:
        for _path, purpose, identity in prepared:
            acquired.append(
                stack.enter_context(
                    acquire_probe_filesystem_claim(
                        session_dir,
                        purpose=purpose,
                        identity=identity,
                        acquired_at=acquired_at,
                    )
                )
            )
        yield acquired
