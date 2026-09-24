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

# Ids file used by `trioctl omnigent loop` so a session that dies
# during start is still pruned. Same env var as trioctl.
SESSION_IDS_ENV_VAR = "TRIO_MAILBOX_SESSION_IDS"


def _env_float(name: str, default: float) -> float:
    """Read a float env override, ignoring invalid values."""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int env override, ignoring invalid values."""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _prompt_wait() -> float:
    """Longest wait for an unaccounted first-prompt copy.

    Default is Omnigent 0.14's pending-input TTL; archived user rows
    arrived up to 573 s after create.
    """
    return _env_float("TRIO_OMNIGENT_PROMPT_WAIT", 600.0)


def _prompt_interval() -> float:
    """Poll interval while waiting for the first prompt."""
    return _env_float("TRIO_OMNIGENT_PROMPT_INTERVAL", 0.4)


def _prompt_attempts() -> int:
    """How many times to POST the first prompt on one session."""
    return max(1, _env_int("TRIO_OMNIGENT_PROMPT_ATTEMPTS", 3))


def _record_created_session_id(session_id: str) -> None:
    """Append the id so loop prune can DELETE even if start fails."""
    path_value = os.environ.get(SESSION_IDS_ENV_VAR)
    if not path_value:
        return
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{session_id}\n")


def _session_item_rows(payload: Any) -> list[Any]:
    """Item rows from a list or ``items``/``data`` envelope."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _item_role(item: Any) -> str:
    """Role on a conversation item or nested ``data``."""
    if not isinstance(item, dict):
        return ""
    role = item.get("role")
    if isinstance(role, str):
        return role.lower()
    data = item.get("data")
    nested = data.get("role") if isinstance(data, dict) else None
    return nested.lower() if isinstance(nested, str) else ""


