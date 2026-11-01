"""Secret references used by TotalOrch-owned provider connections."""

from __future__ import annotations

import os
import re


class CredentialError(ValueError):
    """Raised when a configured secret reference cannot be resolved safely."""


_ENV_REF = re.compile(r"^env://([A-Za-z_][A-Za-z0-9_]*)$")


class EnvironmentSecretResolver:
    """Resolve ``env://NAME`` references without persisting or returning metadata."""

    def resolve(self, secret_ref: str) -> str:
        match = _ENV_REF.fullmatch(secret_ref)
        if match is None:
            raise CredentialError("only env:// secret references are supported")
        value = os.getenv(match.group(1))
        if not value:
            raise CredentialError(f"secret environment variable is not configured: {match.group(1)}")
        return value

    def configured(self, secret_ref: str) -> bool:
        try:
            self.resolve(secret_ref)
        except CredentialError:
            return False
        return True
