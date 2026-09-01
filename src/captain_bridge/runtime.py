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
_AGENT_STATES = {"working", "idle", "blocked", "done", "unknown", "stale"}
_NOT_FOUND_CODES = {"agent_not_found", "agent-not-found", "not_found", "not-found"}


def _run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _json_error_code(text: str) -> str | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        return code if isinstance(code, str) else None
    code = payload.get("code")
    return code if isinstance(code, str) else None


def _herdr(args: list[str], *, optional: bool = False) -> dict[str, Any] | None:
    completed = _run(["herdr", *args])
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if optional and (
            _json_error_code(completed.stderr) in _NOT_FOUND_CODES
            or _json_error_code(completed.stdout) in _NOT_FOUND_CODES
        ):
            return None
        raise OperationError(f"Herdr command failed: {detail or completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise OperationError("Herdr returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise OperationError("Herdr returned invalid JSON")
    if "error" in payload:
        error = payload["error"]
        code = error.get("code") if isinstance(error, dict) else None
        if optional and code in _NOT_FOUND_CODES:
            return None
        detail = (
            error.get("message") or code or "unknown error"
            if isinstance(error, dict)
            else str(error)
        )
        raise OperationError(f"Herdr command failed: {detail}")
    result = payload.get("result", payload)
    if not isinstance(result, dict):
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


def _repo_dir(ship_dir: Path, assignment: dict[str, Any]) -> Path:
    try:
        metadata = json.loads((ship_dir / "metadata.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise NotFoundError(f"ship metadata not found: {ship_dir}") from error
    except json.JSONDecodeError as error:
        raise ValidationError("ship metadata is invalid JSON") from error
    raw = _field(metadata, "repoDir") if isinstance(metadata, dict) else None
    if not isinstance(raw, str) or not raw:
        raise ValidationError("ship metadata.repoDir is required")
    ship_repo = Path(raw).expanduser().resolve()
    assignment_raw = assignment.get("repoDir", assignment.get("repo_dir"))
    if not isinstance(assignment_raw, str) or not assignment_raw:
        raise ValidationError("assignment.repoDir is required")
    assignment_repo = Path(assignment_raw).expanduser().resolve()
    if assignment_repo != ship_repo:
        raise ConflictError(
            f"assignment repoDir does not match ship metadata.repoDir: {assignment_repo} != {ship_repo}"
        )
    if not assignment_repo.is_dir():
        raise NotFoundError(f"ship repository not found: {assignment_repo}")
    return assignment_repo


def _canonical_worktree(repo: Path, assignment_id: str, repository_mode: str) -> Path:
    if repository_mode == "read":
        return repo
    return repo.parent / ".captain-bridge-worktrees" / assignment_id


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
def _teardown_launch(repo: Path, pane_id: str | None, worktree: Path | None) -> None:
    failures: list[str] = []
    if pane_id:
        try:
            result = _run(["herdr", "pane", "close", pane_id])
            if result.returncode:
                detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
                failures.append(f"pane close failed: {detail}")
        except Exception as error:
            failures.append(f"pane close raised {error}")
    if worktree is not None and worktree.exists():
        try:
            result = _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo)
            if result.returncode:
                detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
                failures.append(f"worktree removal failed: {detail}")
        except Exception as error:
            failures.append(f"worktree removal raised {error}")
    if failures:
        raise OperationError("; ".join(failures))



def launch_assignment(
    ship_dir: str | Path,
    assignment: dict[str, Any],
    role: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del role
    ship = Path(ship_dir).expanduser().resolve()
    assignment_id = validate_id(str(_field(assignment, "id", "assignment_id", "assignmentId") or ""), "assignment")
    agent_name = _agent_name(assignment_id)
    repository_mode = assignment.get("repository")
    if repository_mode not in {"read", "worktree"}:
        raise ValidationError(f"invalid assignment repository mode: {repository_mode!r}")
    repo = _repo_dir(ship, assignment)
    canonical_worktree = _canonical_worktree(repo, assignment_id, repository_mode)

    existing = _facts(assignment)
    if any(existing.values()):
        if not all(isinstance(existing[key], str) and existing[key] for key in _FACT_KEYS):
            raise ConflictError("assignment has partial launch facts")
        if existing["agentName"] != agent_name:
            raise ConflictError("assignment launch facts have a non-canonical agent name")
        live = _agent(_herdr(["agent", "get", agent_name], optional=True))
        live_name = _field(live or {}, "name", "agentName", "agent_name")
        live_pane = _field(live or {}, "pane_id", "paneId")
        if not live or live_name != agent_name or live_pane != existing["paneId"]:
            raise ConflictError("assignment launch facts are not live")
        return existing
    live = _agent(_herdr(["agent", "get", agent_name], optional=True))
    if live:
        raise ConflictError(
            f"live Herdr agent exists without persisted launch facts: {agent_name}"
        )
    if repository_mode == "worktree" and canonical_worktree.exists():
        raise ConflictError(
            f"partial launch found worktree without matching agent: {canonical_worktree}"
        )

    prompt = _prompt_path(ship, assignment, assignment_id).read_text(encoding="utf-8")
    worktree: Path | None = None
    pane_id: str | None = None
    try:
        worktree = repo if repository_mode == "read" else _create_worktree(repo, assignment_id)
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
        if assignment.get("model"):
            native.extend(["--model", str(assignment["model"])])
        if assignment.get("effort"):
            native.extend(["--thinking", str(assignment["effort"])])
        if native:
            start_args.extend(["--", *native])
        _herdr(start_args)
        _herdr(["agent", "prompt", agent_name, prompt])
    except Exception as original:
        try:
            _teardown_launch(repo, pane_id, worktree if repository_mode == "worktree" else None)
        except Exception as cleanup:
            raise OperationError(
                f"launch failed: {original}; cleanup failed: {cleanup}"
            ) from original
        raise
    return {
        "agentName": agent_name,
        "paneId": pane_id,
        "worktreeDir": str(worktree),
        "launchedAt": now(),
    }
def observe_assignment(ship_dir: str | Path, assignment: dict[str, Any]) -> dict[str, Any]:
    del ship_dir
    facts = _facts(assignment)
    raw_id = _field(assignment, "id", "assignment_id", "assignmentId")
    target = _agent_name(str(raw_id)) if raw_id else facts["agentName"]
    observed: dict[str, Any] = {"available": False, "status": "missing"}
    if facts["agentName"]:
        observed["agentName"] = facts["agentName"]
    if facts["paneId"]:
        observed["paneId"] = facts["paneId"]
    if raw_id and facts["agentName"] and facts["agentName"] != target:
        observed["status"] = "stale"
        return observed
    if not raw_id and (
        not isinstance(target, str) or not _AGENT_NAME.fullmatch(target)
    ):
        observed["status"] = "stale"
        return observed
    if not target:
        return observed

    live = _agent(_herdr(["agent", "get", target], optional=True))
    if not live:
        return observed
    live_name = _field(live, "name", "agentName", "agent_name")
    live_pane = _field(live, "pane_id", "paneId")
    if (
        (facts["agentName"] and facts["agentName"] != target)
        or (live_name is not None and live_name != target)
        or (facts["paneId"] and live_pane != facts["paneId"])
    ):
        observed["status"] = "stale"
        return observed

    observed["available"] = True
    observed["agentName"] = live_name or target
    if live_pane:
        observed["paneId"] = live_pane
    status = _field(live, "agent_status", "status") or "unknown"
    observed["status"] = status if status in _AGENT_STATES else "unknown"
    if live.get("revision") is not None:
        observed["revision"] = live["revision"]
    return observed


def _message_bound(binding: dict[str, Any], message: str) -> bool:
    target = _field(
        binding,
        "agentName",
        "agent_name",
        "CAPTAIN_BRIDGE_OFFICER_NAME",
        "paneId",
        "pane_id",
        "CAPTAIN_BRIDGE_OFFICER_ID",
    )
    if not isinstance(target, str) or not target:
        return False
    return _herdr(["agent", "prompt", target, message], optional=True) is not None


def message_crewmate(
    ship_dir: str | Path,
    assignment: dict[str, Any],
    message: str,
) -> bool:
    del ship_dir
    if not isinstance(message, str) or not message.strip():
        raise ValidationError("crewmate message is required")
    facts = _facts(assignment)
    raw_id = _field(assignment, "id", "assignment_id", "assignmentId")
    if not raw_id or not facts["agentName"] or not facts["paneId"]:
        return False
    target = _agent_name(str(raw_id))
    if facts["agentName"] != target:
        return False
    live = _agent(_herdr(["agent", "get", target], optional=True))
    if not live:
        return False
    live_name = _field(live, "name", "agentName", "agent_name")
    live_pane = _field(live, "pane_id", "paneId")
    if (live_name is not None and live_name != target) or live_pane != facts["paneId"]:
        return False
    return _message_bound({"agentName": target}, message)

def wake_officer(ship_dir: str | Path, event: dict[str, Any]) -> bool:
    ship = Path(ship_dir).expanduser().resolve()
    try:
        officer = json.loads((ship / "officer.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    if not isinstance(officer, dict):
        return False
    return _message_bound(officer, json.dumps(event, sort_keys=True, separators=(",", ":")))
