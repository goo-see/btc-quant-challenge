from __future__ import annotations


class UpbitError(Exception):
    """Base for all Upbit client errors."""


class UpbitHTTPError(UpbitError):
    def __init__(self, status: int, name: str | None, message: str | None, body: str = "") -> None:
        self.status = status
        self.name = name
        self.message = message
        self.body = body
        super().__init__(f"HTTP {status} {name or ''}: {message or body[:200]}")


class UpbitRateLimitedError(UpbitHTTPError):
    """429 (second-level) or 418 (temporary ban)."""


class UpbitAuthError(UpbitHTTPError):
    """401/403 — invalid key, IP not whitelisted, out_of_scope, etc."""


class UpbitNetworkError(UpbitError):
    """Connection/timeout — the request may or may not have reached the server."""


class UpbitCredentialsMissingError(UpbitError):
    pass
