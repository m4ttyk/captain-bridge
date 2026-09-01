from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from . import assignments, decisions, memory, runtime
from .domain import (
    CaptainError,
    NotFoundError,
    ValidationError,
    new_id,
    now,
    parse_result_sections,
    validate_event_kind,
    validate_id,
)
from .ships import create_ship, open_ship, reconcile
from .storage import Storage


def _ship(value: str | None) -> Path:
    return Storage().resolve_ship(value)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValidationError(message)


def _text(value: str | None, file: str | None, label: str, *, required: bool = True) -> str | None:
    if value is not None and file is not None:
        raise ValidationError(f"provide {label} or {label}-file, not both")
    if file is not None:
        try:
            value = Path(file).expanduser().read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise NotFoundError(f"{label} file not found: {file}") from exc
        except OSError as exc:
            raise ValidationError(f"cannot read {label} file: {file}") from exc
    if value is None:
        if required:
            raise ValidationError(f"{label} is required")
        return None
    value = value.strip()
    if required and not value:
        raise ValidationError(f"{label} must not be empty")
    return value


def _record_event(args: argparse.Namespace) -> dict[str, Any]:
    storage = Storage()
    ship = storage.resolve_ship(args.ship)
    kind = validate_event_kind(args.kind, pi_only=True)
    assignment_id = None
    if args.assignment is not None:
        assignment_id = validate_id(args.assignment, "assignment")
    if kind == "result-ready":
        if assignment_id is None:
            raise ValidationError("result-ready requires an assignment")
        result_path = ship / "assignments" / assignment_id / "result.md"
        try:
            result_text = result_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise NotFoundError(f"assignment result not found: {result_path}") from exc
        except OSError as exc:
            raise ValidationError(f"cannot read assignment result: {result_path}") from exc
        parse_result_sections(result_text)
    event = {
        "id": new_id("event"),
        "kind": kind,
        "at": now(),
        **({"assignmentId": assignment_id} if assignment_id else {}),
        **({"sessionId": args.session_id} if args.session_id else {}),
    }
    storage.append_event(ship, event)
    woken = False
    wake_error = None
    if kind == "result-ready":
        try:
            woken = bool(runtime.wake_officer(ship, event))
        except CaptainError as exc:
            wake_error = str(exc)
    result = {"event": event, "officerWoken": woken}
    if wake_error is not None:
        result["wakeError"] = wake_error
    return result


def _run_ship(args: argparse.Namespace) -> Any:
    if args.action == "create":
        repo = args.repo_opt or args.repo
        slug = args.slug_opt or args.slug
        if not repo or not slug:
            raise ValidationError("ship create requires repo and slug")
        return create_ship(repo, slug)
    if args.action == "open":
        return open_ship(args.path or args.ship)
    if args.action == "reconcile":
        return reconcile(args.ship)
    raise ValidationError(f"unknown ship action: {args.action}")


def _run_assignment(args: argparse.Namespace) -> Any:
    ship = _ship(args.ship)
    if args.action == "create":
        return assignments.create_assignment(
            ship,
            role_name=args.role,
            prompt=_text(args.prompt, args.prompt_file, "prompt") or "",
        )
    if args.action == "launch":
        return assignments.launch_assignment(ship, args.assignment_id)
    if args.action == "inspect":
        return assignments.inspect_assignment(ship, args.assignment_id)
    if args.action == "message":
        return assignments.message_assignment(
            ship,
            args.assignment_id,
            _text(args.message, args.message_file, "message") or "",
        )
    if args.action == "integrate":
        return assignments.integrate_assignment(
            ship,
            args.assignment_id,
            _text(args.commit_opt or args.commit, args.commit_file, "commit") or "",
        )
    if args.action == "cleanup":
        return assignments.cleanup_assignment(ship, args.assignment_id)
    raise ValidationError(f"unknown assignment action: {args.action}")


def _run_decision(args: argparse.Namespace) -> Any:
    ship = _ship(args.ship)
    if args.action == "request":
        return decisions.request_decision(
            ship,
            question=_text(args.question, args.question_file, "question") or "",
            mode=args.mode,
            confidence=args.confidence,
            assignment_id=args.assignment_id,
            affected_assignments=args.affected_assignments or [],
            blocks_further_dependent_work=args.blocks_further_dependent_work,
            impact=_text(args.impact, args.impact_file, "impact", required=False),
            supersedes=args.supersedes,
        )
    if args.action == "resolve":
        return decisions.resolve_decision(
            ship,
            args.decision_id,
            answer=_text(args.answer, args.answer_file, "answer") or "",
            resolved_by=args.resolved_by,
            rationale=_text(args.rationale, args.rationale_file, "rationale") or "",
        )
    if args.action == "review":
        return decisions.review_decision(
            ship,
            args.decision_id,
            note=_text(args.note, args.note_file, "review note", required=False),
        )
    raise ValidationError(f"unknown decision action: {args.action}")


def _run_memory(args: argparse.Namespace) -> Any:
    if args.action == "record":
        values = {
            name: _text(getattr(args, name), getattr(args, f"{name}_file"), label) or ""
            for name, label in (
                ("title", "title"),
                ("symptom", "symptom"),
                ("context", "context"),
                ("cause", "cause"),
                ("workaround", "workaround"),
                ("evidence", "evidence"),
                ("follow_up", "follow-up"),
            )
        }
        return memory.record_memory(area=args.area, supersedes=args.supersedes, home=None, **values)
    if args.action == "search":
        return memory.search_memory(args.query or "", area=args.area)
    if args.action == "inspect":
        return memory.inspect_memory(args.memory_id)
    raise ValidationError(f"unknown memory action: {args.action}")


