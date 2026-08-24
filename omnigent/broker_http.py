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
        """Create and start one session through the live broker sequence.

        ``initial_items`` only seeds history when no runner is bound. The
        follow-up message event is therefore the important dispatch step:
        it lets the server bind the session to the current runner and start
        the native Cursor turn, matching the web UI flow.
        """
        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "model": model,
            "message": message,
            "model_override": model,
            # Keep creation metadata-only. A history seed plus the dispatch
            # event below would duplicate the user's message in native
            # terminal transcripts.
            "initial_items": [],
        }
        if title:
            payload["title"] = title
        created = self._request("POST", "/v1/sessions", payload, 201)
        if not isinstance(created, dict):
            raise BrokerHttpError("POST /v1/sessions returned a non-object response")
        session_id = created.get("id") or created.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise BrokerHttpError("POST /v1/sessions returned no session id")

        # JSON session creation has no runner_id field. Match the UI's
        # unambiguous-runner binding so the first event can reach Cursor.
        if not created.get("runner_id"):
            runners = self.list_runners()
            rows = runners.get("data") if isinstance(runners, dict) else runners
            online = [
                row
                for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, dict)
                and row.get("online") is True
                and isinstance(row.get("runner_id"), str)
                and row["runner_id"]
            ]
            preferred = os.environ.get("TRIO_OMNIGENT_RUNNER_ID", "")
            if preferred:
                online = [row for row in online if row["runner_id"] == preferred]
                if not online:
                    raise BrokerHttpError(
                        "cannot start session: TRIO_OMNIGENT_RUNNER_ID="
                        f"{preferred} is not an online runner"
                    )
            if len(online) != 1:
                ids = ", ".join(row["runner_id"] for row in online) or "none"
                raise BrokerHttpError(
                    "cannot start session: expected exactly one online runner, "
                    f"found {len(online)} ({ids}); set TRIO_OMNIGENT_RUNNER_ID "
                    "to choose one"
                )
            bound = self.bind_session(session_id, online[0]["runner_id"])
            if isinstance(bound, dict):
                created = {**created, **bound}
                created.setdefault("id", session_id)

        self.send_message(session_id, message)
        return created

    def list_runners(self) -> Any:
        """List runners available for binding a newly created session."""
        return self._request("GET", "/v1/runners")

    def bind_session(self, session_id: str, runner_id: str) -> Any:
        """Bind a session to one already-online runner."""
        path = f"/v1/sessions/{quote(session_id, safe='')}"
        return self._request("PATCH", path, {"runner_id": runner_id})

    def send_message(self, session_id: str, message: str) -> Any:
        """Dispatch a user message to a created session."""
        path = f"/v1/sessions/{quote(session_id, safe='')}/events"
        return self._request(
            "POST",
            path,
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": message}],
                },
            },
            202,
        )

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
