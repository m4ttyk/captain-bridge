from __future__ import annotations

import json
import os
import re
import subprocess
import time
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
_BINDING_KEYS = ("agentName", "paneId", "worktreeDir", "launchedAt")
_AGENT_STATES = {"working", "idle", "blocked", "done", "unknown", "stale"}
_NOT_FOUND_CODES = {"agent_not_found", "agent-not-found", "not_found", "not-found"}
_PANE_START_RETRY_DELAYS = (0.1, 0.25, 0.5, 1, 2)


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


class _HerdrCommandError(OperationError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _herdr(args: list[str], *, optional: bool = False) -> dict[str, Any] | None:
    completed = _run(["herdr", *args])
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        code = _json_error_code(completed.stderr) or _json_error_code(completed.stdout)
        if optional and code in _NOT_FOUND_CODES:
            return None
        raise _HerdrCommandError(
            f"Herdr command failed: {detail or completed.returncode}",
            code=code,
        )
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
        raise _HerdrCommandError(f"Herdr command failed: {detail}", code=code)
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise OperationError("Herdr returned an invalid result")
    return result

def _start_agent_when_pane_ready(start_args: list[str]) -> dict[str, Any] | None:
    for attempt in range(len(_PANE_START_RETRY_DELAYS) + 1):
        if attempt:
            time.sleep(_PANE_START_RETRY_DELAYS[attempt - 1])
        try:
            return _herdr(start_args)
        except _HerdrCommandError as error:
            if error.code != "agent_pane_busy" or attempt == len(_PANE_START_RETRY_DELAYS):
                raise
    raise AssertionError("unreachable")




def _field(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return value
    return None


def _binding(assignment: dict[str, Any]) -> dict[str, Any]:
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


def _parse_worktree_path(raw: str) -> str:
    if not raw.startswith('"'):
        return raw
    if len(raw) < 2 or not raw.endswith('"'):
        raise OperationError("git worktree list returned an invalid primary checkout")
    encoded = bytearray()
    index = 1
    escapes = {
        "\\": b"\\",
        '"': b'"',
        "a": b"\a",
        "b": b"\b",
        "t": b"\t",
        "n": b"\n",
        "v": b"\v",
        "f": b"\f",
        "r": b"\r",
    }
    while index < len(raw) - 1:
        char = raw[index]
        if char != "\\":
            encoded.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(raw) - 1:
            raise OperationError("git worktree list returned an invalid primary checkout")
        escaped = raw[index]
        if escaped in escapes:
            encoded.extend(escapes[escaped])
            index += 1
            continue
        if escaped not in "01234567":
            raise OperationError("git worktree list returned an invalid primary checkout")
        end = index + 1
        while end < len(raw) - 1 and end < index + 3 and raw[end] in "01234567":
            end += 1
        encoded.append(int(raw[index:end], 8))
        index = end
    try:
        return encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OperationError("git worktree list returned an invalid primary checkout") from error


def _primary_checkout(repo: Path) -> Path:
    listed = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    if listed.returncode:
        detail = listed.stderr.strip() or listed.stdout.strip() or f"exit {listed.returncode}"
        raise OperationError(f"could not list Git worktrees: {detail}")
    for line in listed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        raw = _parse_worktree_path(line[len("worktree ") :])
        if not raw or not Path(raw).is_absolute():
            raise OperationError("git worktree list returned an invalid primary checkout")
        return Path(raw).expanduser().resolve()
    raise OperationError("git worktree list returned no primary checkout")


def _ensure_worktree_ignored(primary: Path) -> None:
    git_dir = primary / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise OperationError(f"primary checkout has no usable .git directory: {git_dir}")
    info_dir = git_dir / "info"
    if info_dir.is_symlink():
        raise OperationError(f"primary checkout Git info directory is a symlink: {info_dir}")
    try:
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude = info_dir / "exclude"
        if exclude.is_symlink():
            raise OperationError(f"primary checkout Git exclude file is a symlink: {exclude}")
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if any(
            line.strip() in {".worktrees", ".worktrees/", "/.worktrees", "/.worktrees/"}
            for line in current.splitlines()
        ):
            return
        separator = "" if not current or current.endswith("\n") else "\n"
        exclude.write_text(f"{current}{separator}/.worktrees/\n", encoding="utf-8")
    except OperationError:
        raise
    except (OSError, UnicodeError) as error:
        raise OperationError(f"could not update local Git excludes: {error}") from error


def _canonical_worktree(repo: Path, assignment_id: str, repository_mode: str) -> Path:
    validate_id(assignment_id, "assignment")
    if repository_mode == "read":
        return repo
    if repository_mode != "worktree":
        raise ValidationError(f"invalid assignment repository mode: {repository_mode!r}")
    primary = _primary_checkout(repo)
    return primary / ".worktrees" / assignment_id


def _prompt_path(ship_dir: Path, assignment: dict[str, Any], assignment_id: str) -> Path:
    raw = _field(assignment, "prompt_path", "promptPath")
    path = Path(raw).expanduser() if isinstance(raw, str) and raw else ship_dir / "assignments" / assignment_id / "prompt.md"
    path = path.resolve()
    if not path.is_file():
        raise NotFoundError(f"assignment prompt not found: {path}")
    return path


def _read_persisted_officer(ship_dir: Path) -> dict[str, Any]:
    try:
        loaded = json.loads((ship_dir / "officer.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _persisted_officer_target(officer: dict[str, Any]) -> Any:
    return _field(
        officer,
        "agentName",
        "agent_name",
        "CAPTAIN_BRIDGE_OFFICER_NAME",
        "paneId",
        "pane_id",
        "CAPTAIN_BRIDGE_OFFICER_ID",
    )


def _environment_officer_target() -> Any:
    return _field(os.environ, "CAPTAIN_BRIDGE_OFFICER_NAME", "CAPTAIN_BRIDGE_OFFICER_ID")


def _current_pane_target(current: dict[str, Any]) -> Any:
    pane = current.get("pane") if isinstance(current.get("pane"), dict) else current
    return _field(pane, "name", "pane_id", "paneId")

def _current_workspace_id(current: dict[str, Any]) -> str:
    workspace = current.get("workspace")
    if isinstance(workspace, str) and workspace:
        return workspace
    sources = [current]
    pane = current.get("pane")
    if isinstance(pane, dict):
        sources.append(pane)
    if isinstance(workspace, dict):
        sources.append(workspace)
    for source in sources:
        value = _field(source, "workspace_id", "workspaceId")
        if isinstance(value, str) and value:
            return value
    raise OperationError("current Officer workspace could not be identified")

def _officer_target(ship_dir: Path, current: dict[str, Any]) -> str:
    target = (
        _persisted_officer_target(_read_persisted_officer(ship_dir))
        or _environment_officer_target()
        or _current_pane_target(current)
    )
    if not isinstance(target, str) or not target:
        raise OperationError("current Officer could not be identified")
    return target


def _create_worktree(
    repo: Path,
    assignment_id: str,
    canonical_worktree: Path | None = None,
) -> Path:
    worktree = canonical_worktree or _canonical_worktree(repo, assignment_id, "worktree")
    root = worktree.parent
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ConflictError(f"assignment worktree root is not a directory: {root}")
    if os.path.lexists(worktree):
        raise ConflictError(f"partial launch already created worktree: {worktree}")
    root.mkdir(parents=True, exist_ok=True)
    _ensure_worktree_ignored(worktree.parent.parent)
    branch = f"captain/{assignment_id}"
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


def _teardown_launch(repo: Path, tab_id: str | None, worktree: Path | None) -> None:
    failures: list[str] = []
    if tab_id:
        try:
            result = _run(["herdr", "tab", "close", tab_id])
            if result.returncode:
                detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
                failures.append(f"tab close failed: {detail}")
        except Exception as error:
            failures.append(f"tab close raised {error}")
    if worktree is not None and os.path.lexists(worktree):
        try:
            if worktree.is_symlink() or worktree.parent.is_symlink():
                failures.append(f"refusing to remove unsafe assignment worktree: {worktree}")
            else:
                result = _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo)
                if result.returncode:
                    detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
                    failures.append(f"worktree removal failed: {detail}")
        except Exception as error:
            failures.append(f"worktree removal raised {error}")
    if failures:
        raise OperationError("; ".join(failures))



def _validate_existing_binding(binding: dict[str, Any], agent_name: str) -> dict[str, Any]:
    if not all(isinstance(binding[key], str) and binding[key] for key in _BINDING_KEYS):
        raise ConflictError("assignment has partial launch facts")
    if binding["agentName"] != agent_name:
        raise ConflictError("assignment launch facts have a non-canonical agent name")
    live = _agent(_herdr(["agent", "get", agent_name], optional=True))
    live_name = _field(live or {}, "name", "agentName", "agent_name")
    live_pane = _field(live or {}, "pane_id", "paneId")
    if not live or live_name != agent_name or live_pane != binding["paneId"]:
        raise ConflictError("assignment launch facts are not live")
    return binding


def _start_fresh_resources(
    ship: Path,
    assignment: dict[str, Any],
    assignment_id: str,
    agent_name: str,
    repo: Path,
    repository_mode: str,
) -> dict[str, Any]:
    live = _agent(_herdr(["agent", "get", agent_name], optional=True))
    if live:
        raise ConflictError(
            f"live Herdr agent exists without persisted launch facts: {agent_name}"
        )
    canonical_worktree = _canonical_worktree(repo, assignment_id, repository_mode)
    if repository_mode == "worktree" and canonical_worktree.exists():
        raise ConflictError(
            f"partial launch found worktree without matching agent: {canonical_worktree}"
        )

    prompt = _prompt_path(ship, assignment, assignment_id).read_text(encoding="utf-8")
    worktree: Path | None = None
    tab_id: str | None = None
    pane_id: str | None = None
    try:
        worktree = repo if repository_mode == "read" else _create_worktree(
            repo, assignment_id, canonical_worktree
        )
        current = _herdr(["pane", "current", "--current"])
        assert current is not None
        officer = _officer_target(ship, current)

        created = _herdr([
            "tab",
            "create",
            "--workspace",
            _current_workspace_id(current),
            "--cwd",
            str(worktree),
            "--label",
            assignment_id,
            "--env",
            f"CAPTAIN_BRIDGE_SHIP={ship}",
            "--env",
            f"CAPTAIN_BRIDGE_ASSIGNMENT={assignment_id}",
            "--env",
            f"CAPTAIN_BRIDGE_OFFICER={officer}",
            "--no-focus",
        ])
        assert created is not None
        tab = created.get("tab")
        tab_id = _field(tab, "tab_id", "tabId") if isinstance(tab, dict) else None
        root_pane = created.get("root_pane")
        pane_id = _field(root_pane, "pane_id", "paneId") if isinstance(root_pane, dict) else None
        if not isinstance(tab_id, str) or not tab_id:
            raise OperationError("Herdr tab create did not return a tab ID")
        if not isinstance(pane_id, str) or not pane_id:
            raise OperationError("Herdr tab create did not return a root pane ID")

        start_args = ["agent", "start", agent_name, "--kind", "omp", "--pane", pane_id]
        agent_options: list[str] = ["--cwd", str(worktree)]
        if assignment.get("model"):
            agent_options.extend(["--model", str(assignment["model"])])
        if assignment.get("effort"):
            agent_options.extend(["--thinking", str(assignment["effort"])])
        if agent_options:
            start_args.extend(["--", *agent_options])
        _start_agent_when_pane_ready(start_args)
        _herdr(["agent", "prompt", agent_name, prompt])
    except Exception as original:
        try:
            _teardown_launch(repo, tab_id, worktree if repository_mode == "worktree" else None)
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


def launch_assignment(
    ship_dir: str | Path,
    assignment: dict[str, Any],
) -> dict[str, Any]:
    ship = Path(ship_dir).expanduser().resolve()
    assignment_id = validate_id(str(_field(assignment, "id", "assignment_id", "assignmentId") or ""), "assignment")
    agent_name = _agent_name(assignment_id)
    repository_mode = assignment.get("repository")
    if repository_mode not in {"read", "worktree"}:
        raise ValidationError(f"invalid assignment repository mode: {repository_mode!r}")
    repo = _repo_dir(ship, assignment)
    binding = _binding(assignment)
    if any(binding.values()):
        return _validate_existing_binding(binding, agent_name)
    return _start_fresh_resources(
        ship,
        assignment,
        assignment_id,
        agent_name,
        repo,
        repository_mode,
    )


def observe_assignment(ship_dir: str | Path, assignment: dict[str, Any]) -> dict[str, Any]:
    del ship_dir
    binding = _binding(assignment)
    raw_id = _field(assignment, "id", "assignment_id", "assignmentId")
    target = _agent_name(str(raw_id)) if raw_id else binding["agentName"]
    observed: dict[str, Any] = {"available": False, "status": "missing"}
    if binding["agentName"]:
        observed["agentName"] = binding["agentName"]
    if binding["paneId"]:
        observed["paneId"] = binding["paneId"]
    if raw_id and binding["agentName"] and binding["agentName"] != target:
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
        (binding["agentName"] and binding["agentName"] != target)
        or (live_name is not None and live_name != target)
        or (binding["paneId"] and live_pane != binding["paneId"])
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


def _send_prompt(binding: dict[str, Any], message: str) -> bool:
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
    binding = _binding(assignment)
    raw_id = _field(assignment, "id", "assignment_id", "assignmentId")
    if not raw_id or not binding["agentName"] or not binding["paneId"]:
        return False
    target = _agent_name(str(raw_id))
    if binding["agentName"] != target:
        return False
    live = _agent(_herdr(["agent", "get", target], optional=True))
    if not live:
        return False
    live_name = _field(live, "name", "agentName", "agent_name")
    live_pane = _field(live, "pane_id", "paneId")
    if (live_name is not None and live_name != target) or live_pane != binding["paneId"]:
        return False
    return _send_prompt({"agentName": target}, message)


def wake_officer(ship_dir: str | Path, event: dict[str, Any]) -> bool:
    ship = Path(ship_dir).expanduser().resolve()
    officer = _read_persisted_officer(ship)
    if not officer:
        return False
    return _send_prompt(officer, json.dumps(event, sort_keys=True, separators=(",", ":")))
