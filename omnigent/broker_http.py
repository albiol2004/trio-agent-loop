"""Small stdlib-only HTTP client for Omnigent broker sessions."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:6767"


class BrokerHttpError(RuntimeError):
    """An actionable failure while talking to the broker."""


def _expired(expires_at: object) -> bool:
    """Return whether a numeric or ISO timestamp is in the past."""
    if expires_at is None:
        return False
    try:
        stamp = float(expires_at)
    except (TypeError, ValueError):
        if not isinstance(expires_at, str):
            return False
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamp = parsed.timestamp()
    return stamp <= time.time()


def auth_token(base_url: str) -> str | None:
    """Find a bearer token without requiring a package import."""
    token = os.environ.get("OMNIGENT_TOKEN") or os.environ.get(
        "OMNIGENT_REMOTE_AUTH_TOKEN"
    )
    if token and token.strip():
        return token.strip()

    path = Path.home() / ".omnigent" / "auth_tokens.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    normalized = base_url.rstrip("/")
    record: object = None
    for key in (base_url, normalized, normalized + "/"):
        if key in data:
            record = data[key]
            break
    if isinstance(record, dict):
        if _expired(record.get("expires_at")):
            return None
        for key in ("token", "access_token", "accessToken", "auth_token"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
    if isinstance(record, str) and record.strip():
        return record.strip()
    return None


class BrokerClient:
    """Call the small session surface exposed by the broker."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        expected_status: int = 200,
    ) -> Any:
        """Send one JSON request and decode its JSON response."""
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {"Accept": "application/json"}
        token = auth_token(self.base_url)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{self.base_url}/{path.lstrip('/')}"
        request = Request(url, data=body, headers=headers, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = response.getcode()
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            suffix = f": {detail}" if detail else ""
            raise BrokerHttpError(
                f"{method} {url} failed with HTTP {exc.code}{suffix}"
            ) from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise BrokerHttpError(f"{method} {url} failed: {exc}") from exc

        if status != expected_status:
            raise BrokerHttpError(
                f"{method} {url} returned HTTP {status}, expected "
                f"{expected_status}"
            )
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerHttpError(f"{method} {url} returned invalid JSON") from exc

    def create_session(
        self,
        agent_id: str,
        model: str,
        message: str,
        title: str | None = None,
    ) -> Any:
        """Create a session with both goal fields and live API extras."""
        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "model": model,
            "message": message,
            "model_override": model,
            "initial_items": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": message}],
                }
            ],
        }
        if title:
            payload["title"] = title
        return self._request("POST", "/v1/sessions", payload, 201)

    def list_agents(self) -> Any:
        """List broker agents so live doctor can pick a probe target."""
        return self._request("GET", "/v1/agents")

    def get_session(self, session_id: str) -> Any:
        """Fetch one session snapshot."""
        path = f"/v1/sessions/{quote(session_id, safe='')}"
        return self._request("GET", path)

    def get_items(
        self,
        session_id: str,
        limit: int = 100,
        order: str = "asc",
    ) -> Any:
        """Fetch committed session items in the requested order."""
        query = urlencode({"limit": limit, "order": order})
        path = f"/v1/sessions/{quote(session_id, safe='')}/items?{query}"
        return self._request("GET", path)