def _run_event(args: argparse.Namespace) -> Any:
    if args.action == "emit":
        return _record_event(args)
    raise ValidationError(f"unknown event action: {args.action}")


def _run(args: argparse.Namespace) -> Any:
    if args.group == "ship":
        return _run_ship(args)
    if args.group == "assignment":
        return _run_assignment(args)
    if args.group == "decision":
        return _run_decision(args)
    if args.group == "memory":
        return _run_memory(args)
    if args.group == "_event":
        return _run_event(args)
    raise ValidationError(f"unknown command group: {args.group}")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="captain-bridge")
    groups = parser.add_subparsers(dest="group", required=True)

    ship = groups.add_parser("ship")
    ship_sub = ship.add_subparsers(dest="action", required=True)
    create = ship_sub.add_parser("create")
    create.add_argument("repo", nargs="?")
    create.add_argument("slug", nargs="?")
    create.add_argument("--repo", "--repo-dir", dest="repo_opt")
    create.add_argument("--slug", dest="slug_opt")
    open_cmd = ship_sub.add_parser("open")
    open_cmd.add_argument("path", nargs="?")
    open_cmd.add_argument("--ship")
    reconcile_cmd = ship_sub.add_parser("reconcile")
    reconcile_cmd.add_argument("--ship")

    assignment = groups.add_parser("assignment")
    assignment_sub = assignment.add_subparsers(dest="action", required=True)
    ac = assignment_sub.add_parser("create")
    ac.add_argument("--ship")
    ac.add_argument("--role", "--role-name", dest="role", required=True)
    ac.add_argument("--prompt")
    ac.add_argument("--prompt-file")
    for action in ("launch", "inspect", "cleanup"):
        cmd = assignment_sub.add_parser(action)
        cmd.add_argument("assignment_id")
        cmd.add_argument("--ship")
    msg = assignment_sub.add_parser("message")
    msg.add_argument("assignment_id")
    msg.add_argument("--ship")
    msg.add_argument("--message")
    msg.add_argument("--message-file")
    integ = assignment_sub.add_parser("integrate")
    integ.add_argument("assignment_id")
    integ.add_argument("commit", nargs="?")
    integ.add_argument("--commit", dest="commit_opt")
    integ.add_argument("--commit-file")
    integ.add_argument("--ship")

    decision = groups.add_parser("decision")
    decision_sub = decision.add_subparsers(dest="action", required=True)
    dr = decision_sub.add_parser("request")
    dr.add_argument("--ship")
    dr.add_argument("--question")
    dr.add_argument("--question-file")
    dr.add_argument("--mode", required=True, choices=("autonomous", "reviewable", "approval-required"))
    dr.add_argument("--confidence", required=True, choices=("low", "medium", "high"))
    dr.add_argument("--assignment-id")
    dr.add_argument("--affected-assignment", dest="affected_assignments", action="append")
    dr.add_argument("--blocks-further-dependent-work", action="store_true")
    dr.add_argument("--impact")
    dr.add_argument("--impact-file")
    dr.add_argument("--supersedes")
    dres = decision_sub.add_parser("resolve")
    dres.add_argument("decision_id")
    dres.add_argument("--ship")
    dres.add_argument("--answer")
    dres.add_argument("--answer-file")
    dres.add_argument("--resolved-by", required=True)
    dres.add_argument("--rationale")
    dres.add_argument("--rationale-file")
    drev = decision_sub.add_parser("review")
    drev.add_argument("decision_id")
    drev.add_argument("--ship")
    drev.add_argument("--note")
    drev.add_argument("--note-file")

    mem = groups.add_parser("memory")
    mem_sub = mem.add_subparsers(dest="action", required=True)
    mr = mem_sub.add_parser("record")
    mr.add_argument("--title")
    mr.add_argument("--title-file")
    mr.add_argument("--area", required=True)
    for name in ("symptom", "context", "cause", "workaround", "evidence", "follow-up"):
        dest = name.replace("-", "_")
        mr.add_argument(f"--{name}", dest=dest)
        mr.add_argument(f"--{name}-file", dest=f"{dest}_file")
    mr.add_argument("--supersedes")
    ms = mem_sub.add_parser("search")
    ms.add_argument("query", nargs="?")
    ms.add_argument("--area")
    mi = mem_sub.add_parser("inspect")
    mi.add_argument("memory_id")

    event = groups.add_parser("_event", help=argparse.SUPPRESS)
    event_sub = event.add_subparsers(dest="action", required=True)
    emit = event_sub.add_parser("emit", help=argparse.SUPPRESS)
    emit.add_argument("--ship")
    emit.add_argument("--kind", required=True)
    emit.add_argument("--assignment", "--assignment-id", dest="assignment")
    emit.add_argument("--session-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        result = _run(args)
    except CaptainError as exc:
        print(
            json.dumps(
                {"error": {"code": exc.exit_code, "message": str(exc)}},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return exc.exit_code
    except Exception as exc:
        print(
            json.dumps(
                {"error": {"code": 5, "message": f"operation failed: {exc}"}},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 5
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
