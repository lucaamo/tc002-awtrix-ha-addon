from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class BridgeError(Exception):
    code: str
    message: str
    field: str | None = None
    status: int = 422

    def __str__(self) -> str:
        return self.message

    def as_result(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field is not None:
            error["field"] = self.field
        return {"ok": False, "error": error}


def invalid_json(message: str = "Invalid JSON payload") -> BridgeError:
    return BridgeError("invalidJson", message, status=400)


def validation(message: str, field: str | None = None) -> BridgeError:
    return BridgeError("validationFailed", message, field=field, status=422)


def not_found(message: str, field: str | None = None) -> BridgeError:
    return BridgeError("notFound", message, field=field, status=404)


def capacity(message: str) -> BridgeError:
    return BridgeError("insufficientStorage", message, status=507)


def unsupported(message: str, field: str | None = None) -> BridgeError:
    return BridgeError("unsupported", message, field=field, status=501)


def unavailable(message: str) -> BridgeError:
    return BridgeError("unavailable", message, status=503)
