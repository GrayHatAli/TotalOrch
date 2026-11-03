"""Structured-output parsing for workflow agents (MVP-0611)."""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

T = TypeVar("T", bound=BaseModel)


class OutputParseError(ValueError):
    """The model did not return the structured output the node requires."""


def _json_candidate(text: str) -> str:
    stripped = text.strip()
    fenced = _FENCE.search(stripped)
    if fenced is not None:
        stripped = fenced.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise OutputParseError("response contains no JSON object")
    return stripped[start : end + 1]


def parse_output(text: str, schema: type[T]) -> T:
    """Parse the first JSON object in a model response into ``schema``.

    Agents are instructed to return JSON only, but a fenced block or a short
    prose prefix is common enough that extracting the first object is kinder
    than failing the workflow.
    """

    try:
        payload = json.loads(_json_candidate(text))
    except json.JSONDecodeError as exc:
        raise OutputParseError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise OutputParseError("response JSON must be an object")
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise OutputParseError(f"response does not match {schema.__name__}: {exc}") from exc
