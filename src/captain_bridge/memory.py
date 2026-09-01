from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from .domain import (
    ConflictError,
    NotFoundError,
    OperationError,
    ValidationError,
    new_id,
    now,
    require_text,
    slugify,
    validate_id,
)
from .storage import Storage

_HEADINGS = (
    ("Symptom", "symptom"),
    ("Context", "context"),
    ("Cause", "cause"),
    ("Workaround", "workaround"),
    ("Evidence", "evidence"),
    ("Follow-up", "followUp"),
)


def _required(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    return require_text(value, label)


def _memory_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("memory ID must be text")
    return validate_id(value, "memory")


def _storage(home: str | Path | None) -> Storage:
    return Storage(home)


def _memory_dir(storage: Storage) -> Path:
    return storage.home_dir / "memory"


def _render(record: dict[str, Any]) -> str:
    frontmatter = [
        "+++",
        f"id = {json.dumps(record['id'], ensure_ascii=False)}",
        f"title = {json.dumps(record['title'], ensure_ascii=False)}",
        f"area = {json.dumps(record['area'], ensure_ascii=False)}",
        f"status = {json.dumps(record['status'], ensure_ascii=False)}",
        f"createdAt = {json.dumps(record['createdAt'], ensure_ascii=False)}",
    ]
    if record.get("supersedes") is not None:
        frontmatter.append(f"supersedes = {json.dumps(record['supersedes'], ensure_ascii=False)}")
    frontmatter.extend(("+++", ""))
    for heading, key in _HEADINGS:
        frontmatter.extend((f"## {heading}", record[key], ""))
    return "\n".join(frontmatter)


def _read_path(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise NotFoundError(f"memory not found: {path.stem}") from exc
    except (OSError, UnicodeError) as exc:
        raise OperationError(f"cannot read memory: {path}") from exc

    lines = text.splitlines()
    if not lines or lines[0] != "+++":
        raise OperationError(f"invalid memory frontmatter: {path}")
    try:
        frontmatter_end = lines.index("+++", 1)
        metadata = tomllib.loads("\n".join(lines[1:frontmatter_end]))
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        raise OperationError(f"invalid memory frontmatter: {path}") from exc

    found: list[tuple[str, str, int]] = []
    names = {heading: key for heading, key in _HEADINGS}
    for index, line in enumerate(lines[frontmatter_end + 1 :], frontmatter_end + 1):
        if line.startswith("## ") and line[3:] in names:
            heading = line[3:]
            found.append((heading, names[heading], index))
    if [heading for heading, _, _ in found] != [heading for heading, _ in _HEADINGS]:
        raise OperationError(f"memory headings are missing, duplicated, or out of order: {path}")

    sections: dict[str, str] = {}
    for offset, (_, key, start) in enumerate(found):
        end = found[offset + 1][2] if offset + 1 < len(found) else len(lines)
        value = "\n".join(lines[start + 1 : end]).strip()
        if not value:
            raise OperationError(f"memory section {key} is empty: {path}")
        sections[key] = value

    required_metadata = ("id", "title", "area", "status", "createdAt")
    if any(not isinstance(metadata.get(key), str) or not metadata[key] for key in required_metadata):
        raise OperationError(f"invalid memory metadata: {path}")
    if metadata["id"] != path.stem or metadata["status"] != "active":
        raise OperationError(f"invalid memory metadata: {path}")
    try:
        validate_id(metadata["id"], "memory")
        if "supersedes" in metadata:
            validate_id(metadata["supersedes"], "memory")
    except (TypeError, ValidationError) as exc:
        raise OperationError(f"invalid memory metadata: {path}") from exc

    return {
        "id": metadata["id"],
        "title": metadata["title"],
        "area": metadata["area"],
        "status": metadata["status"],
        "createdAt": metadata["createdAt"],
        "supersedes": metadata.get("supersedes"),
        **sections,
    }


def _all_memories(storage: Storage) -> list[dict[str, Any]]:
    directory = _memory_dir(storage)
    if not directory.exists():
        return []
    return [_read_path(path) for path in sorted(directory.glob("memory_*.md"))]


def _superseded_by(records: list[dict[str, Any]]) -> dict[str, str]:
    links: dict[str, str] = {}
    for record in sorted(records, key=lambda item: (item["createdAt"], item["id"])):
        if record["supersedes"] is not None:
            links[record["supersedes"]] = record["id"]
    return links


def record_memory(
    *,
    title: str,
    area: str,
    symptom: str,
    context: str,
    cause: str,
    workaround: str,
    evidence: str,
    follow_up: str,
    supersedes: str | None = None,
    home: str | Path | None = None,
) -> dict[str, Any]:
    storage = _storage(home)
    title = _required(title, "title")
    area = slugify(_required(area, "area"))
    sections = {
        "symptom": _required(symptom, "symptom"),
        "context": _required(context, "context"),
        "cause": _required(cause, "cause"),
        "workaround": _required(workaround, "workaround"),
        "evidence": _required(evidence, "evidence"),
        "followUp": _required(follow_up, "follow-up"),
    }
    if supersedes is not None:
        supersedes = _memory_id(supersedes)
        _read_path(_memory_dir(storage) / f"{supersedes}.md")

    memory_id = new_id("memory")
    path = _memory_dir(storage) / f"{memory_id}.md"
    if path.exists():
        raise ConflictError(f"memory already exists: {memory_id}")
    record: dict[str, Any] = {
        "id": memory_id,
        "title": title,
        "area": area,
        "status": "active",
        "createdAt": now(),
        "supersedes": supersedes,
        **sections,
    }
    storage.atomic_write_text(path, _render(record))
    return record


def search_memory(
    query: str = "",
    *,
    area: str | None = None,
    home: str | Path | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(query, str):
        raise ValidationError("query must be text")
    needle = query.strip().casefold()
    area = slugify(_required(area, "area")) if area is not None else None
    records = _all_memories(_storage(home))
    superseded = _superseded_by(records)
    results = [
        record
        for record in records
        if record["id"] not in superseded
        and (area is None or record["area"].casefold() == area.casefold())
        and (not needle or needle in _render(record).casefold())
    ]
    return sorted(results, key=lambda item: (item["createdAt"], item["id"]), reverse=True)


def inspect_memory(
    memory_id: str,
    *,
    home: str | Path | None = None,
) -> dict[str, Any]:
    storage = _storage(home)
    memory_id = _memory_id(memory_id)
    records = _all_memories(storage)
    record = next((item for item in records if item["id"] == memory_id), None)
    if record is None:
        raise NotFoundError(f"memory not found: {memory_id}")
    superseded_by = _superseded_by(records).get(memory_id)
    return {**record, **({"supersededBy": superseded_by} if superseded_by else {})}
