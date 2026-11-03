"""Secret redaction for logs and telemetry payloads (MVP-0908)."""

from __future__ import annotations

import re
from typing import Any

_SECRET_KEY = re.compile(
    r"(api[_-]?key|authorization|credential|password|secret|token)",
    re.IGNORECASE,
)
REDACTED = "[redacted]"


def redact(value: Any) -> Any:
    """Recursively redact values whose key looks like a credential.

    Keys are matched conservatively; a harmless key such as ``token_count`` is
    redacted too, which is preferable to leaking a real credential.
    """

    if isinstance(value, dict):
        return {
            str(key): REDACTED if _SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value
