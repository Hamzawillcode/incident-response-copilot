"""Shared read-only HTTP plumbing for the observability tools.

Every network call made by ``copilot.tools`` goes through :func:`get_json`:

* method is always ``GET`` (OpenSearch's ``_search`` accepts a JSON body on
  GET, so no tool ever needs POST/PUT/DELETE);
* the path must satisfy the calling tool's allow-list;
* proxy environment variables are ignored (``trust_env=False``);
* redirects are not followed;
* the body is streamed and aborted beyond ``max_bytes``.

Failures are raised as :class:`ToolError` with a stable ``type`` so each tool
can turn them into a structured ``{"status": "error", ...}`` result.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

USER_AGENT = "incident-response-copilot/0.2"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024


class ToolError(Exception):
    """A failure that a tool reports as ``{"status": "error", "error": {...}}``."""

    def __init__(self, type_: str, message: str):
        super().__init__(message)
        self.type = type_
        self.message = message


# --------------------------------------------------------------------------- #
# Configuration helpers
# --------------------------------------------------------------------------- #
def safe_url(url: str) -> str:
    """Strip credentials (user:password@) so URLs can appear in messages."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return parts._replace(netloc="***@" + parts.netloc.rsplit("@", 1)[1]).geturl()


def base_url_from_env(var: str, default: str) -> str:
    url = os.environ.get(var, default).strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ToolError("configuration", f"{var} must be an http(s) URL, got {safe_url(url)!r}.")
    if parts.query or parts.fragment:
        raise ToolError("configuration", f"{var} must not contain a query string or fragment.")
    return url.rstrip("/")


def float_from_env(var: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(var, "")
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if low < value <= high else default


def int_from_env(var: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(var, "")
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if low <= value <= high else default


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_client(base_url: str, timeout: float) -> httpx.Client:
    """Create the HTTP client. Tests replace this function to inject recordings."""
    return httpx.Client(
        base_url=base_url,
        timeout=httpx.Timeout(timeout),
        trust_env=False,
        follow_redirects=False,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )


def get_json(
    *,
    base_url: str,
    path: str,
    allowed: Callable[[str], bool],
    timeout: float,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backend: str = "backend",
) -> tuple[int, Any]:
    """Perform the single allow-listed GET and return ``(status_code, body)``.

    ``body`` is parsed JSON, or the decoded text if the response is not JSON.
    """
    if not allowed(path):
        raise ToolError("internal", f"Path {path!r} is not allow-listed.")  # programming error, never user input
    content = None
    headers = {}
    if json_body is not None:
        content = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    try:
        with make_client(base_url, timeout) as client:
            with client.stream("GET", path, params=params, content=content, headers=headers) as resp:
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ToolError(
                            "response_too_large",
                            f"Response exceeded {max_bytes // (1024 * 1024)} MiB; narrow the query.",
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks)
                status = resp.status_code
    except httpx.TimeoutException:
        raise ToolError("timeout", f"{backend} did not answer within {timeout:g}s.") from None
    except httpx.TransportError as exc:
        raise ToolError("connection",
                        f"Cannot reach {backend} at {safe_url(base_url)}: {type(exc).__name__}.") from None
    except httpx.HTTPError as exc:  # e.g. DecodingError (corrupt compressed body)
        raise ToolError("http_error", f"{backend} response could not be read: {type(exc).__name__}.") from None
    except httpx.InvalidURL:
        raise ToolError("configuration", f"Invalid {backend} URL {safe_url(base_url)!r}.") from None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", errors="replace")


def path_in(paths: Iterable[str]) -> Callable[[str], bool]:
    allowed = frozenset(paths)
    return lambda path: path in allowed


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def round6(value: float) -> float:
    return float(f"{value:.6g}")


def error(base: dict[str, Any], type_: str, message: str) -> dict[str, Any]:
    out = dict(base)
    out.update(status="error", error={"type": type_, "message": message})
    return out


def snippet(body: Any, limit: int = 120) -> str:
    return " ".join(str(body).split())[:limit]


def validate_minutes(minutes: Any, low: int, high: int) -> str | None:
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return "minutes must be an integer."
    if not low <= minutes <= high:
        return f"minutes must be between {low} and {high}."
    return None


def validate_limit(limit: Any, high: int, name: str = "limit") -> str | None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= high:
        return f"{name} must be an integer between 1 and {high}."
    return None


def validate_optional_text(value: Any, name: str, max_len: int = 200) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return f"{name} must be a non-empty string when given."
    if len(value) > max_len:
        return f"{name} is longer than {max_len} characters."
    return None
