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


def _required(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    return require_text(value, label)


def _optional(value: object | None, label: str) -> str | None:
    return None if value is None else _required(value, label)


def _entity_id(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{kind} ID must be text")
    return validate_id(value, kind)


def _decision_path(ship: Path, decision_id: str) -> Path:
    return ship / "decisions" / f"{_entity_id(decision_id, 'decision')}.json"


def _read_decision(storage: Storage, path: Path, decision_id: str) -> dict[str, Any]:
    try:
        record = storage.read_json(path)
    except json.JSONDecodeError as exc:
        raise OperationError(f"invalid decision record: {path}") from exc
    if not isinstance(record, dict) or record.get("id") != decision_id:
        raise OperationError(f"invalid decision record: {path}")
    return record


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
    question = _required(question, "question")
    mode = decision_mode(confidence, _required(mode, "decision mode"))
    if not isinstance(blocks_further_dependent_work, bool):
        raise ValidationError("blocksFurtherDependentWork must be a boolean")

    assignment_id = _entity_id(assignment_id, "assignment") if assignment_id is not None else None
    if affected_assignments is not None and not isinstance(affected_assignments, list):
        raise ValidationError("affectedAssignments must be a list")
    affected = [_entity_id(item, "assignment") for item in affected_assignments or []]
    impact = _optional(impact, "impact")
    if supersedes is not None:
        supersedes = _entity_id(supersedes, "decision")
        _read_decision(storage, _decision_path(ship, supersedes), supersedes)

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
    storage.exclusive_write_json(_decision_path(ship, decision_id), record)
    return record


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
    record = _read_decision(storage, path, decision_id)
    resolution = {
        "answer": _required(answer, "answer"),
        "resolvedBy": _required(resolved_by, "resolvedBy"),
        "rationale": _required(rationale, "rationale"),
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
    record = _read_decision(storage, path, decision_id)
    note = _optional(note, "review note")
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
