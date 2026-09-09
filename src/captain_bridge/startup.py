from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

from .domain import ConflictError, NotFoundError, OperationError, ValidationError
from .ships import create_ship, open_ship
from .storage import Storage




def _git_root(cwd: str | Path | None = None) -> Path:
    directory = Path(cwd or Path.cwd()).expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise OperationError(f"could not run git: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or "not inside a Git checkout"
        raise ValidationError(f"current directory is not a Git worktree: {detail}")
    root = result.stdout.strip()
    if not root:
        raise ValidationError("git did not return a checkout root")
    return Path(root).resolve()


def _herdr(args: list[str]) -> dict[str, Any] | None:
    try:
        result = subprocess.run(["herdr", *args], capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("result", payload)
    return value if isinstance(value, dict) else None


def _current_pane() -> dict[str, Any] | None:
    current = _herdr(["pane", "current", "--current"])
    if not current:
        return None
    pane = current.get("pane")
    return pane if isinstance(pane, dict) else current


def _field(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return value
    return None


def _live_officer_target(officer: dict[str, Any]) -> str | None:
    target = _field(officer, "agentName", "agent_name")
    if isinstance(target, str) and target and _herdr(["agent", "get", target]) is not None:
        return target

    pane_id = _field(officer, "paneId", "pane_id")
    if not isinstance(pane_id, str) or not pane_id:
        return None
    agents = _herdr(["agent", "list"])
    values = agents.get("agents") if isinstance(agents, dict) else None
    if not isinstance(values, list):
        return None
    for agent in values:
        if not isinstance(agent, dict) or _field(agent, "pane_id", "paneId") != pane_id:
            continue
        name = _field(agent, "name", "agentName", "agent_name")
        return name if isinstance(name, str) and name else pane_id
    return None


def _current_officer(pane: dict[str, Any] | None, persisted: dict[str, Any]) -> dict[str, str]:
    env_name = os.environ.get("CAPTAIN_BRIDGE_OFFICER_NAME")
    env_pane = os.environ.get("CAPTAIN_BRIDGE_OFFICER_ID")
    if env_name or env_pane:
        return {
            **({"agentName": env_name} if env_name else {}),
            **({"paneId": env_pane} if env_pane else {}),
        }

    pane_id = _field(pane or {}, "pane_id", "paneId")
    detected = _field(pane or {}, "agentName", "agent_name")
    status = _field(pane or {}, "agent_status", "status")
    if isinstance(pane_id, str) and pane_id:
        if isinstance(detected, str) and detected and status in {"working", "idle", "blocked"}:
            return {"agentName": detected, "paneId": pane_id}
        return {"paneId": pane_id}
    return {
        **({"agentName": str(persisted["agentName"])} if persisted.get("agentName") else {}),
        **({"paneId": str(persisted["paneId"])} if persisted.get("paneId") else {}),
    }


def _select_ship(root: Path, storage: Storage) -> Path:
    explicit = os.environ.get("CAPTAIN_BRIDGE_SHIP")
    if explicit:
        ship = storage.resolve_ship(explicit)
        metadata = storage.read_json(ship / "metadata.json")
        registered = metadata.get("repoDir") if isinstance(metadata, dict) else None
        if not isinstance(registered, str) or Path(registered).expanduser().resolve() != root:
            raise ConflictError(f"ship {ship} is not registered for checkout {root}")
        return ship

    try:
        return storage.resolve_ship_for_repository(root)
    except NotFoundError:
        slug = os.environ.get("CAPTAIN_BRIDGE_SHIP_SLUG", "default")
        return Path(create_ship(root, slug, storage=storage)["path"])


def _officer_prompt() -> str:
    path = Path(__file__).parent / "resources" / "officer.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OperationError(f"packaged Officer policy is unavailable: {path}") from exc
    if not text:
        raise OperationError(f"packaged Officer policy is empty: {path}")
    return text


def _merge_prompt(args: list[str], role_prompt: str, *, cwd: Path | None = None) -> list[str]:
    forwarded: list[str] = []
    custom: list[str] = []
    index = 0

    def append_value(value: str) -> None:
        candidate = Path(value).expanduser()
        if cwd is not None and not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            custom.append(candidate.read_text(encoding="utf-8") if candidate.is_file() else value)
        except OSError:
            custom.append(value)

    while index < len(args):
        value = args[index]
        if value == "--append-system-prompt":
            if index + 1 >= len(args):
                raise ValidationError("--append-system-prompt requires a value")
            append_value(args[index + 1])
            index += 2
            continue
        if value.startswith("--append-system-prompt="):
            append_value(value.split("=", 1)[1])
            index += 1
            continue
        if value == "--cwd":
            if index + 1 >= len(args):
                raise ValidationError("--cwd requires a value")
            index += 2
            continue
        if value.startswith("--cwd="):
            index += 1
            continue
        forwarded.append(value)
        index += 1

    prompt_parts = [role_prompt, *[part for part in custom if part]]
    forwarded.extend(["--append-system-prompt", "\n\n".join(prompt_parts)])
    return forwarded


def start(argv: list[str] | None = None) -> int:
    launch_cwd = Path.cwd().expanduser().resolve()
    root = _git_root(launch_cwd)
    storage = Storage()
    storage.ensure_defaults()
    ship = _select_ship(root, storage)
    persisted = storage.read_json(ship / "officer.json") if (ship / "officer.json").exists() else {}
    if not isinstance(persisted, dict):
        persisted = {}

    current_pane = _current_pane()
    live_target = _live_officer_target(persisted)
    identity = persisted if live_target else _current_officer(current_pane, persisted)
    opened = open_ship(ship, storage=storage, officer=identity)
    officer = opened.get("officer") if isinstance(opened.get("officer"), dict) else {}

    env = os.environ.copy()
    env["CAPTAIN_BRIDGE_SHIP"] = str(ship)

    def bind_environment() -> None:
        if officer.get("agentName"):
            env["CAPTAIN_BRIDGE_OFFICER_NAME"] = str(officer["agentName"])
        else:
            env.pop("CAPTAIN_BRIDGE_OFFICER_NAME", None)
        if officer.get("paneId"):
            env["CAPTAIN_BRIDGE_OFFICER_ID"] = str(officer["paneId"])
        else:
            env.pop("CAPTAIN_BRIDGE_OFFICER_ID", None)

    bind_environment()
    if live_target:
        try:
            attached = subprocess.run(
                ["herdr", "agent", "attach", live_target],
                cwd=root,
                env=env,
                check=False,
            )
        except OSError:
            attached = None
        if attached is not None and attached.returncode == 0:
            return 0

        identity = _current_officer(current_pane, {})
        opened = open_ship(ship, storage=storage, officer=identity)
        officer = opened.get("officer") if isinstance(opened.get("officer"), dict) else {}
        bind_environment()

    omp_args = _merge_prompt(list(argv or []), _officer_prompt(), cwd=launch_cwd)
    omp_args.extend(["--cwd", str(root)])
    try:
        return subprocess.run(["omp", *omp_args], cwd=root, env=env, check=False).returncode
    except OSError as exc:
        raise OperationError(f"could not start omp: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    return start(argv)
