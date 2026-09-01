from __future__ import annotations

import json
import subprocess
import tomllib
from functools import wraps
from pathlib import Path
from typing import Any

from . import decisions, runtime
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


def _complete_runtime_facts(facts: Any, assignment_id: str) -> dict[str, Any]:
    if not isinstance(facts, dict):
        raise OperationError("runtime returned a non-object assignment binding")
    missing = [key for key in ("agentName", "paneId", "worktreeDir", "launchedAt") if not facts.get(key)]
    if missing:
        raise OperationError(f"runtime returned a partial assignment binding; missing: {', '.join(missing)}")
    return facts


def _canonical_worktree(assignment: dict[str, Any], assignment_id: str) -> Path:
    repo_dir = assignment.get("repoDir")
    if not isinstance(repo_dir, str) or not repo_dir:
        raise ValidationError("assignment repoDir is required")
    return (Path(repo_dir).expanduser().resolve().parent / ".captain-bridge-worktrees" / assignment_id).resolve()


def _safe_worktree(assignment: dict[str, Any], assignment_id: str, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ConflictError(f"assignment {assignment_id} has no safe worktree path")
    path = Path(raw).expanduser().resolve()
    if path != _canonical_worktree(assignment, assignment_id):
        raise ConflictError(f"assignment {assignment_id} worktree path is not assignment-owned")
    return path


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
def _assignment_repo(storage: Storage, ship: Path, assignment: dict[str, Any]) -> Path:
    metadata = storage.read_json(ship / "metadata.json")
    ship_raw = metadata.get("repoDir") if isinstance(metadata, dict) else None
    assignment_raw = assignment.get("repoDir")
    if not isinstance(ship_raw, str) or not ship_raw:
        raise ValidationError("ship metadata.repoDir is required")
    if not isinstance(assignment_raw, str) or not assignment_raw:
        raise ValidationError("assignment repoDir is required")
    ship_repo = Path(ship_raw).expanduser().resolve()
    assignment_repo = Path(assignment_raw).expanduser().resolve()
    if assignment_repo != ship_repo:
        raise ConflictError(
            f"assignment repoDir does not match ship metadata.repoDir: "
            f"{assignment_repo} != {ship_repo}"
        )
    return assignment_repo



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
    if directory.exists():
        raise ConflictError(f"assignment already exists: {assignment_id}")
    assignment = {
        "id": assignment_id,
        "createdAt": now(),
        "role": role_name,
        "repository": role["repository"],
        "model": role.get("model"),
        "effort": role.get("effort"),
        "repoDir": str(Path(repo_dir).expanduser().resolve()),
    }
    with storage.staged_directory(directory) as staging:
        storage.atomic_write_json(staging / "assignment.json", assignment)
        storage.atomic_write_text(
            staging / "prompt.md",
            f"{role_prompt}\n\n## Assignment\n\n{prompt}\n",
        )
        (staging / "artifacts").mkdir()
    storage.append_event(ship, _event(assignment_id, "assignment-created"))
    return assignment


@_captain_operation
def launch_assignment(ship: Path, assignment_id: str) -> dict:
    storage = Storage()
    ship = storage.resolve_ship(ship)
    directory, _ = _paths(ship, assignment_id)
    with storage.file_lock(directory / "launch.lock"):
        directory, assignment = _load_assignment(storage, ship, assignment_id)
        launch_path = directory / "launch.json"
        if launch_path.exists():
            intent = storage.read_json(launch_path)
            if not isinstance(intent, dict) or intent.get("assignmentId") != assignment_id:
                raise ConflictError(f"assignment {assignment_id} has an invalid launch intent")
        else:
            storage.exclusive_write_json(
                launch_path,
                {"assignmentId": assignment_id, "requestedAt": now()},
            )
        facts = _complete_runtime_facts(runtime.launch_assignment(ship, assignment), assignment_id)
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
    worktree = (assignment.get("runtime") or {}).get("worktreeDir")
    status = derive_assignment_status(
        event_kinds=(event.get("kind", "") for event in events),
        has_result=sections is not None,
        has_integration=integration is not None,
        runtime=observed,
    )
    return {
        "assignment": assignment,
        "status": status,
        "result": sections,
        "events": events,
        "pendingDecisions": decisions.pending_decisions(ship, assignment_id),
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
    with storage.file_lock((directory / "integration.lock").resolve()):
        commit = require_text(commit, "commit")
        if commit.startswith("-"):
            raise ValidationError("commit must not begin with '-'")
        repo = _assignment_repo(storage, ship, assignment)
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
    directory, _ = _paths(ship, assignment_id)
    with storage.file_lock((directory / "cleanup.lock").resolve()):
        directory, assignment = _load_assignment(storage, ship, assignment_id)
        repo = _assignment_repo(storage, ship, assignment)
        observed = runtime.observe_assignment(ship, assignment)
        if not isinstance(observed, dict):
            raise OperationError("runtime returned an invalid assignment observation")
        if observed.get("available") or observed.get("status") == "stale":
            raise ConflictError(f"assignment {assignment_id} still has live runtime resources")

        events = _events(ship, assignment_id)
        if any(event.get("kind") == "assignment-cleaned" for event in events):
            return {"assignmentId": assignment_id, "cleaned": True, "worktreeRemoved": False}
        if assignment.get("repository") != "read" and not (directory / "integration.json").exists():
            raise ConflictError(f"assignment {assignment_id} must be integrated before cleanup")

        removed = False
        if assignment.get("repository") == "worktree":
            binding = assignment.get("runtime")
            raw_worktree = binding.get("worktreeDir") if isinstance(binding, dict) else None
            worktree_path = (
                _safe_worktree(assignment, assignment_id, raw_worktree)
                if raw_worktree
                else _canonical_worktree(assignment, assignment_id)
            )
            if worktree_path.exists():
                if worktree_path == repo:
                    raise ConflictError("refusing to remove the target repository as an assignment worktree")
                _git(repo, "worktree", "remove", str(worktree_path))
                if worktree_path.exists():
                    raise ConflictError(f"assignment {assignment_id} worktree remains after cleanup")
                removed = True
        storage.append_event(ship, _event(assignment_id, "assignment-cleaned"))
        return {"assignmentId": assignment_id, "cleaned": True, "worktreeRemoved": removed}
