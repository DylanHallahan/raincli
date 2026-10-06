# Vendored from raincli tag v0.4.0 (commit a214082792ff64731cae1d6f12fc5cc900ae8b11), path raincli/raincli_agent/errors.py. Test fixture for protocol 16.12 C1; do not edit.
"""Typed errors and the CLI exit codes they map to."""

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3  # forbidden or conflict
EXIT_TIMEOUT = 4  # watch timeout
EXIT_CAPACITY = 5
EXIT_UNREACHABLE = 6


class RainError(Exception):
    """Base class for every error raincli_agent raises on purpose."""

    exit_code = EXIT_ERROR


class UsageError(RainError):
    exit_code = EXIT_USAGE


class ConfigError(RainError):
    pass


class ApiError(RainError):
    """An error response (or transport failure) from the RainCLI API."""

    code = "error"
    status = None

    def __init__(self, message, *, code=None, status=None, retry_after=None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.retry_after = retry_after

    def __str__(self):
        base = super().__str__()
        where = f"{self.status} " if self.status else ""
        return f"{where}{self.code}: {base}"


class Invalid(ApiError):
    code, status = "invalid", 400


class Unauthorized(ApiError):
    code, status = "unauthorized", 401


class Forbidden(ApiError):
    code, status = "forbidden", 403
    exit_code = EXIT_CONFLICT


class NotFound(ApiError):
    code, status = "not_found", 404


class Conflict(ApiError):
    """409: id_conflict, not_acked, or any other conflict code."""

    code, status = "conflict", 409
    exit_code = EXIT_CONFLICT


class IdConflict(Conflict):
    code = "id_conflict"


class NotAcked(Conflict):
    code = "not_acked"


class TooLarge(ApiError):
    code, status = "too_large", 413


class InboxFull(ApiError):
    code, status = "inbox_full", 429
    exit_code = EXIT_CAPACITY


class RateLimited(ApiError):
    code, status = "rate_limited", 429
    exit_code = EXIT_CAPACITY


class Unavailable(ApiError):
    """503 (or 502/504) after retries were exhausted."""

    code, status = "unavailable", 503
    exit_code = EXIT_UNREACHABLE


class Unreachable(ApiError):
    """Connection error or timeout after retries were exhausted."""

    code = "unreachable"
    exit_code = EXIT_UNREACHABLE


class RedirectRefused(ApiError):
    """The server answered 3xx. The client never follows redirects."""

    code = "redirect_refused"


class BadResponse(ApiError):
    code = "bad_response"


BY_CODE = {
    cls.code: cls
    for cls in (Invalid, Unauthorized, Forbidden, NotFound, IdConflict, NotAcked,
                TooLarge, InboxFull, RateLimited, Unavailable)
}
BY_STATUS = {400: Invalid, 401: Unauthorized, 403: Forbidden, 404: NotFound,
             409: Conflict, 413: TooLarge, 429: RateLimited, 502: Unavailable,
             503: Unavailable, 504: Unavailable}


def error_for(status, code, message, retry_after=None):
    cls = BY_CODE.get(code)
    if cls is None or (cls.status is not None and cls.status != status):
        cls = BY_STATUS.get(status, ApiError)
    return cls(message, code=code or cls.code, status=status, retry_after=retry_after)
