"""Small stdlib-only HTTP client for Omnigent broker sessions."""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:6767"


class BrokerHttpError(RuntimeError):
    """An actionable failure while talking to the broker."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


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


def _broker_base_url() -> str:
    """Resolve the dashboard broker URL without importing Omnigent."""
    for name in ("OMNIGENT_URL", "OMNIGENT_BASE_URL"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip().rstrip("/")
    return DEFAULT_BASE_URL


def bundle_agent_dir(config_yaml_path: str | os.PathLike[str]) -> bytes:
    """Build a gzip-compressed tar containing a root-level ``config.yaml``."""
    path = Path(config_yaml_path)
    content = path.read_bytes()
    archive = io.BytesIO()
    with gzip.GzipFile(fileobj=archive, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as tar:
            info = tarfile.TarInfo("config.yaml")
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(content))
    return archive.getvalue()


def _multipart_body(
    bundle_bytes: bytes, title: str | None = None
) -> tuple[bytes, str]:
    """Encode the broker's metadata and bundle multipart fields."""
    boundary = f"----trio-broker-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    if title is not None:
        metadata = json.dumps({"title": title}, separators=(",", ":")).encode(
            "utf-8"
        )
        parts.append(
            f"--{boundary}\r\n".encode("ascii")
            + b'Content-Disposition: form-data; name="metadata"\r\n'
            + b"Content-Type: application/json\r\n\r\n"
            + metadata
            + b"\r\n"
        )
    parts.append(
        f"--{boundary}\r\n".encode("ascii")
        + b'Content-Disposition: form-data; name="bundle"; '
        + b'filename="bundle.tar.gz"\r\n'
        + b"Content-Type: application/gzip\r\n\r\n"
        + bytes(bundle_bytes)
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), boundary


