from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .domain import ConflictError, ValidationError, new_id, now, validate_slug
from .storage import Storage


def _repo(path: str | Path) -> Path:
    repo = Path(path).expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValidationError(f"not a Git worktree: {repo}")
    return repo


def _officer() -> dict[str, str]:
    officer: dict[str, str] = {}
    if os.environ.get("CAPTAIN_BRIDGE_OFFICER_NAME"):
        officer["agentName"] = os.environ["CAPTAIN_BRIDGE_OFFICER_NAME"]
    if os.environ.get("CAPTAIN_BRIDGE_OFFICER_ID"):
        officer["paneId"] = os.environ["CAPTAIN_BRIDGE_OFFICER_ID"]
    return officer

def create_ship(
    repo: str | Path,
    slug: str,
    *,
    storage: Storage | None = None,
    officer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    storage = storage or Storage()
    storage.ensure_defaults()
    repository = _repo(repo)
    validate_slug(slug)
    ship_id = new_id("ship")
    ship = storage.ships_dir / f"{repository.name}-{slug}"
    if ship.exists():
        raise ConflictError(f"ship already exists: {ship}")
    metadata = {
        "shipId": ship_id,
        "slug": slug,
        "repoDir": str(repository),
        "name": f"{repository.name}-{slug}",
        "createdAt": now(),
    }
    with storage.staged_directory(ship) as staging:
        for name in ("events", "decisions", "memory", "assignments"):
            (staging / name).mkdir()
        storage.exclusive_write_json(staging / "metadata.json", metadata)
        storage.atomic_write_text(staging / "index.md", f"# {metadata['name']}\n")
        storage.exclusive_write_json(staging / "officer.json", officer if officer is not None else _officer())
    return reconcile(ship, storage=storage)


def open_ship(path: str | Path | None = None, *, storage: Storage | None = None) -> dict[str, Any]:
    storage = storage or Storage()
    storage.ensure_defaults()
    ship = storage.resolve_ship(path)
    metadata = storage.read_json(ship / "metadata.json")
    repository = metadata.get("repoDir")
    if not isinstance(repository, str) or not repository:
        raise ValidationError("ship metadata.repoDir is required")
    repo = Path(repository).expanduser().resolve()
    if not repo.exists() or not (repo / ".git").exists():
        raise ValidationError(f"ship repository missing: {repo}")
    officer_path = ship / "officer.json"
    officer = storage.read_json(officer_path) if officer_path.exists() else {}
    current = _officer()
    if current and current != officer:
        officer = current
        storage.atomic_write_json(officer_path, officer)
    return reconcile(ship, storage=storage)


def _all_events(ship: Path) -> list[dict[str, Any]]:
    import json

    events: list[dict[str, Any]] = []
    for path in sorted((ship / "events").glob("*.json")):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"invalid event record: {path}") from exc
        if isinstance(event, dict):
            events.append(event)
    return sorted(events, key=lambda event: (event.get("at", ""), event.get("id", "")))


def reconcile(path: str | Path, *, storage: Storage | None = None) -> dict[str, Any]:
    storage = storage or Storage()
    storage.ensure_defaults()
    ship = storage.resolve_ship(path)
    metadata = storage.read_json(ship / "metadata.json")
    if not all(isinstance(metadata.get(key), str) and metadata[key] for key in ("shipId", "createdAt", "repoDir")):
        raise ValidationError("ship metadata requires shipId, createdAt, and repoDir")
    officer_path = ship / "officer.json"
    officer = storage.read_json(officer_path) if officer_path.exists() else {}
    from . import assignments, decisions

    assignment_views = []
    for directory in sorted((ship / "assignments").iterdir() if (ship / "assignments").exists() else []):
        if directory.is_dir() and (directory / "assignment.json").exists():
            assignment_views.append(assignments.inspect_assignment(ship, directory.name))

    decision_records = []
    import json

    for decision_path in sorted((ship / "decisions").glob("*.json")):
        try:
            record = json.loads(decision_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"invalid decision record: {decision_path}") from exc
        if isinstance(record, dict):
            decision_records.append(record)
    pending = decisions.pending_decisions(ship, assignment_id=None)
    events = _all_events(ship)
    summary = {
        "assignmentCount": len(assignment_views),
        "pendingDecisionCount": len(pending),
        "eventCount": len(events),
        "resultReadyCount": sum(view.get("status") == "result-ready" for view in assignment_views),
        "integratedCount": sum(view.get("status") == "integrated" for view in assignment_views),
        "worktreeCount": sum(bool(view.get("worktreeExists")) for view in assignment_views),
        "attentionRequired": bool(pending or any(view.get("status") in {"result-ready", "failed"} for view in assignment_views)),
    }
    return {
        "shipId": metadata["shipId"],
        "name": metadata.get("name", ship.name),
        "slug": metadata.get("slug"),
        "repoDir": metadata["repoDir"],
        "path": str(ship),
        "createdAt": metadata["createdAt"],
        "officer": officer,
        "assignments": assignment_views,
        "decisions": decision_records,
        "pendingDecisions": pending,
        "events": events,
        "summary": summary,
    }
