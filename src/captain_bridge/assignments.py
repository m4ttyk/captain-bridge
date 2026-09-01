from __future__ import annotations

import json
import subprocess
import tomllib
from functools import wraps
from pathlib import Path
from typing import Any

from . import runtime
from .domain import (
    CaptainError,
    ConflictError,
    NotFoundError,
    OperationError,
    ValidationError,
    derive_assignment_status,
    new_id,
    now,
    parse_result_sections,
    require_text,
    validate_id,
    validate_slug,
)
from .storage import Storage


def _captain_operation(function):
    @wraps(function)
    def operation(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CaptainError:
            raise
        except Exception as exc:
            raise OperationError(f"{function.__name__} failed: {exc}") from exc

    return operation


def _paths(ship: Path, assignment_id: str) -> tuple[Path, Path]:
    validate_id(assignment_id, "assignment")
    directory = ship / "assignments" / assignment_id
    return directory, directory / "assignment.json"


def _load_assignment(storage: Storage, ship: Path, assignment_id: str) -> tuple[Path, dict[str, Any]]:
    directory, path = _paths(ship, assignment_id)
    assignment = storage.read_json(path)
    if assignment.get("id") != assignment_id:
        raise ConflictError(f"assignment record does not match {assignment_id}")
    return directory, assignment


def _role(storage: Storage, role_name: str) -> tuple[dict[str, Any], str]:
    validate_slug(role_name)
    home = storage.home_dir() if callable(storage.home_dir) else storage.home_dir
    path = Path(home) / "roles" / f"{role_name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise NotFoundError(f"role not found: {role_name}") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "+++":
        raise ValidationError(f"role {role_name!r} has no TOML frontmatter")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "+++")
    except StopIteration as exc:
        raise ValidationError(f"role {role_name!r} has unterminated TOML frontmatter") from exc
    try:
        role = tomllib.loads("\n".join(lines[1:end]))
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"role {role_name!r} has invalid TOML frontmatter: {exc}") from exc
    repository = role.get("repository")
    if repository not in {"read", "worktree"}:
        raise ValidationError(f"role {role_name!r} repository must be 'read' or 'worktree'")
    role["name"] = role_name
    return role, "\n".join(lines[end + 1 :]).strip()


def _event(assignment_id: str, kind: str, **facts: Any) -> dict[str, Any]:
    return {
        "id": new_id("event"),
        "kind": kind,
        "at": now(),
        "assignmentId": assignment_id,
        **facts,
    }


