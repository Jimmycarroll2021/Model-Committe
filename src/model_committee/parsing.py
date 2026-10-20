"""Pull the structured JSON block out of free-form model output."""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)

_FENCE_LINE = re.compile(r"^[ \t]*```[ \t]*([\w+-]*)[ \t]*$")


def _fenced_blocks(text: str) -> list[tuple[int, str, str]]:
    """(start offset, language, body) for each ``` block, pairing fences in order so a closing fence of one
    block can never be mistaken for the opener of the next."""
    blocks, offset, open_at, lang, body = [], 0, None, "", []
    for line in text.splitlines(keepends=True):
        m = _FENCE_LINE.match(line.rstrip("\r\n"))
        if m and open_at is None:
            open_at, lang, body = offset, m.group(1).lower(), []
        elif m and not m.group(1):
            blocks.append((open_at, lang, "".join(body).rstrip("\r\n")))
            open_at = None
        elif open_at is not None:
            body.append(line)
        offset += len(line)
    return blocks


def _json_blocks(text: str) -> list[tuple[int, str]]:
    return [(start, body) for start, lang, body in _fenced_blocks(text) if lang in ("", "json")]


def _balanced_objects(text: str) -> list[str]:
    """Every top-level {...} span in text, respecting strings, in order of appearance."""
    spans, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and depth > 0:
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append(text[start : i + 1])
    return spans


def _loads_lenient(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Common model slip: trailing commas before ] or }.
        return json.loads(re.sub(r",\s*([\]}])", r"\1", raw))


def _candidates(text: str) -> list[object]:
    """Parseable JSON objects, best first: fenced blocks newest-first, then bare objects newest-first."""
    raws = [body.strip() for _, body in reversed(_json_blocks(text))]
    raws += list(reversed(_balanced_objects(text)))
    out = []
    for raw in raws:
        if not raw.startswith("{"):
            continue
        try:
            out.append(_loads_lenient(raw))
        except json.JSONDecodeError:
            continue
    return out


def extract_json(text: str) -> object:
    """Return the best JSON object in text (last fenced block, else last bare object). Raises ValueError."""
    found = _candidates(text)
    if not found:
        raise ValueError("no parseable JSON object found in model output")
    return found[0]


def parse_model(text: str, model: type[M]) -> M:
    """First candidate that validates against `model`. Raises ValueError fit to show the model on retry."""
    found = _candidates(text)
    if not found:
        raise ValueError("no parseable JSON object found in model output")
    first_error: ValidationError | None = None
    for data in found:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            first_error = first_error or exc
    raise ValueError(f"JSON did not match the required schema: {first_error.errors(include_url=False)[:5]}")


def strip_json_block(text: str) -> str:
    """The prose part of a member report: everything before the final fenced JSON block."""
    blocks = _json_blocks(text)
    if blocks and blocks[-1][1].strip().startswith("{"):
        return text[: blocks[-1][0]].rstrip()
    return text.rstrip()
