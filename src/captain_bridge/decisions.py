from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import (
    ConflictError,
    OperationError,
    ValidationError,
    decision_mode,
    now,
    new_id,
    require_text,
    validate_id,
)
from .storage import Storage




def _entity_id(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{kind} ID must be text")
    return validate_id(value, kind)


def _decision_path(ship: Path, decision_id: str) -> Path:
    return ship / "decisions" / f"{_entity_id(decision_id, 'decision')}.json"


def _read_decision(storage: Storage, path: Path, decision_id: str) -> dict[str, Any]:
    try:
        record = storage.read_json(path)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise OperationError(f"invalid decision record: {path}") from exc
    if not isinstance(record, dict) or record.get("id") != decision_id:
        raise OperationError(f"invalid decision record: {path}")
    return record


def _all_decisions(storage: Storage, ship: Path) -> list[dict[str, Any]]:
    directory = ship / "decisions"
    if not directory.exists():
        return []
    return [
        _read_decision(storage, path, _entity_id(path.stem, "decision"))
        for path in sorted(directory.glob("*.json"))
    ]


def _superseded_by(records: list[dict[str, Any]]) -> dict[str, str]:
    links: dict[str, str] = {}
    for record in records:
        target = record.get("supersedes")
        if target is not None:
            if target in links:
                raise OperationError(f"decision {target} has multiple successors")
            links[target] = record["id"]
    return links


def request_decision(
    ship: Path,
    *,
    question: str,
    mode: str,
    confidence: str,
    assignment_id: str | None = None,
    affected_assignments: list[str] | None = None,
    blocks_further_dependent_work: bool = False,
    impact: str | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    storage = Storage()
    ship = storage.resolve_ship(ship)
    question = require_text(question, "question")
    confidence = require_text(confidence, "confidence")
    mode = decision_mode(confidence, require_text(mode, "decision mode"))
    if not isinstance(blocks_further_dependent_work, bool):
        raise ValidationError("blocksFurtherDependentWork must be a boolean")

    assignment_id = _entity_id(assignment_id, "assignment") if assignment_id is not None else None
    if affected_assignments is not None and not isinstance(affected_assignments, list):
        raise ValidationError("affectedAssignments must be a list")
    affected = [_entity_id(item, "assignment") for item in affected_assignments or []]
    impact = None if impact is None else require_text(impact, "impact")
    if supersedes is not None:
        supersedes = _entity_id(supersedes, "decision")
        _read_decision(storage, _decision_path(ship, supersedes), supersedes)
        if supersedes in _superseded_by(_all_decisions(storage, ship)):
            raise ConflictError(f"decision {supersedes} already has a successor")

    decision_id = new_id("decision")
    record: dict[str, Any] = {
        "id": decision_id,
        "status": "pending",
        "mode": mode,
        "confidence": confidence,
        "question": question,
        "answer": None,
        "resolvedBy": None,
        "rationale": None,
        "createdAt": now(),
        "resolvedAt": None,
        "assignmentId": assignment_id,
        "affectedAssignments": affected,
        "blocksFurtherDependentWork": blocks_further_dependent_work,
        "impact": impact,
        "supersedes": supersedes,
        "reviewedAt": None,
        "reviewNote": None,
    }
    if supersedes is not None:
        with storage.file_lock(ship / "decisions.lock"):
            _read_decision(storage, _decision_path(ship, supersedes), supersedes)
            if supersedes in _superseded_by(_all_decisions(storage, ship)):
                raise ConflictError(f"decision {supersedes} already has a successor")
            storage.exclusive_write_json(_decision_path(ship, decision_id), record)
    else:
        storage.exclusive_write_json(_decision_path(ship, decision_id), record)
    return record


def pending_decisions(
    ship: Path,
    assignment_id: str | None = None,
) -> list[dict[str, Any]]:
    storage = Storage()
    ship = storage.resolve_ship(ship)
    assignment_id = _entity_id(assignment_id, "assignment") if assignment_id is not None else None
    records = _all_decisions(storage, ship)
    superseded = _superseded_by(records)
    pending = [
        record
        for record in records
        if record["id"] not in superseded
        and (assignment_id is None or record.get("assignmentId") == assignment_id)
        and (
            record.get("answer") is None
            or (record.get("mode") == "reviewable" and record.get("reviewedAt") is None)
        )
    ]
    return sorted(pending, key=lambda item: (item.get("createdAt", ""), item.get("id", "")))


def resolve_decision(
    ship: Path,
    decision_id: str,
    *,
    answer: str,
    resolved_by: str,
    rationale: str,
) -> dict[str, Any]:
    storage = Storage()
    ship = storage.resolve_ship(ship)
    path = _decision_path(ship, decision_id)
    with storage.file_lock((ship / "decisions.lock").resolve()):
        record = _read_decision(storage, path, decision_id)
        resolution = {
            "answer": require_text(answer, "answer"),
            "resolvedBy": require_text(resolved_by, "resolvedBy"),
            "rationale": require_text(rationale, "rationale"),
        }
        if record.get("status") == "resolved":
            if all(record.get(key) == value for key, value in resolution.items()):
                return record
            raise ConflictError(f"decision {decision_id} is already resolved differently")
        if record.get("status") != "pending":
            raise ConflictError(f"decision {decision_id} cannot be resolved from status {record.get('status')!r}")

        record.update(resolution)
        record["status"] = "resolved"
        record["resolvedAt"] = now()
        storage.atomic_write_json(path, record)
        return record


def review_decision(
    ship: Path,
    decision_id: str,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    storage = Storage()
    ship = storage.resolve_ship(ship)
    path = _decision_path(ship, decision_id)
    with storage.file_lock((ship / "decisions.lock").resolve()):
        record = _read_decision(storage, path, decision_id)
        note = None if note is None else require_text(note, "review note")
        if record.get("status") != "resolved":
            raise ConflictError(f"decision {decision_id} must be resolved before review")
        if record.get("reviewedAt") is not None:
            if record.get("reviewNote") == note:
                return record
            raise ConflictError(f"decision {decision_id} was already reviewed with a different note")

        record["reviewedAt"] = now()
        record["reviewNote"] = note
        storage.atomic_write_json(path, record)
        return record
