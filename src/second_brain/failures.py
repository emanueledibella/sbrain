"""Allowlisted diagnostics: never persist exception messages or source excerpts."""

from dataclasses import asdict, dataclass

import httpx


@dataclass(frozen=True)
class Failure:
    stage: str
    code: str
    retryable: bool

    def dict(self):
        return asdict(self)


class RecoveryFailure(RuntimeError):
    def __init__(self, failure: Failure):
        self.failure = failure
        super().__init__(failure.code)


class ValidationRejected(ValueError):
    def __init__(self, failure, detail):
        self.failure = failure
        super().__init__(detail)


def classify(exc, stage):
    if isinstance(exc, (RecoveryFailure, ValidationRejected)):
        return exc.failure
    if isinstance(exc, httpx.TimeoutException):
        return Failure(stage, "dependency_timeout", True)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return Failure(stage, f"dependency_http_{status}", status in {408, 429} or status >= 500)
    if isinstance(exc, httpx.TransportError):
        return Failure(stage, "dependency_unavailable", True)
    if isinstance(exc, ValueError):
        return Failure(stage, "validation_rejected", False)
    return Failure(stage, "operation_failed", True)