def _multipart_request(
    method: str,
    path: str,
    bundle_bytes: bytes,
    *,
    title: str | None = None,
    expected_status: int,
) -> Any:
    """Send one broker multipart request and decode its JSON response."""
    base_url = _broker_base_url()
    body, boundary = _multipart_body(bundle_bytes, title)
    headers = {
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    token = auth_token(base_url)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{base_url}/{path.lstrip('/')}"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30.0) as response:
            status = response.getcode()
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        suffix = f": {detail}" if detail else ""
        raise BrokerHttpError(
            f"{method} {url} failed with HTTP {exc.code}{suffix}",
            status_code=exc.code,
        ) from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise BrokerHttpError(f"{method} {url} failed: {exc}") from exc
    if status != expected_status:
        raise BrokerHttpError(
            f"{method} {url} returned HTTP {status}, expected "
            f"{expected_status}",
            status_code=status,
        )
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerHttpError(
            f"{method} {url} returned invalid JSON", status_code=status
        ) from exc


def register_agent_bundle(bundle_bytes: bytes, title: str) -> dict[str, str]:
    """Register a rendered bundle and return the broker's durable IDs."""
    response = _multipart_request(
        "POST",
        "/v1/sessions",
        bundle_bytes,
        title=title,
        expected_status=201,
    )
    if not isinstance(response, dict):
        raise BrokerHttpError("POST /v1/sessions returned a non-object response")
    result = {
        "agent_id": response.get("agent_id"),
        "session_id": response.get("session_id"),
        "agent_name": response.get("agent_name"),
    }
    if not all(isinstance(value, str) and value for value in result.values()):
        raise BrokerHttpError(
            "POST /v1/sessions returned incomplete agent registration"
        )
    return result


def update_agent_bundle(session_id: str, bundle_bytes: bytes) -> str:
    """Replace a session's bundle and return its durable agent ID."""
    path = f"/v1/sessions/{quote(session_id, safe='')}/agent"
    response = _multipart_request(
        "PUT", path, bundle_bytes, expected_status=200
    )
    if not isinstance(response, dict):
        raise BrokerHttpError(f"PUT {path} returned a non-object response")
    agent_id = response.get("id")
    if not isinstance(agent_id, str) or not agent_id:
        raise BrokerHttpError(f"PUT {path} returned no agent id")
    return agent_id


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

        # Transient network faults (socket timeouts, connection resets) on
        # idempotent GETs must not surface as role failures: a poll that
        # dies mid-loop makes the driver re-dispatch a duplicate Lead. Retry
        # GETs a few times before giving up; non-idempotent verbs never retry.
        attempts = 4 if method == "GET" else 1
        status = raw = None
        for attempt in range(attempts):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    status = response.getcode()
                    raw = response.read()
                break
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace").strip()
                suffix = f": {detail}" if detail else ""
                raise BrokerHttpError(
                    f"{method} {url} failed with HTTP {exc.code}{suffix}"
                ) from exc
            except (OSError, URLError, TimeoutError) as exc:
                if attempt + 1 >= attempts:
                    raise BrokerHttpError(
                        f"{method} {url} failed: {exc}"
                    ) from exc
                time.sleep(2.0 * (attempt + 1))

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
        runner_id: str | None = None,
    ) -> Any:
        """Create and start one session through the live broker sequence.

        ``initial_items`` only seeds history when no runner is bound. The
        follow-up message event is therefore the important dispatch step:
        it lets the server bind the session to the current runner and start
        the native Cursor turn, matching the web UI flow.

        `runner_id`, when given, takes precedence over the
        ``TRIO_OMNIGENT_RUNNER_ID`` environment variable for picking which
        online runner to bind to. Neither ever auto-picks among several
        online runners -- that stays an explicit, actionable error.
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
            preferred_source = "--runner-id"
            preferred = runner_id or ""
            if not preferred:
                preferred_source = "TRIO_OMNIGENT_RUNNER_ID"
                preferred = os.environ.get("TRIO_OMNIGENT_RUNNER_ID", "")
            if preferred:
                online = [row for row in online if row["runner_id"] == preferred]
                if not online:
                    raise BrokerHttpError(
                        "cannot start session: "
                        f"{preferred_source}={preferred} is not an online runner"
                    )
            if len(online) != 1:
                ids = (
                    "\n".join(f"  {row['runner_id']}" for row in online)
                    or "  (none online)"
                )
                raise BrokerHttpError(
                    "cannot start session: expected exactly one online runner, "
                    f"found {len(online)}:\n{ids}\n"
                    "set TRIO_OMNIGENT_RUNNER_ID=<id> or pass --runner-id <id> "
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

    def list_sessions(
        self,
        limit: int = 20,
        after: str | None = None,
        kind: str | None = None,
    ) -> Any:
        """List sessions registered on this broker, across all callers.

        ``limit`` defaults to the server's own default (20) so every
        existing (unpaged) caller sends the exact same request it always
        has; paged callers (see trioctl's session-archive prune) pass a
        larger ``limit`` and chase ``after`` -- a session-id cursor -- to
        see past the server's default newest-20 window. The route has no
        ``offset`` param, only cursor pagination.

        ``kind`` is omitted by default, which leaves the server on its own
        default (``kind=default``) -- the route also accepts ``sub_agent``
        and ``any``. A caller that needs sessions the Omnigent UI created
        via ``sys_session_create`` (``kind=sub_agent``, invisible under the
        server default) passes ``kind="any"`` explicitly.
        """
        params: dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        if kind:
            params["kind"] = kind
        query = urlencode(params)
        return self._request("GET", f"/v1/sessions?{query}")

    def delete_session(self, session_id: str) -> Any:
        """Permanently delete one broker session."""
        path = f"/v1/sessions/{quote(session_id, safe='')}"
        return self._request("DELETE", path, expected_status=200)

    def get_session(self, session_id: str) -> Any:
        """Fetch one session snapshot."""
        path = f"/v1/sessions/{quote(session_id, safe='')}"
        return self._request("GET", path)

    def get_items(
        self,
        session_id: str,
        limit: int = 100,
        order: str = "asc",
        after: str | None = None,
    ) -> Any:
        """Fetch committed session items in the requested order.

        The items route only supports cursor pagination -- there is no
        ``offset`` -- so ``after`` is omitted from the query when unset,
        meaning every existing (unpaged) caller sends the exact same
        request it always has; paged callers (see trioctl's
        session-archive prune) pass the previous page's last item id
        explicitly to walk past a broker's per-request ``limit`` cap.
        """
        params: dict[str, Any] = {"limit": limit, "order": order}
        if after:
            params["after"] = after
        query = urlencode(params)
        path = f"/v1/sessions/{quote(session_id, safe='')}/items?{query}"
        return self._request("GET", path)
