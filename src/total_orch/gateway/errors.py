"""Normalized errors crossing the Gateway Adapter boundary."""

from __future__ import annotations


class GatewayError(RuntimeError):
    """An execution or discovery error with an explicit retry classification."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        provider: str | None = None,
        request_id: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.provider = provider
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "status_code": self.status_code,
            "provider": self.provider,
            "request_id": self.request_id,
            "retry_after_seconds": self.retry_after_seconds,
        }
