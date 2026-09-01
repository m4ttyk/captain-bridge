from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable

READABLE_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
ID_KINDS = {"ship", "assignment", "decision", "memory", "event"}
DECISION_MODES = {"autonomous", "reviewable", "approval-required"}
CONFIDENCES = {"low", "medium", "high"}
PI_EVENT_KINDS = {
    "session-started",
    "agent-started",
    "agent-settled",
    "session-shutdown",
    "result-ready",
}
RESULT_SECTIONS = ("Outcome", "Commits", "Verification", "Findings", "Open questions")

_ID_RE = re.compile(r"^(ship|assignment|decision|memory|event)_([23456789abcdefghjkmnpqrstuvwxyz]{8})$")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_EVENT_KIND_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")


class CaptainError(Exception):
    """Expected user-facing failure."""

    exit_code = 1


class ValidationError(CaptainError):
    exit_code = 2


class NotFoundError(CaptainError):
    exit_code = 3


class ConflictError(CaptainError):
    exit_code = 4


class OperationError(CaptainError):
    exit_code = 5


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id(kind: str) -> str:
    if kind not in ID_KINDS:
        raise ValueError(f"unknown ID kind: {kind}")
    return f"{kind}_{''.join(secrets.choice(READABLE_ALPHABET) for _ in range(8))}"


def validate_id(value: str, kind: str | None = None) -> str:
    match = _ID_RE.fullmatch(value or "")
    if not match or (kind is not None and match.group(1) != kind):
        expected = f"{kind}_" if kind else "an entity prefix and "
        raise ValidationError(f"invalid ID {value!r}; expected {expected}eight readable characters")
    return value


def validate_slug(value: str) -> str:
    if not _SLUG_RE.fullmatch(value or ""):
        raise ValidationError(
            f"invalid slug {value!r}; use 1-64 lowercase letters, digits, and interior hyphens"
        )
    return value


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return validate_slug(slug)


def validate_event_kind(value: str, *, pi_only: bool = False) -> str:
    if not _EVENT_KIND_RE.fullmatch(value or ""):
        raise ValidationError(f"invalid event kind {value!r}")
    if pi_only and value not in PI_EVENT_KINDS:
        allowed = ", ".join(sorted(PI_EVENT_KINDS))
        raise ValidationError(f"unsupported adapter event kind {value!r}; expected one of: {allowed}")
    return value




def require_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    value = value.strip()
    if not value:
        raise ValidationError(f"{label} must not be empty")
    return value


def decision_mode(confidence: str, requested: str | None = None) -> str:
    """Validate an explicit decision mode.

    Confidence is evidence about a decision, not authority to select its mode.
    In particular, low-confidence reversible work can still be reviewable.
    """
    if confidence not in CONFIDENCES:
        raise ValidationError(f"invalid confidence {confidence!r}")
    if requested is None:
        raise ValidationError("decision mode must be explicit")
    if requested not in DECISION_MODES:
        raise ValidationError(f"invalid decision mode {requested!r}")
    return requested


def parse_result_sections(text: str) -> dict[str, str]:
    found: list[tuple[str, int]] = []
    canonical = {name.casefold(): name for name in RESULT_SECTIONS}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _HEADING_RE.fullmatch(line)
        if match and match.group(1).strip().casefold() in canonical:
            found.append((canonical[match.group(1).strip().casefold()], index))
    names = [item[0] for item in found]
    if names != list(RESULT_SECTIONS):
        raise ValidationError(
            "result.md must contain these headings once and in order: " + ", ".join(RESULT_SECTIONS)
        )
    sections: dict[str, str] = {}
    for offset, (name, line_index) in enumerate(found):
        end = found[offset + 1][1] if offset + 1 < len(found) else len(lines)
        sections[name] = "\n".join(lines[line_index + 1 : end]).strip()
    return sections


def derive_assignment_status(
    *,
    event_kinds: Iterable[str],
    has_result: bool,
    has_integration: bool,
    runtime: dict[str, Any] | None = None,
) -> str:
    """Derive status from durable facts, never a persisted runtime snapshot."""
    del runtime
    kinds = list(event_kinds)
    if "assignment-cleaned" in kinds:
        return "cleaned"
    if has_integration:
        return "integrated"
    if has_result or "result-ready" in kinds:
        return "result-ready"
    if "assignment-failed" in kinds:
        return "failed"
    if "assignment-launched" in kinds:
        return "launched"
    return "created"
