"""Request intake and deterministic task classification (MVP-0602)."""

from __future__ import annotations

import re

from .models import TaskClassification, TaskType

_BUG_TERMS = (
    "bug",
    "broken",
    "crash",
    "error",
    "fail",
    "failure",
    "fix",
    "incorrect",
    "regression",
    "traceback",
)
_FEATURE_TERMS = (
    "add",
    "build",
    "create",
    "endpoint",
    "feature",
    "implement",
    "introduce",
    "support",
)


def _matches(text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(term for term in terms if re.search(rf"\b{re.escape(term)}\b", text))


def classify_task(text: str, explicit: TaskType | None = None) -> TaskClassification:
    """Classify a request as a feature or a bug fix.

    Explicit caller intent always wins. Otherwise the request is scored by the
    number of distinct bug/feature terms it uses; ties stay with the safer
    feature-development workflow, which still permits a code change.
    """

    if explicit is not None:
        return TaskClassification(
            task_type=explicit,
            reason=f"caller declared task_type={explicit.value}",
            confidence=1.0,
        )

    lowered = text.lower()
    bug_hits = _matches(lowered, _BUG_TERMS)
    feature_hits = _matches(lowered, _FEATURE_TERMS)
    if len(bug_hits) > len(feature_hits):
        return TaskClassification(
            task_type=TaskType.bug_fix,
            reason=f"bug-fix terms present: {', '.join(bug_hits)}",
            confidence=min(0.9, 0.5 + 0.1 * len(bug_hits)),
        )
    if feature_hits:
        return TaskClassification(
            task_type=TaskType.feature,
            reason=f"feature terms present: {', '.join(feature_hits)}",
            confidence=min(0.9, 0.5 + 0.1 * len(feature_hits)),
        )
    return TaskClassification(
        task_type=TaskType.feature,
        reason="no task-type terms found; defaulting to the feature workflow",
        confidence=0.4,
    )