def _events(ship: Path, assignment_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted((ship / "events").glob("*.json")):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationError(f"cannot read immutable event {path}: {exc}") from exc
        if event.get("assignmentId") == assignment_id:
            events.append(event)
    return sorted(events, key=lambda item: (item.get("at", ""), item.get("id", "")))


def _pending_decisions(ship: Path, assignment_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((ship / "decisions").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationError(f"cannot read decision {path}: {exc}") from exc
        if record.get("assignmentId") == assignment_id:
            records.append(record)
    superseded = {record.get("supersedes") for record in records if record.get("supersedes")}
    pending = [
        record
        for record in records
        if record.get("id") not in superseded
        and (
            record.get("answer") is None
            or (record.get("mode") == "reviewable" and record.get("reviewedAt") is None)
        )
    ]
    return sorted(pending, key=lambda item: (item.get("requestedAt", ""), item.get("id", "")))


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise OperationError(f"cannot run git: {exc}") from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise OperationError(f"git {' '.join(args)} failed: {detail}")
    return result


@_captain_operation
def create_assignment(ship: Path, *, role_name: str, prompt: str) -> dict:
    storage = Storage()
    storage.ensure_defaults()
    ship = storage.resolve_ship(ship)
    prompt = require_text(prompt, "prompt")
    role, role_prompt = _role(storage, role_name)
    metadata = storage.read_json(ship / "metadata.json")
    repo_dir = metadata.get("repoDir")
    if not isinstance(repo_dir, str) or not repo_dir:
        raise ValidationError("ship metadata.repoDir is required")

    assignment_id = new_id("assignment")
    directory, path = _paths(ship, assignment_id)
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ConflictError(f"assignment already exists: {assignment_id}") from exc
    assignment = {
        "id": assignment_id,
        "createdAt": now(),
        "role": role_name,
        "repository": role["repository"],
        "repoDir": str(Path(repo_dir).expanduser().resolve()),
    }
    storage.exclusive_write_json(path, assignment)
    storage.atomic_write_text(
        directory / "prompt.md",
        f"{role_prompt}\n\n## Assignment\n\n{prompt}\n",
    )
    (directory / "artifacts").mkdir()
    storage.append_event(ship, _event(assignment_id, "assignment-created"))
    return assignment


@_captain_operation
def launch_assignment(ship: Path, assignment_id: str) -> dict:
    storage = Storage()
    ship = storage.resolve_ship(ship)
    directory, assignment = _load_assignment(storage, ship, assignment_id)
    existing = assignment.get("runtime")
    if existing is not None:
        if not isinstance(existing, dict):
            raise ConflictError(f"assignment {assignment_id} has a malformed runtime binding")
        missing = [key for key in ("agentName", "paneId", "worktreeDir") if not existing.get(key)]
        if missing:
            raise ConflictError(
                f"assignment {assignment_id} has a partial runtime binding; missing: {', '.join(missing)}"
            )
        return existing
    role, _ = _role(storage, assignment["role"])
    launch_path = directory / "launch.json"
    if launch_path.exists():
        raise ConflictError(
            f"assignment {assignment_id} has a launch request but no runtime binding; inspect it before relaunching"
        )
    storage.exclusive_write_json(
        launch_path,
        {"assignmentId": assignment_id, "requestedAt": now()},
    )
    facts = runtime.launch_assignment(ship, assignment, role)
    if not isinstance(facts, dict):
        raise OperationError("runtime returned a non-object assignment binding")
    missing = [key for key in ("agentName", "paneId", "worktreeDir") if not facts.get(key)]
    if missing:
        raise OperationError(f"runtime returned a partial assignment binding; missing: {', '.join(missing)}")
    assignment["runtime"] = facts
    storage.atomic_write_json(directory / "assignment.json", assignment)
    storage.append_event(ship, _event(assignment_id, "assignment-launched"))
    return facts


@_captain_operation
def inspect_assignment(ship: Path, assignment_id: str) -> dict:
    storage = Storage()
    ship = storage.resolve_ship(ship)
    directory, assignment = _load_assignment(storage, ship, assignment_id)
    events = _events(ship, assignment_id)
    result_path = directory / "result.md"
    sections = parse_result_sections(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
    integration_path = directory / "integration.json"
    integration = storage.read_json(integration_path) if integration_path.exists() else None
    observed = runtime.observe_assignment(ship, assignment) if assignment.get("runtime") else None
    runtime_facts = assignment.get("runtime") or {}
    worktree = runtime_facts.get("worktreeDir")
    derived_runtime = dict(observed or {})
    live_status = derived_runtime.get("status")
    derived_runtime["status"] = {
        "working": "running",
        "blocked": "waiting",
        "idle": "settled",
        "done": "settled",
    }.get(live_status, live_status)
    status = derive_assignment_status(
        event_kinds=(event.get("kind", "") for event in events),
        has_result=sections is not None,
        has_integration=integration is not None,
        runtime=derived_runtime,
    )
    return {
        "assignment": assignment,
        "status": status,
        "result": sections,
        "events": events,
        "pendingDecisions": _pending_decisions(ship, assignment_id),
        "runtime": observed,
        "worktreeExists": bool(worktree and Path(worktree).exists()),
        "integration": integration,
    }


@_captain_operation
def message_assignment(ship: Path, assignment_id: str, message: str) -> dict:
    storage = Storage()
    ship = storage.resolve_ship(ship)
    _, assignment = _load_assignment(storage, ship, assignment_id)
    message = require_text(message, "message")
    if not assignment.get("runtime"):
        raise ConflictError(f"assignment {assignment_id} has no runtime binding")
    response = runtime.message_crewmate(ship, assignment, message)
    if response is False or (isinstance(response, dict) and response.get("delivered") is False):
        raise OperationError(f"message was not delivered to assignment {assignment_id}")
    storage.append_event(ship, _event(assignment_id, "assignment-messaged"))
    if isinstance(response, dict):
        return response
    return {"assignmentId": assignment_id, "delivered": True}


@_captain_operation
def integrate_assignment(ship: Path, assignment_id: str, commit: str) -> dict:
    storage = Storage()
    ship = storage.resolve_ship(ship)
    directory, assignment = _load_assignment(storage, ship, assignment_id)
    commit = require_text(commit, "commit")
    if commit.startswith("-"):
        raise ValidationError("commit must not begin with '-'")
    repo = Path(assignment["repoDir"])
    resolved = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", check=False)
    if resolved.returncode:
        raise NotFoundError(f"commit not found: {commit}")
    canonical = resolved.stdout.strip()

    receipt_path = directory / "integration.json"
    if receipt_path.exists():
        receipt = storage.read_json(receipt_path)
        if receipt.get("commit") != canonical:
            raise ConflictError(
                f"assignment {assignment_id} is already integrated at {receipt.get('commit', 'an unknown commit')}"
            )
        return receipt
    dirty = _git(repo, "status", "--porcelain").stdout
    if dirty:
        raise ConflictError(f"target repository is dirty: {repo}")

    ancestor = _git(repo, "merge-base", "--is-ancestor", canonical, "HEAD", check=False)
    if ancestor.returncode not in {0, 1}:
        detail = ancestor.stderr.strip() or ancestor.stdout.strip() or f"exit {ancestor.returncode}"
        raise OperationError(f"cannot determine whether {canonical} is reachable from target: {detail}")
    already_reachable = ancestor.returncode == 0
    if not already_reachable:
        cherry_pick = _git(repo, "cherry-pick", canonical, check=False)
        if cherry_pick.returncode:
            detail = cherry_pick.stderr.strip() or cherry_pick.stdout.strip() or f"exit {cherry_pick.returncode}"
            aborted = _git(repo, "cherry-pick", "--abort", check=False)
            if aborted.returncode:
                abort_detail = aborted.stderr.strip() or aborted.stdout.strip() or f"exit {aborted.returncode}"
                detail = f"{detail}; abort also failed: {abort_detail}"
            raise ConflictError(f"cherry-pick did not apply cleanly: {detail}")

    receipt = {
        "assignmentId": assignment_id,
        "commit": canonical,
        "targetCommit": _git(repo, "rev-parse", "HEAD").stdout.strip(),
        "alreadyReachable": already_reachable,
        "integratedAt": now(),
    }
    storage.atomic_write_json(receipt_path, receipt)
    storage.append_event(ship, _event(assignment_id, "assignment-integrated", commit=canonical))
    return receipt


@_captain_operation
def cleanup_assignment(ship: Path, assignment_id: str) -> dict:
    storage = Storage()
    ship = storage.resolve_ship(ship)
    directory, assignment = _load_assignment(storage, ship, assignment_id)
    events = _events(ship, assignment_id)
    if any(event.get("kind") == "assignment-cleaned" for event in events):
        return {"assignmentId": assignment_id, "cleaned": True, "worktreeRemoved": False}
    if assignment.get("repository") != "read" and not (directory / "integration.json").exists():
        raise ConflictError(f"assignment {assignment_id} must be integrated before cleanup")

    removed = False
    if assignment.get("repository") == "worktree":
        worktree = (assignment.get("runtime") or {}).get("worktreeDir")
        if worktree:
            worktree_path = Path(worktree).expanduser().resolve()
            repo = Path(assignment["repoDir"]).expanduser().resolve()
            if worktree_path == repo:
                raise ConflictError("refusing to remove the target repository as an assignment worktree")
            if worktree_path.exists():
                _git(repo, "worktree", "remove", str(worktree_path))
                removed = True
    storage.append_event(ship, _event(assignment_id, "assignment-cleaned"))
    return {"assignmentId": assignment_id, "cleaned": True, "worktreeRemoved": removed}