def _prompt_text(text: str) -> str:
    """Normalize like the cursor paste: CR/CRLF to LF, controls but tab dropped."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch for ch in text if ch in "\n\t" or ord(ch) >= 0x20
    ).strip()


def _user_row_texts(items: list[Any]) -> list[str]:
    """Normalized bodies of every user row, empty ones included."""
    return [
        _prompt_text(_item_text(item))
        for item in items
        if _item_role(item) == "user"
    ]


def _item_text(item: Any) -> str:
    """Flatten item content/text fields into one string."""
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(
                    str(part.get("text") or part.get("input_text") or "")
                )
            elif part is not None:
                parts.append(str(part))
        return "".join(parts)
    data = item.get("data")
    if isinstance(data, dict):
        return _item_text(data)
    value = item.get("text")
    return value if isinstance(value, str) else ""


class BrokerHttpError(RuntimeError):
    """An actionable failure while talking to the broker."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PromptDeliveryUncertain(BrokerHttpError):
    """A first-prompt copy is unaccounted for: it may still run.

    The session is left alive (never re-posted, never deleted) so a
    queued or running copy is not duplicated or killed mid-work.
    """

    def __init__(self, message: str, session_id: str):
        super().__init__(message)
        self.session_id = session_id


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
        host_id: str | None = None,
        workspace: str | None = None,
        prompt_timeout: float | None = None,
    ) -> Any:
        """Create one session on a dedicated host runner.

        Default path: POST ``host_id`` + ``workspace`` so the server
        launches a runner for this session (0.12 and 0.14). If the
        create response has no ``runner_id``, fall back to
        ``POST /v1/hosts/{host_id}/runners``.

        Explicit ``runner_id`` / ``TRIO_OMNIGENT_RUNNER_ID`` still
        PATCHes an already-online runner (shared-runner override).

        After POST, the session id is written to
        ``TRIO_MAILBOX_SESSION_IDS`` before bind/dispatch. Any later
        failure deletes the session so the loop does not leave orphans,
        except :class:`PromptDeliveryUncertain`, which keeps it. The
        first prompt is re-posted to the same session only on a known
        miss (see :meth:`ensure_first_prompt`); ``prompt_timeout`` caps
        its wait (the caller's role timeout).
        """
        workdir = workspace or os.getcwd()
        preferred_source = "--runner-id"
        preferred = runner_id or ""
        if not preferred:
            preferred_source = "TRIO_OMNIGENT_RUNNER_ID"
            preferred = os.environ.get("TRIO_OMNIGENT_RUNNER_ID", "")

        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "model": model,
            "message": message,
            "model_override": model,
            # Keep creation metadata-only. A history seed plus the
            # dispatch event would duplicate the user message.
            "initial_items": [],
        }
        if title:
            payload["title"] = title

        # Dedicated runner unless the caller named an existing one.
        if not preferred:
            resolved_host = self._resolve_host_id(host_id)
            payload["host_id"] = resolved_host
            payload["workspace"] = workdir

        session_id: str | None = None
        try:
            created = self._request("POST", "/v1/sessions", payload, 201)
            if not isinstance(created, dict):
                raise BrokerHttpError(
                    "POST /v1/sessions returned a non-object response"
                )
            session_id = created.get("id") or created.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise BrokerHttpError(
                    "POST /v1/sessions returned no session id"
                )
            _record_created_session_id(session_id)
            created.setdefault("id", session_id)

            if preferred:
                created = self._bind_named_runner(
                    created, session_id, preferred, preferred_source
                )
            elif not created.get("runner_id"):
                launched = self.launch_runner(
                    payload["host_id"], session_id, workdir
                )
                if isinstance(launched, dict):
                    created = {**created, **launched}
                    created.setdefault("id", session_id)

            self.send_message(session_id, message)
            wait = _prompt_wait()
            if prompt_timeout is not None:
                wait = min(wait, prompt_timeout)
            self.ensure_first_prompt(session_id, message, wait_seconds=wait)
            return created
        except PromptDeliveryUncertain:
            raise
        except Exception:
            if session_id:
                self._delete_started_session(session_id)
            raise

    def _resolve_host_id(self, host_id: str | None) -> str:
        """Pick the online host: flag/env, else exactly one live host."""
        source = "--host-id"
        preferred = host_id or ""
        if not preferred:
            source = "TRIO_OMNIGENT_HOST_ID"
            preferred = os.environ.get("TRIO_OMNIGENT_HOST_ID", "")
        hosts = self._online_hosts()
        if preferred:
            match = [row for row in hosts if row["host_id"] == preferred]
            if not match:
                raise BrokerHttpError(
                    "cannot start session: "
                    f"{source}={preferred} is not an online host"
                )
            return preferred
        if len(hosts) != 1:
            ids = (
                "\n".join(f"  {row['host_id']}" for row in hosts)
                or "  (none online)"
            )
            raise BrokerHttpError(
                "cannot start session: expected exactly one online host, "
                f"found {len(hosts)}:\n{ids}\n"
                "set TRIO_OMNIGENT_HOST_ID=<id> or pass --host-id <id> "
                "to choose one"
            )
        return hosts[0]["host_id"]

    def _online_hosts(self) -> list[dict[str, Any]]:
        """Online rows from GET /v1/hosts (``hosts`` or ``data``)."""
        payload = self.list_hosts()
        rows = payload.get("hosts") if isinstance(payload, dict) else payload
        if rows is None and isinstance(payload, dict):
            rows = payload.get("data")
        online: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            hid = row.get("host_id") or row.get("id")
            status = str(row.get("status") or "").lower()
            is_online = row.get("online") is True or status == "online"
            if is_online and isinstance(hid, str) and hid:
                online.append({**row, "host_id": hid})
        return online

    def _bind_named_runner(
        self,
        created: dict[str, Any],
        session_id: str,
        preferred: str,
        preferred_source: str,
    ) -> dict[str, Any]:
        """PATCH an already-online runner chosen by id."""
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
        online = [row for row in online if row["runner_id"] == preferred]
        if not online:
            raise BrokerHttpError(
                "cannot start session: "
                f"{preferred_source}={preferred} is not an online runner"
            )
        bound = self.bind_session(session_id, online[0]["runner_id"])
        if isinstance(bound, dict):
            created = {**created, **bound}
            created.setdefault("id", session_id)
        return created

    def _delete_started_session(self, session_id: str) -> None:
        """Best-effort DELETE so a failed start does not orphan."""
        try:
            self.delete_session(session_id)
        except BrokerHttpError:
            pass

    def list_hosts(self) -> Any:
        """List hosts so create can pick a dedicated launch target."""
        return self._request("GET", "/v1/hosts")

    def launch_runner(
        self, host_id: str, session_id: str, workspace: str
    ) -> Any:
        """Launch a runner when POST /v1/sessions ignored host_id."""
        path = f"/v1/hosts/{quote(host_id, safe='')}/runners"
        return self._request(
            "POST",
            path,
            {"session_id": session_id, "workspace": workspace},
            200,
        )

    def ensure_first_prompt(
        self,
        session_id: str,
        message: str,
        *,
        attempts: int | None = None,
        wait_seconds: float | None = None,
        interval: float | None = None,
    ) -> None:
        """Return once a user row equals the prompt; re-post only a known miss.

        The caller already POSTed once. On Omnigent 0.14 a 2xx POST only
        queues the copy for injection; the user row, mirrored from
        Cursor's own chat store, is the only receipt and records what
        Cursor actually received. ``pending_inputs`` drains on *any* user
        row and ``running`` is PTY activity, so neither shows integrity,
        and a re-post while a copy is queued or running later runs as a
        whole extra turn (canary 6f173b9d; live s2 POST #3).

        Landed: a user row equal to the prompt, normalized as the cursor
        paste encoder does. Known miss: every accepted POST has produced a
        user row and none matches (e.g. a head-truncated paste) -- re-post,
        up to ``attempts`` copies, then raise :class:`BrokerHttpError`.
        Otherwise a copy is unaccounted for: wait up to ``wait_seconds``
        from the last POST, then raise :class:`PromptDeliveryUncertain`
        without re-posting. A runner restart or a welcome-screen drop
        that leaves no row cannot be told apart from a slow turn here, so
        it ends as uncertain rather than being retried.
        """
        tries = attempts if attempts is not None else _prompt_attempts()
        ceiling = max(
            wait_seconds if wait_seconds is not None else _prompt_wait(), 0.0
        )
        poll = interval if interval is not None else _prompt_interval()
        want = _prompt_text(message)
        posts = 1
        deadline = time.monotonic() + ceiling
        while True:
            rows = _user_row_texts(_session_item_rows(self.get_items(session_id)))
            if want and want in rows:
                return
            if len(rows) >= posts:
                if posts >= tries:
                    raise BrokerHttpError(
                        f"cannot start session: first prompt did not land "
                        f"intact on {session_id} after {posts} attempt(s)"
                    )
                self.send_message(session_id, message)
                posts += 1
                deadline = time.monotonic() + ceiling
                continue
            now = time.monotonic()
            if now >= deadline:
                raise PromptDeliveryUncertain(
                    f"first prompt on {session_id}: {posts - len(rows)} of "
                    f"{posts} posted copies unaccounted for after "
                    f"{ceiling:g}s; not re-posting, session kept",
                    session_id,
                )
            time.sleep(min(poll, deadline - now))

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
