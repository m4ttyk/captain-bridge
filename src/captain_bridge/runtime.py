from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .domain import (
    ConflictError,
    NotFoundError,
    OperationError,
    ValidationError,
    now,
    validate_id,
)

_AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_FACT_KEYS = ("agentName", "paneId", "worktreeDir", "launchedAt")
_AGENT_STATES = {"working", "idle", "blocked", "done", "unknown"}


def _run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _herdr(args: list[str], *, optional: bool = False) -> dict[str, Any] | None:
    completed = _run(["herdr", *args])
    if completed.returncode:
        if optional:
            return None
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise OperationError(f"Herdr command failed: {detail or completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        if optional:
            return None
        raise OperationError("Herdr returned invalid JSON") from error
    if not isinstance(payload, dict):
        if optional:
            return None
        raise OperationError("Herdr returned invalid JSON")
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        if optional:
            return None
        raise OperationError("Herdr returned an invalid result")
    return result


def _field(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return value
    return None


def _facts(assignment: dict[str, Any]) -> dict[str, Any]:
    runtime = assignment.get("runtime")
    source = runtime if isinstance(runtime, dict) else assignment
    return {
        "agentName": _field(source, "agentName", "agent_name"),
        "paneId": _field(source, "paneId", "pane_id"),
        "worktreeDir": _field(source, "worktreeDir", "worktree", "worktree_dir"),
        "launchedAt": _field(source, "launchedAt", "launched_at"),
    }


def _agent(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not result:
        return None
    value = result.get("agent", result)
    return value if isinstance(value, dict) else None


def _agent_name(assignment_id: str) -> str:
    name = assignment_id.lower()
    if not _AGENT_NAME.fullmatch(name):
        raise ValidationError(f"assignment does not form a valid Herdr agent name: {assignment_id}")
    return name


def _repo_dir(ship_dir: Path) -> Path:
    try:
        metadata = json.loads((ship_dir / "metadata.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise NotFoundError(f"ship metadata not found: {ship_dir}") from error
    except json.JSONDecodeError as error:
        raise ValidationError("ship metadata is invalid JSON") from error
    raw = _field(metadata, "repoDir") if isinstance(metadata, dict) else None
    if not isinstance(raw, str) or not raw:
        raise ValidationError("ship metadata.repoDir is required")
    repo = Path(raw).expanduser().resolve()
    if not repo.is_dir():
        raise NotFoundError(f"ship repository not found: {repo}")
    return repo


def _prompt_path(ship_dir: Path, assignment: dict[str, Any], assignment_id: str) -> Path:
    raw = _field(assignment, "prompt_path", "promptPath")
    path = Path(raw).expanduser() if isinstance(raw, str) and raw else ship_dir / "assignments" / assignment_id / "prompt.md"
    path = path.resolve()
    if not path.is_file():
        raise NotFoundError(f"assignment prompt not found: {path}")
    return path


def _officer_target(ship_dir: Path, current: dict[str, Any]) -> str:
    officer: dict[str, Any] = {}
    try:
        loaded = json.loads((ship_dir / "officer.json").read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            officer = loaded
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    pane = current.get("pane") if isinstance(current.get("pane"), dict) else current
    target = _field(
        officer,
        "agentName",
        "agent_name",
        "CAPTAIN_BRIDGE_OFFICER_NAME",
        "paneId",
        "pane_id",
        "CAPTAIN_BRIDGE_OFFICER_ID",
    ) or _field(pane, "name", "pane_id", "paneId")
    if not isinstance(target, str) or not target:
        raise OperationError("current Officer could not be identified")
    return target


def _create_worktree(repo: Path, assignment_id: str) -> Path:
    worktree = repo.parent / ".captain-bridge-worktrees" / assignment_id
    branch = f"captain/{assignment_id}"
    if worktree.exists():
        raise ConflictError(f"partial launch already created worktree: {worktree}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    ref = _run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo)
    if ref.returncode not in (0, 1):
        raise OperationError(ref.stderr.strip() or "could not inspect assignment branch")
    args = ["git", "worktree", "add"]
    if ref.returncode == 1:
        args.extend(["-b", branch])
    args.extend([str(worktree), branch] if ref.returncode == 0 else [str(worktree)])
    created = _run(args, cwd=repo)
    if created.returncode:
        raise OperationError(created.stderr.strip() or "could not create assignment worktree")
    return worktree


def launch_assignment(
    ship_dir: str | Path,
    assignment: dict[str, Any],
    role: dict[str, Any],
) -> dict[str, Any]:
    ship = Path(ship_dir).expanduser().resolve()
    assignment_id = validate_id(str(_field(assignment, "id", "assignment_id", "assignmentId") or ""), "assignment")
    agent_name = _agent_name(assignment_id)

    existing = _facts(assignment)
    if any(existing.values()):
        if not all(existing[key] for key in _FACT_KEYS):
            raise ConflictError("assignment has partial launch facts")
        live = _agent(_herdr(["agent", "get", str(existing["agentName"])], optional=True))
        if not live or _field(live, "pane_id", "paneId") != existing["paneId"]:
            raise ConflictError("assignment launch facts are not live")
        return existing
    if _herdr(["agent", "get", agent_name], optional=True) is not None:
        raise ConflictError(f"Herdr agent already exists without launch facts: {agent_name}")

    repository_mode = role.get("repository")
    if repository_mode not in {"read", "worktree"}:
        raise ValidationError(f"invalid role repository mode: {repository_mode!r}")
    repo = _repo_dir(ship)
    worktree = repo if repository_mode == "read" else _create_worktree(repo, assignment_id)
    prompt = _prompt_path(ship, assignment, assignment_id).read_text(encoding="utf-8")

    current = _herdr(["pane", "current", "--current"])
    assert current is not None
    officer = _officer_target(ship, current)
    split = _herdr([
        "pane",
        "split",
        "--current",
        "--direction",
        "right",
        "--cwd",
        str(worktree),
        "--env",
        f"CAPTAIN_BRIDGE_SHIP={ship}",
        "--env",
        f"CAPTAIN_BRIDGE_ASSIGNMENT={assignment_id}",
        "--env",
        f"CAPTAIN_BRIDGE_OFFICER={officer}",
        "--no-focus",
    ])
    assert split is not None
    pane = split.get("pane") if isinstance(split.get("pane"), dict) else split
    pane_id = _field(pane, "pane_id", "paneId")
    if not isinstance(pane_id, str) or not pane_id:
        raise OperationError("Herdr split did not return a pane ID")

    start_args = ["agent", "start", agent_name, "--kind", "omp", "--pane", pane_id]
    native: list[str] = []
    if role.get("model"):
        native.extend(["--model", str(role["model"])])
    if role.get("effort"):
        native.extend(["--thinking", str(role["effort"])])
    if native:
        start_args.extend(["--", *native])
    _herdr(start_args)
    _herdr(["agent", "prompt", agent_name, prompt])
    return {
        "agentName": agent_name,
        "paneId": pane_id,
        "worktreeDir": str(worktree),
        "launchedAt": now(),
    }


def observe_assignment(ship_dir: str | Path, assignment: dict[str, Any]) -> dict[str, Any]:
    del ship_dir
    facts = _facts(assignment)
    target = facts["agentName"] or facts["paneId"]
    if not target:
        raw_id = _field(assignment, "id", "assignment_id", "assignmentId")
        target = _agent_name(str(raw_id)) if raw_id else None
    live = _agent(_herdr(["agent", "get", str(target)], optional=True)) if target else None
    observed: dict[str, Any] = {"available": bool(live), "status": "missing"}
    if facts["agentName"]:
        observed["agentName"] = facts["agentName"]
    if facts["paneId"]:
        observed["paneId"] = facts["paneId"]
    if not live:
        return observed
    observed["agentName"] = _field(live, "name") or facts["agentName"] or target
    observed["paneId"] = _field(live, "pane_id", "paneId") or facts["paneId"]
    status = _field(live, "agent_status", "status") or "unknown"
    observed["status"] = status if status in _AGENT_STATES else "unknown"
    if live.get("revision") is not None:
        observed["revision"] = live["revision"]
    return observed


def _message_bound(binding: dict[str, Any], message: str) -> bool:
    targets = [
        _field(binding, "agentName", "agent_name", "CAPTAIN_BRIDGE_OFFICER_NAME"),
        _field(binding, "paneId", "pane_id", "CAPTAIN_BRIDGE_OFFICER_ID"),
    ]
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, str) or not target or target in seen:
            continue
        seen.add(target)
        if _herdr(["agent", "prompt", target, message], optional=True) is not None:
            return True
    return False


def message_crewmate(
    ship_dir: str | Path,
    assignment: dict[str, Any],
    message: str,
) -> bool:
    del ship_dir
    if not isinstance(message, str) or not message.strip():
        raise ValidationError("crewmate message is required")
    return _message_bound(_facts(assignment), message)


def wake_officer(ship_dir: str | Path, event: dict[str, Any]) -> bool:
    ship = Path(ship_dir).expanduser().resolve()
    try:
        officer = json.loads((ship / "officer.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    if not isinstance(officer, dict):
        return False
    return _message_bound(officer, json.dumps(event, sort_keys=True, separators=(",", ":")))
