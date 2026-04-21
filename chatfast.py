"""ChatFast — personal ChatGPT client riding Codex OAuth tokens.

Reuses the Codex CLI OAuth client_id to sign in with a ChatGPT Plus/Pro
subscription via the device-code flow, then calls the Codex responses
backend as a chatbot. Single-file Tk app, dark theme.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import queue
import re
import sqlite3
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable, Iterator

# ---------------------------------------------------------------------------
# Constants (extracted from codex-rs/login/src/)
# ---------------------------------------------------------------------------

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
DEVICE_USERCODE_URL = f"{ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{ISSUER}/api/accounts/deviceauth/token"
OAUTH_TOKEN_URL = f"{ISSUER}/oauth/token"
DEVICE_REDIRECT_URI = f"{ISSUER}/deviceauth/callback"
VERIFICATION_URL = f"{ISSUER}/codex/device"

BACKEND_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"

AUTH_DIR = Path.home() / ".chatfast"
DB_FILE = AUTH_DIR / "chatfast.sqlite3"

# Full model catalog from codex-rs/models-manager/models.json.
# The ChatGPT-backed /codex/responses endpoint typically only accepts the
# `-codex` variants; non-codex and oss models are included for completeness —
# the server will reject them with a clear message if your account can't use them.
# Model catalog for Codex with ChatGPT sign-in.
# Sources: OpenAI "Codex Models" docs + codex-rs/models-manager/models.json.
# "Recommended" = models explicitly listed for ChatGPT sign-in on the docs page.
# `gpt-5.3-codex-spark` is a Pro-only research-preview text-only model
# optimized for near-instant, real-time coding iteration.
# Model catalog for Codex with ChatGPT sign-in.
#
# Spark (`gpt-5.3-codex-spark`) is a real standalone model slug served on
# Cerebras hardware (~1000 tok/s). Confirmed by probing /backend-api/codex/models
# with a Pro token and client_version >= 0.100.0 — it's returned as its own
# entry, not as a service-tier variant of gpt-5.4. Text-only, 128k context.
# Runtime caveat: even though the model's metadata advertises
# `supports_reasoning_summaries: true`, the backend rejects `reasoning.summary`
# with "unsupported_parameter" (openai/codex issue #13009). We strip `summary`
# and `include: reasoning.encrypted_content` before sending to Spark.
SPARK_MODEL = "gpt-5.3-codex-spark"

MODEL_SPECS: dict[str, list[str]] = {
    # === Recommended ===
    "gpt-5.4":                ["low", "medium", "high", "xhigh"],
    "gpt-5.4-mini":           ["low", "medium", "high", "xhigh"],
    "gpt-5.3-codex":          ["low", "medium", "high", "xhigh"],
    SPARK_MODEL:              ["low", "medium", "high", "xhigh"],
    # === Alternative ===
    "gpt-5.2":                ["low", "medium", "high", "xhigh"],
}
MODEL_GROUPS: list[tuple[str, list[str]]] = [
    ("Recommended", [
        "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", SPARK_MODEL,
    ]),
    ("Alternative", [
        "gpt-5.2",
    ]),
]
AVAILABLE_MODELS = list(MODEL_SPECS.keys())
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_EFFORT = "medium"

# Context window per model (default shown by the UI bar). Sourced from live
# /codex/models probe against a ChatGPT Pro account on 2026-04-21.
CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5.4":              272_000,
    "gpt-5.4-mini":         272_000,
    "gpt-5.3-codex":        272_000,
    SPARK_MODEL:            128_000,
    "gpt-5.2":              272_000,
}
# Models that advertise a larger `max_context_window` — user can toggle the
# bar's denominator to this value. The backend decides the real ceiling;
# here we only change what the UI displays as "full".
MAX_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5.4":              1_000_000,
}
# Codex reserves this many tokens for its fixed system prompt / tool defs
# (see codex-rs/protocol/src/protocol.rs BASELINE_TOKENS). Same value used
# here so the "% left" calculation matches Codex CLI behavior.
BASELINE_TOKENS = 12_000


def format_token_count(n: int) -> str:
    """Format a token count like '1.2k' / '273k' / '1M'."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M".rstrip("0").rstrip(".")
    if n >= 1_000:
        return f"{n/1_000:.1f}k".rstrip("0").rstrip(".")
    return str(n)


def percent_of_context_remaining(total_tokens: int, context_window: int) -> int:
    """Mirror of codex-rs percent_of_context_window_remaining."""
    if context_window <= BASELINE_TOKENS:
        return 0
    effective = context_window - BASELINE_TOKENS
    used = max(0, total_tokens - BASELINE_TOKENS)
    remaining = max(0, effective - used)
    pct = (remaining / effective) * 100.0
    return int(max(0.0, min(100.0, round(pct))))

# Match the real Codex CLI so Cloudflare's WAF and the auth service don't
# route-reject us as an unknown client. These strings mirror
# codex-rs/login/src/auth/default_client.rs.
ORIGINATOR = "codex_cli_rs"
# Must be >= 0.100.0 for Spark to be entitled. Bumped above that so the
# server includes gpt-5.3-codex-spark in /codex/models for our account.
CODEX_CLI_VERSION = "0.105.0"


def _build_user_agent() -> str:
    os_type = platform.system() or "Unknown"
    os_version = platform.release() or "unknown"
    arch = platform.machine() or "unknown"
    return f"{ORIGINATOR}/{CODEX_CLI_VERSION} ({os_type} {os_version}; {arch}) chatfast"


USER_AGENT = _build_user_agent()

# ---------------------------------------------------------------------------
# Dark theme palette
# ---------------------------------------------------------------------------

COL_BG          = "#0c0c0e"   # near-black window bg (slightly warmer)
COL_BG_ALT      = "#101014"   # user turn tint
COL_PANEL       = "#17181b"   # input panel / dropdown bg
COL_PANEL_ALT   = "#1f2024"   # buttons / hover
COL_PANEL_HOVER = "#2a2b2f"
COL_BORDER      = "#2c2d32"
COL_TEXT        = "#f2f2f2"   # near-white body text
COL_TEXT_DIM    = "#c9c9cc"
COL_MUTED       = "#82828a"
COL_ACCENT      = "#e38b56"   # orange asterisk / primary
COL_ACCENT_FG   = "#1a1208"
COL_SUCCESS     = "#7ec699"
COL_ERROR       = "#e07878"
COL_SELECT_BG   = "#2b2b30"
COL_SELECT_FG   = "#ffffff"

# Typography sizes — change here and the whole app + markdown tags follow.
FS_CHAT    = 11   # conversation body
FS_LABEL   = 11   # "You" / "Assistant" headers
FS_SMALL   = 9    # meta text
FS_CODE    = 10   # fenced code blocks


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only so the user doesn't need `pip install`)
# ---------------------------------------------------------------------------

def _default_headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "originator": ORIGINATOR,
        "Accept": "application/json",
    }


def _http_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, method=method, data=body)
    merged = _default_headers()
    if headers:
        merged.update(headers)
    for k, v in merged.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 60.0):
    body = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return _http_request(url, "POST", h, body, timeout)


def _http_post_form(url: str, form: dict, timeout: float = 60.0):
    body = urllib.parse.urlencode(form).encode("utf-8")
    h = {"Content-Type": "application/x-www-form-urlencoded"}
    return _http_request(url, "POST", h, body, timeout)


class _StreamCancelled(Exception):
    """Raised inside the stream reader when cancel() is signalled."""


def _stream_post_json(
    url: str,
    payload: dict,
    headers: dict,
    cancel_event: threading.Event | None = None,
    timeout: float = 300.0,
) -> Iterator[tuple[str, bytes]]:
    """Generator yielding (kind, data) tuples for SSE streaming.

    kind is either:
      "status"  — first yield: HTTP status code as bytes. Non-200 means the
                  next yield is the full error body and the stream ends.
      "data"    — a single raw SSE line (without trailing \\n). Blank lines
                  (event separators) are filtered out here.

    Streaming is truly incremental: we use `read1()` to return bytes as soon
    as they hit the socket rather than waiting for a full buffer. Cancel via
    `cancel_event.set()` from another thread — the connection is torn down
    cleanly.
    """
    body = json.dumps(payload).encode("utf-8")
    h = _default_headers()
    h["Content-Type"] = "application/json"
    h["Accept"] = "text/event-stream"
    h["Cache-Control"] = "no-cache"
    h.update(headers)
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in h.items():
        req.add_header(k, v)

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        yield "status", str(e.code).encode()
        try:
            yield "data", e.read()
        except Exception:
            pass
        return

    yield "status", str(resp.status).encode()

    # Python's http.client wraps the socket in a BufferedReader which exposes
    # read1(). read1(n) returns up to n bytes as soon as *any* are available —
    # that's what we want for real streaming; plain read(n) can block until it
    # has n bytes, which kills SSE responsiveness.
    reader = resp
    read_chunk = getattr(reader, "read1", None) or reader.read

    buf = b""
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise _StreamCancelled()

            try:
                chunk = read_chunk(8192)
            except (TimeoutError, ConnectionError, OSError) as e:
                if cancel_event is not None and cancel_event.is_set():
                    raise _StreamCancelled() from e
                raise

            if not chunk:
                # End of stream. Flush any remaining trailing line.
                tail = buf.rstrip(b"\r\n")
                if tail:
                    yield "data", tail
                return

            buf += chunk
            # SSE lines end in LF (RFC 6202); CR before LF is tolerated.
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                raw_line = buf[:nl]
                buf = buf[nl + 1:]
                line = raw_line.rstrip(b"\r")
                if not line:
                    # Blank line = SSE event boundary; nothing to do for us.
                    continue
                yield "data", line
    finally:
        try:
            resp.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Auth storage + refresh
# ---------------------------------------------------------------------------

@dataclass
class Tokens:
    id_token: str
    access_token: str
    refresh_token: str
    account_id: str | None = None
    email: str | None = None
    plan_type: str | None = None
    last_refresh: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _jwt_claims(jwt: str) -> dict:
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _parse_id_token_info(id_token: str) -> tuple[str | None, str | None, str | None]:
    """Returns (account_id, email, plan_type) parsed from id_token claims."""
    claims = _jwt_claims(id_token)
    auth = claims.get("https://api.openai.com/auth") or {}
    profile = claims.get("https://api.openai.com/profile") or {}
    account_id = None
    plan_type = None
    if isinstance(auth, dict):
        account_id = auth.get("chatgpt_account_id")
        plan_type = auth.get("chatgpt_plan_type")
    email = claims.get("email")
    if not email and isinstance(profile, dict):
        email = profile.get("email")
    return (
        str(account_id) if account_id else None,
        str(email) if email else None,
        str(plan_type) if plan_type else None,
    )


# ---------------------------------------------------------------------------
# SQLite-backed auth storage — `~/.chatfast/chatfast.sqlite3`
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    client_id       TEXT    NOT NULL,
    id_token        TEXT    NOT NULL,
    access_token    TEXT    NOT NULL,
    refresh_token   TEXT    NOT NULL,
    account_id      TEXT,
    email           TEXT,
    plan_type       TEXT,
    last_refresh    TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS chats (
    id                       TEXT PRIMARY KEY,
    title                    TEXT NOT NULL,
    model                    TEXT NOT NULL,
    effort                   TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    title_generated          INTEGER NOT NULL DEFAULT 0,
    last_total_tokens        INTEGER NOT NULL DEFAULT 0,
    context_window_override  INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,   -- JSON-encoded content[] array
    created_at  TEXT NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
"""


def _db() -> sqlite3.Connection:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    # Backfill for DBs created before these columns existed.
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(chats)").fetchall()}
    if "last_total_tokens" not in existing_cols:
        conn.execute(
            "ALTER TABLE chats ADD COLUMN last_total_tokens INTEGER NOT NULL DEFAULT 0"
        )
    if "context_window_override" not in existing_cols:
        conn.execute(
            "ALTER TABLE chats ADD COLUMN context_window_override INTEGER"
        )
    try:
        os.chmod(DB_FILE, 0o600)
    except Exception:
        pass
    return conn


def load_tokens() -> Tokens | None:
    if not DB_FILE.exists():
        return None
    try:
        conn = _db()
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT id_token, access_token, refresh_token, account_id, email, "
            "plan_type, last_refresh FROM auth WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    id_token, access_token, refresh_token, account_id, email, plan_type, last_refresh = row
    if not access_token or not refresh_token:
        return None
    return Tokens(
        id_token=id_token or "",
        access_token=access_token,
        refresh_token=refresh_token,
        account_id=account_id,
        email=email,
        plan_type=plan_type,
        last_refresh=last_refresh or datetime.now(timezone.utc).isoformat(),
    )


def save_tokens(t: Tokens) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO auth (id, client_id, id_token, access_token, refresh_token,
                              account_id, email, plan_type, last_refresh, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                client_id     = excluded.client_id,
                id_token      = excluded.id_token,
                access_token  = excluded.access_token,
                refresh_token = excluded.refresh_token,
                account_id    = excluded.account_id,
                email         = excluded.email,
                plan_type     = excluded.plan_type,
                last_refresh  = excluded.last_refresh,
                updated_at    = excluded.updated_at
            """,
            (
                CLIENT_ID,
                t.id_token,
                t.access_token,
                t.refresh_token,
                t.account_id,
                t.email,
                t.plan_type,
                t.last_refresh,
                now,
            ),
        )
    finally:
        conn.close()


def clear_tokens() -> None:
    if not DB_FILE.exists():
        return
    conn = _db()
    try:
        conn.execute("DELETE FROM auth WHERE id = 1")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Chat / message storage
# ---------------------------------------------------------------------------

DEFAULT_CHAT_TITLE = "New chat"


@dataclass
class Chat:
    id: str
    title: str
    model: str
    effort: str
    created_at: str
    updated_at: str
    title_generated: bool = False
    last_total_tokens: int = 0
    context_window_override: int | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_chat(model: str, effort: str, title: str = DEFAULT_CHAT_TITLE) -> Chat:
    cid = str(uuid.uuid4())
    now = _now_iso()
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO chats (id, title, model, effort, created_at, updated_at, title_generated) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (cid, title, model, effort, now, now),
        )
    finally:
        conn.close()
    return Chat(cid, title, model, effort, now, now, False)


def list_chats() -> list[Chat]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, title, model, effort, created_at, updated_at, title_generated, "
            "last_total_tokens, context_window_override "
            "FROM chats ORDER BY updated_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [Chat(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]),
                 int(r[7] or 0), r[8]) for r in rows]


def update_chat_usage(chat_id: str, last_total_tokens: int) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE chats SET last_total_tokens = ? WHERE id = ?",
            (int(last_total_tokens), chat_id),
        )
    finally:
        conn.close()


def update_chat_context_override(chat_id: str, context_window: int | None) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE chats SET context_window_override = ? WHERE id = ?",
            (context_window, chat_id),
        )
    finally:
        conn.close()


def rename_chat(chat_id: str, title: str, generated: bool = False) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE chats SET title = ?, title_generated = ?, updated_at = ? WHERE id = ?",
            (title, 1 if generated else 0, _now_iso(), chat_id),
        )
    finally:
        conn.close()


def update_chat_model(chat_id: str, model: str, effort: str) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE chats SET model = ?, effort = ?, updated_at = ? WHERE id = ?",
            (model, effort, _now_iso(), chat_id),
        )
    finally:
        conn.close()


def delete_chat(chat_id: str) -> None:
    conn = _db()
    try:
        conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    finally:
        conn.close()


def touch_chat(chat_id: str) -> None:
    conn = _db()
    try:
        conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (_now_iso(), chat_id))
    finally:
        conn.close()


def save_message(chat_id: str, role: str, content: list[dict]) -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, json.dumps(content), _now_iso()),
        )
    finally:
        conn.close()


def load_messages(chat_id: str) -> list[dict]:
    """Return a Responses-API-shaped history: [{role, content}, ...]."""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for role, content_json in rows:
        try:
            content = json.loads(content_json)
        except Exception:
            continue
        out.append({"role": role, "content": content})
    return out


def refresh_tokens(t: Tokens) -> Tokens:
    status, _hdrs, body = _http_post_json(
        OAUTH_TOKEN_URL,
        {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": t.refresh_token,
        },
    )
    if status != 200:
        raise RuntimeError(f"refresh failed [{status}]: {body[:300].decode(errors='replace')}")
    data = json.loads(body)
    new_id_token = data.get("id_token") or t.id_token
    account_id = t.account_id
    email = t.email
    plan_type = t.plan_type
    if data.get("id_token"):
        acc, em, plan = _parse_id_token_info(data["id_token"])
        account_id = acc or account_id
        email = em or email
        plan_type = plan or plan_type
    new = Tokens(
        id_token=new_id_token,
        access_token=data.get("access_token") or t.access_token,
        refresh_token=data.get("refresh_token") or t.refresh_token,
        account_id=account_id,
        email=email,
        plan_type=plan_type,
        last_refresh=datetime.now(timezone.utc).isoformat(),
    )
    save_tokens(new)
    return new


# ---------------------------------------------------------------------------
# Device-code sign-in
# ---------------------------------------------------------------------------

@dataclass
class DeviceCode:
    verification_url: str
    user_code: str
    device_auth_id: str
    interval: int


def request_device_code() -> DeviceCode:
    status, _hdrs, body = _http_post_json(DEVICE_USERCODE_URL, {"client_id": CLIENT_ID})
    if status != 200:
        raise RuntimeError(f"device code request failed [{status}]: {body[:300].decode(errors='replace')}")
    data = json.loads(body)
    interval = data.get("interval", 5)
    if isinstance(interval, str):
        interval = int(interval.strip())
    return DeviceCode(
        verification_url=VERIFICATION_URL,
        user_code=data["user_code"],
        device_auth_id=data["device_auth_id"],
        interval=int(interval),
    )


def poll_device_authorization(
    dc: DeviceCode, cancel_flag: Callable[[], bool]
) -> Tokens:
    """Poll until the user approves on the site, then exchange the auth code for tokens."""
    deadline = time.monotonic() + 15 * 60
    while True:
        if cancel_flag():
            raise RuntimeError("device auth cancelled")
        if time.monotonic() > deadline:
            raise RuntimeError("device auth timed out after 15 minutes")

        status, _hdrs, body = _http_post_json(
            DEVICE_TOKEN_URL,
            {"device_auth_id": dc.device_auth_id, "user_code": dc.user_code},
        )

        if status == 200:
            data = json.loads(body)
            auth_code = data["authorization_code"]
            code_verifier = data["code_verifier"]
            break

        if status in (403, 404):
            time.sleep(dc.interval)
            continue

        raise RuntimeError(f"device auth failed [{status}]: {body[:300].decode(errors='replace')}")

    # Exchange authorization code for real OAuth tokens
    st, _h, body = _http_post_form(
        OAUTH_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": DEVICE_REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": code_verifier,
        },
    )
    if st != 200:
        raise RuntimeError(f"token exchange failed [{st}]: {body[:300].decode(errors='replace')}")
    toks = json.loads(body)
    acc, email, plan = _parse_id_token_info(toks["id_token"])
    tokens = Tokens(
        id_token=toks["id_token"],
        access_token=toks["access_token"],
        refresh_token=toks["refresh_token"],
        account_id=acc,
        email=email,
        plan_type=plan,
    )
    save_tokens(tokens)
    return tokens


# ---------------------------------------------------------------------------
# Chat client — calls chatgpt.com/backend-api/codex/responses
# ---------------------------------------------------------------------------

def _build_responses_request(
    history: list[dict], model: str, effort: str, stream: bool
) -> dict:
    # Spark rejects `reasoning.summary` with unsupported_parameter at runtime
    # (openai/codex issue #13009); it also rejects the encrypted reasoning
    # include. Strip both when targeting Spark.
    is_spark = model == SPARK_MODEL
    reasoning: dict = {"effort": effort}
    if not is_spark:
        reasoning["summary"] = "auto"

    payload: dict = {
        "model": model,
        "input": history,
        "stream": stream,
        "store": False,
        "reasoning": reasoning,
        "instructions": (
            "You are a helpful, concise conversational assistant. "
            "Answer directly. Do not use tools or file operations."
        ),
    }
    if not is_spark:
        payload["include"] = ["reasoning.encrypted_content"]
    return payload


def _auth_headers(t: Tokens, session_id: str) -> dict:
    h = {
        "Authorization": f"Bearer {t.access_token}",
        "OpenAI-Beta": "responses=experimental",
        "version": CODEX_CLI_VERSION,
        "session_id": session_id,
    }
    if t.account_id:
        h["chatgpt-account-id"] = t.account_id
    return h


def _extract_text_from_item(item: dict) -> str:
    """Pull concatenated text from a response.output_item.done `item`.

    Items can look like:
      {"type":"message","role":"assistant",
       "content":[{"type":"output_text","text":"..."}, ...]}
    """
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for piece in content:
        if not isinstance(piece, dict):
            continue
        t = piece.get("type", "")
        if t in ("output_text", "text"):
            txt = piece.get("text")
            if isinstance(txt, str):
                parts.append(txt)
    return "".join(parts)


def _format_stream_error(event: dict) -> str:
    """Extract a human-readable message from a response.failed / incomplete event."""
    resp = event.get("response") or {}
    err = resp.get("error") if isinstance(resp, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or json.dumps(err)
        return str(msg)
    incomplete = resp.get("incomplete_details") if isinstance(resp, dict) else None
    if isinstance(incomplete, dict):
        return f"incomplete: {incomplete.get('reason', 'unknown')}"
    return event.get("type", "stream error")


def chat_stream(
    tokens_ref: list,   # mutable one-element list holding the Tokens
    history: list[dict],
    model: str,
    effort: str,
    session_id: str,
    on_delta: Callable[[str], None],
    on_reasoning: Callable[[str], None],
    on_done: Callable[[], None],
    on_error: Callable[[str], None],
    on_phase: Callable[[str], None] = lambda _p: None,
    on_reasoning_step: Callable[[str], None] = lambda _k: None,
    on_usage: Callable[[dict], None] = lambda _u: None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Run one streaming chat turn. Refreshes tokens once on 401.

    Callbacks:
      on_delta          — incremental output text (main response)
      on_reasoning      — incremental reasoning summary text (muted preview)
      on_done           — stream finished successfully
      on_error          — fatal error, stream ended
      on_phase          — phase transition: "thinking" | "reasoning" | "writing"
      on_reasoning_step — "start"/"end" boundary for each reasoning summary part
      on_usage          — fires once on response.completed with the server's
                          `usage` dict (input_tokens, output_tokens,
                          total_tokens, *_details). Used to drive the context
                          usage bar.

    cancel_event — optional threading.Event. Set it to tear the stream down
                   mid-generation (Stop button). Returns via on_error("cancelled").
    """
    payload = _build_responses_request(history, model, effort, stream=True)

    for attempt in range(2):
        tokens = tokens_ref[0]
        headers = _auth_headers(tokens, session_id)
        status_seen: int | None = None
        error_body = b""
        got_any_delta = False
        fallback_text = ""

        try:
            for kind, data in _stream_post_json(
                BACKEND_RESPONSES_URL, payload, headers, cancel_event=cancel_event,
            ):
                if kind == "status":
                    status_seen = int(data.decode())
                    continue

                if status_seen != 200:
                    error_body += data
                    continue

                line = data
                if not line.startswith(b"data:"):
                    continue
                payload_bytes = line[5:].lstrip()
                if not payload_bytes or payload_bytes == b"[DONE]":
                    continue
                try:
                    event = json.loads(payload_bytes)
                except Exception:
                    continue

                etype = event.get("type", "")

                if etype == "response.created":
                    on_phase("thinking")

                elif etype == "response.output_item.added":
                    item = event.get("item") or {}
                    it = item.get("type") if isinstance(item, dict) else ""
                    if it == "reasoning":
                        on_phase("reasoning")
                    elif it == "message":
                        on_phase("writing")

                elif etype == "response.output_text.delta":
                    delta = event.get("delta", "")
                    if delta:
                        got_any_delta = True
                        on_delta(delta)

                elif etype == "response.reasoning_summary_text.delta":
                    delta = event.get("delta", "")
                    if delta:
                        on_reasoning(delta)

                elif etype == "response.reasoning_text.delta":
                    delta = event.get("delta", "")
                    if delta:
                        on_reasoning(delta)

                elif etype == "response.reasoning_summary_part.added":
                    on_reasoning_step("start")

                elif etype == "response.reasoning_summary_part.done":
                    on_reasoning_step("end")

                elif etype == "response.reasoning_summary_text.done":
                    # End of a summary text block (one logical reasoning step).
                    on_reasoning_step("text_done")

                elif etype == "response.output_item.done":
                    # Fallback if the server skipped text deltas (short answers,
                    # or some models that only emit full items): capture the
                    # message content here so we can surface it at `completed`.
                    item = event.get("item") or {}
                    if isinstance(item, dict) and item.get("type") == "message":
                        fallback_text += _extract_text_from_item(item)

                elif etype == "response.completed":
                    if not got_any_delta and fallback_text:
                        on_delta(fallback_text)
                        got_any_delta = True
                    # Surface token usage so the UI can refresh its context bar.
                    resp = event.get("response") or {}
                    usage = resp.get("usage") if isinstance(resp, dict) else None
                    if isinstance(usage, dict):
                        on_usage(usage)

                elif etype == "response.failed":
                    on_error(_format_stream_error(event))
                    return

                elif etype == "response.incomplete":
                    on_error(_format_stream_error(event))
                    return

                elif etype in ("response.error", "error"):
                    err = event.get("error") or event.get("message") or str(event)
                    on_error(f"server error: {err}")
                    return

                # Remaining event types (custom_tool_call_input.delta,
                # response.in_progress, etc.) are tool calls or no-op metadata
                # we don't care about in chatbot mode.
        except _StreamCancelled:
            on_error("cancelled")
            return
        except Exception as e:
            on_error(f"network error: {e}")
            return

        if status_seen == 401 and attempt == 0:
            try:
                tokens_ref[0] = refresh_tokens(tokens)
                continue
            except Exception as e:
                on_error(f"token refresh failed: {e}")
                return

        if status_seen != 200:
            on_error(
                f"HTTP {status_seen}: {error_body[:800].decode(errors='replace').strip()}"
            )
            return

        if not got_any_delta:
            on_error("no output produced (empty stream)")
            return

        on_done()
        return


# ---------------------------------------------------------------------------
# Title generation (1-3 word chat title via Spark)
# ---------------------------------------------------------------------------

TITLE_INSTRUCTIONS = (
    "You generate short chat titles. Reply with 1 to 3 words capturing the "
    "core topic. Title Case. No punctuation, no quotes, no emoji, no trailing "
    "period. Absolutely do not explain — output only the title."
)


def generate_title(tokens_ref: list, user_text: str, assistant_text: str) -> str:
    """Blocking call to Spark that returns a short title string (or '')."""
    history = [
        {"role": "user",
         "content": [{"type": "input_text", "text": user_text[:2000]}]},
        {"role": "assistant",
         "content": [{"type": "output_text", "text": assistant_text[:2000]}]},
        {"role": "user",
         "content": [{"type": "input_text",
                      "text": "Title this chat in 1-3 words."}]},
    ]
    collected: list[str] = []
    done = threading.Event()
    err: list[str] = []

    def on_delta(d): collected.append(d)
    def on_reasoning(_): pass
    def on_done(): done.set()
    def on_error(m): err.append(m); done.set()

    # Temporarily swap instructions on the payload for this call.
    payload = _build_responses_request(history, SPARK_MODEL, "low", stream=True)
    payload["instructions"] = TITLE_INSTRUCTIONS

    def worker():
        # We reuse chat_stream's retry/refresh logic via a minimal inline loop
        # so the signature stays simple.
        for attempt in range(2):
            tokens = tokens_ref[0]
            headers = _auth_headers(tokens, str(uuid.uuid4()))
            status_seen = None
            got = False
            try:
                for kind, data in _stream_post_json(
                    BACKEND_RESPONSES_URL, payload, headers,
                ):
                    if kind == "status":
                        status_seen = int(data.decode()); continue
                    if status_seen != 200:
                        continue
                    line = data
                    if not line.startswith(b"data:"): continue
                    ps = line[5:].lstrip()
                    if not ps or ps == b"[DONE]": continue
                    try:
                        ev = json.loads(ps)
                    except Exception:
                        continue
                    if ev.get("type") == "response.output_text.delta":
                        d = ev.get("delta", "")
                        if d:
                            got = True
                            on_delta(d)
            except Exception as e:
                err.append(str(e))
                done.set(); return
            if status_seen == 401 and attempt == 0:
                try:
                    tokens_ref[0] = refresh_tokens(tokens)
                    continue
                except Exception as e:
                    err.append(str(e))
                    done.set(); return
            if status_seen == 200 and got:
                on_done(); return
            err.append(f"title HTTP {status_seen}")
            done.set(); return

    threading.Thread(target=worker, daemon=True).start()
    if not done.wait(timeout=25):
        return ""
    if err and not collected:
        return ""
    raw = "".join(collected).strip()
    # Sanitize: first line only, strip quotes, collapse whitespace, cap at 3 words.
    raw = raw.splitlines()[0] if raw else ""
    raw = raw.strip(" \t\"'`.,:;!?")
    words = raw.split()
    return " ".join(words[:3]) if words else ""


# ---------------------------------------------------------------------------
# Markdown + syntax highlight renderer
# ---------------------------------------------------------------------------

try:
    from pygments import lex as _pyg_lex  # type: ignore
    from pygments.lexers import get_lexer_by_name as _pyg_get_lexer  # type: ignore
    from pygments.util import ClassNotFound as _PygClassNotFound  # type: ignore
    from pygments.token import Token as _PygToken  # type: ignore
    _HAS_PYGMENTS = True
except Exception:
    _HAS_PYGMENTS = False

COL_CODE_BG = "#101012"

# Syntax palette (foreground) for common token groups, tuned for dark theme.
SYN_COLORS = {
    "syn_keyword":         "#c786dc",
    "syn_name_function":   "#79bdee",
    "syn_name_class":      "#79bdee",
    "syn_name_builtin":    "#79bdee",
    "syn_name_decorator":  "#e5b78a",
    "syn_name_tag":        "#c786dc",
    "syn_name_attribute":  "#e5b78a",
    "syn_string":          "#a8d08d",
    "syn_string_doc":      "#7d9c6d",
    "syn_comment":         "#7a7a80",
    "syn_number":          "#e38b56",
    "syn_operator":        "#d9d9d9",
    "syn_punctuation":     "#b8b8b8",
    "syn_plain":           "#e6e6e6",
}


def _pyg_to_tag(tok) -> str:
    if not _HAS_PYGMENTS:
        return "syn_plain"
    T = _PygToken
    if tok in T.Keyword:            return "syn_keyword"
    if tok in T.Name.Function:      return "syn_name_function"
    if tok in T.Name.Class:         return "syn_name_class"
    if tok in T.Name.Builtin:       return "syn_name_builtin"
    if tok in T.Name.Decorator:     return "syn_name_decorator"
    if tok in T.Name.Tag:           return "syn_name_tag"
    if tok in T.Name.Attribute:     return "syn_name_attribute"
    if tok in T.String.Doc:         return "syn_string_doc"
    if tok in T.String:             return "syn_string"
    if tok in T.Comment:            return "syn_comment"
    if tok in T.Number:             return "syn_number"
    if tok in T.Operator:           return "syn_operator"
    if tok in T.Punctuation:        return "syn_punctuation"
    return "syn_plain"


def configure_markdown_tags(w: tk.Text) -> None:
    """Register every tag the markdown renderer emits. Call once per widget."""
    w.tag_configure("md_h1", foreground=COL_TEXT,
                    font=("Segoe UI", FS_CHAT + 5, "bold"),
                    spacing1=14, spacing3=6)
    w.tag_configure("md_h2", foreground=COL_TEXT,
                    font=("Segoe UI", FS_CHAT + 3, "bold"),
                    spacing1=12, spacing3=4)
    w.tag_configure("md_h3", foreground=COL_TEXT,
                    font=("Segoe UI", FS_CHAT + 1, "bold"),
                    spacing1=10, spacing3=3)
    w.tag_configure("md_body", foreground=COL_TEXT,
                    font=("Segoe UI", FS_CHAT), spacing3=4)
    w.tag_configure("md_bullet", foreground=COL_TEXT,
                    font=("Segoe UI", FS_CHAT),
                    lmargin1=10, lmargin2=28, spacing3=4)
    w.tag_configure("md_bullet_marker", foreground=COL_ACCENT,
                    font=("Segoe UI", FS_CHAT, "bold"),
                    lmargin1=10, lmargin2=28)
    w.tag_configure("md_blockquote", foreground=COL_MUTED,
                    font=("Segoe UI", FS_CHAT, "italic"),
                    lmargin1=18, lmargin2=18, spacing3=4)
    w.tag_configure("md_hr", foreground=COL_BORDER,
                    font=("Segoe UI", FS_SMALL),
                    spacing1=8, spacing3=8)
    w.tag_configure("md_bold", foreground=COL_TEXT,
                    font=("Segoe UI", FS_CHAT, "bold"))
    w.tag_configure("md_italic", foreground=COL_TEXT,
                    font=("Segoe UI", FS_CHAT, "italic"))
    w.tag_configure("md_inline_code", foreground="#e0b98a",
                    background="#1a1a1d", font=("Consolas", FS_CODE))
    w.tag_configure("md_link", foreground="#7aa7d9", underline=True)
    w.tag_configure("md_code_lang", foreground=COL_MUTED,
                    background=COL_CODE_BG, font=("Consolas", FS_SMALL),
                    lmargin1=14, lmargin2=14, spacing1=8, spacing3=4)
    w.tag_configure("md_code_bg", foreground=SYN_COLORS["syn_plain"],
                    background=COL_CODE_BG, font=("Consolas", FS_CODE),
                    lmargin1=14, lmargin2=14, rmargin=14, spacing3=2)
    for name, color in SYN_COLORS.items():
        w.tag_configure(name, foreground=color, background=COL_CODE_BG,
                        font=("Consolas", FS_CODE),
                        lmargin1=14, lmargin2=14, rmargin=14)


_INLINE_RE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold>\*\*[^*\n]+?\*\*)"
    r"|(?P<bold_u>__[^_\n]+?__)"
    r"|(?P<italic>\*[^*\n]+?\*)"
    r"|(?P<italic_u>_[^_\n]+?_)"
    r"|(?P<link>\[[^\]\n]+\]\([^)\s]+\))"
)


def _render_inline(w: tk.Text, text: str, base_tag: str = "md_body") -> None:
    """Insert `text` with inline markdown styling applied."""
    i = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > i:
            w.insert("end", text[i:m.start()], base_tag)
        kind = m.lastgroup
        raw = m.group()
        if kind == "code":
            w.insert("end", raw[1:-1], "md_inline_code")
        elif kind in ("bold", "bold_u"):
            w.insert("end", raw[2:-2], "md_bold")
        elif kind in ("italic", "italic_u"):
            w.insert("end", raw[1:-1], "md_italic")
        elif kind == "link":
            lm = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", raw)
            if lm:
                w.insert("end", lm.group(1), "md_link")
            else:
                w.insert("end", raw, base_tag)
        i = m.end()
    if i < len(text):
        w.insert("end", text[i:], base_tag)


def _render_code_block(w: tk.Text, lang: str, code: str) -> None:
    # Remember the range so we can attach a unique "copy" tag to the whole block.
    block_start = w.index("end-1c")
    lang_label = f" {lang}" if lang else " code"
    w.insert("end", f"{lang_label}    · click to copy\n", "md_code_lang")
    lexer = None
    if _HAS_PYGMENTS and lang:
        try:
            lexer = _pyg_get_lexer(lang.lower())
        except _PygClassNotFound:
            lexer = None
    body = code.rstrip("\n")
    if lexer is None:
        w.insert("end", body + "\n", "md_code_bg")
    else:
        for tok, val in _pyg_lex(body, lexer):
            w.insert("end", val, _pyg_to_tag(tok))
        if not body.endswith("\n"):
            w.insert("end", "\n", "md_code_bg")
    block_end = w.index("end-1c")

    # Register a unique click tag over the block for copy-on-click.
    cblocks = getattr(w, "_code_blocks", None)
    if cblocks is None:
        cblocks = {}
        w._code_blocks = cblocks
    counter = getattr(w, "_code_block_counter", 0)
    w._code_block_counter = counter + 1
    tag = f"md_code_click_{counter}"
    cblocks[tag] = code
    w.tag_add(tag, block_start, block_end)
    w.tag_bind(tag, "<Button-1>", lambda _e, _w=w, _t=tag: _copy_code_block(_w, _t))
    w.tag_bind(tag, "<Enter>", lambda _e, _w=w: _w.config(cursor="hand2"))
    w.tag_bind(tag, "<Leave>", lambda _e, _w=w: _w.config(cursor=""))


def _copy_code_block(w: tk.Text, tag: str) -> None:
    code = (getattr(w, "_code_blocks", {}) or {}).get(tag, "")
    if not code:
        return
    try:
        w.clipboard_clear()
        w.clipboard_append(code)
        # Keep the clipboard contents valid even if the selection changes.
        w.update()
    except Exception:
        return
    try:
        w.event_generate("<<CodeBlockCopied>>")
    except Exception:
        pass


def _balance_markdown(s: str) -> str:
    """Close trailing unclosed inline/fence tokens so partial text renders cleanly.

    Used during streaming: if the frontier of the buffer has an unclosed `**`
    or `` ` ``, or an open triple-backtick fence, we append a virtual closer so
    the styling shows up immediately. When the real closer arrives in a later
    delta, the re-rendered output looks the same — no visual pop.

    Single-* italic is intentionally not balanced: every stray `*` would flicker
    italic, which is worse than waiting for both asterisks.
    """
    lines = s.split("\n")
    # Triple-fence balance across the whole buffer. Match render_markdown's
    # strict check: fences must start at column 0.
    if sum(1 for l in lines if l.startswith("```")) % 2 == 1:
        lines.append("```")
    # Inline (** and `) balance on the final line only. Close inner tokens
    # (backticks) first, then outer (**) — mirrors typical nesting.
    last = lines[-1]
    single = last.replace("```", "")
    if single.count("`") % 2 == 1:
        last += "`"
    if last.count("**") % 2 == 1:
        last += "**"
    lines[-1] = last
    return "\n".join(lines)


def render_markdown(w: tk.Text, md: str) -> None:
    """Parse a markdown string and append it to `w` at the current end."""
    lines = md.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if line.startswith("```"):
            lang = line[3:].strip()
            j = i + 1
            buf: list[str] = []
            while j < n and not lines[j].startswith("```"):
                buf.append(lines[j])
                j += 1
            _render_code_block(w, lang, "\n".join(buf))
            i = j + 1
            continue

        # Headings
        if line.startswith("### "):
            w.insert("end", line[4:] + "\n", "md_h3"); i += 1; continue
        if line.startswith("## "):
            w.insert("end", line[3:] + "\n", "md_h2"); i += 1; continue
        if line.startswith("# "):
            w.insert("end", line[2:] + "\n", "md_h1"); i += 1; continue

        # Horizontal rule
        if stripped in ("---", "***", "___"):
            w.insert("end", "─" * 40 + "\n", "md_hr")
            i += 1; continue

        # Blockquote
        if line.startswith("> "):
            w.insert("end", "│ ", "md_blockquote")
            _render_inline(w, line[2:], "md_blockquote")
            w.insert("end", "\n", "md_blockquote")
            i += 1; continue

        # Bullets / numbered
        bm = re.match(r"^\s*([-*+])\s+(.*)", line)
        nm = re.match(r"^\s*(\d+)\.\s+(.*)", line)
        if bm:
            w.insert("end", "  •  ", "md_bullet_marker")
            _render_inline(w, bm.group(2), "md_bullet")
            w.insert("end", "\n", "md_bullet"); i += 1; continue
        if nm:
            w.insert("end", f"  {nm.group(1)}.  ", "md_bullet_marker")
            _render_inline(w, nm.group(2), "md_bullet")
            w.insert("end", "\n", "md_bullet"); i += 1; continue

        # Blank line
        if stripped == "":
            w.insert("end", "\n", "md_body"); i += 1; continue

        # Paragraph line
        _render_inline(w, line, "md_body")
        w.insert("end", "\n", "md_body")
        i += 1


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class SignInDialog(tk.Toplevel):
    """Modal dialog that runs the device-code flow."""

    def __init__(self, master, on_success: Callable[[Tokens], None]):
        super().__init__(master)
        self.title("Sign in to ChatGPT")
        self.configure(bg=COL_BG)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self._on_success = on_success
        self._cancel = False
        self._ui_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._start_flow()
        self.after(50, self._drain_queue)

    def _build_ui(self):
        pad = 24
        frame = tk.Frame(self, bg=COL_BG)
        frame.pack(padx=pad, pady=pad)

        tk.Label(
            frame,
            text="Sign in with ChatGPT",
            bg=COL_BG, fg=COL_TEXT,
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w")

        tk.Label(
            frame,
            text="Uses the Codex CLI OAuth client — your ChatGPT Plus/Pro subscription.",
            bg=COL_BG, fg=COL_MUTED,
            font=("Segoe UI", 9),
            wraplength=420, justify="left",
        ).pack(anchor="w", pady=(2, 14))

        self.status_var = tk.StringVar(value="Requesting device code…")
        tk.Label(
            frame, textvariable=self.status_var,
            bg=COL_BG, fg=COL_TEXT, font=("Segoe UI", 10),
        ).pack(anchor="w")

        self.code_var = tk.StringVar(value="")
        self.code_lbl = tk.Label(
            frame, textvariable=self.code_var,
            bg=COL_PANEL, fg=COL_ACCENT,
            font=("Consolas", 22, "bold"),
            padx=18, pady=10,
        )
        self.code_lbl.pack(pady=14, anchor="w")

        self.url_var = tk.StringVar(value="")
        url_lbl = tk.Label(
            frame, textvariable=self.url_var,
            bg=COL_BG, fg="#7aa7d9",
            font=("Segoe UI", 10, "underline"),
            cursor="hand2",
        )
        url_lbl.pack(anchor="w")
        url_lbl.bind("<Button-1>", self._open_url)

        tk.Label(
            frame,
            text="Never share this code. It expires in 15 minutes.",
            bg=COL_BG, fg=COL_MUTED, font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(10, 0))

        btn_row = tk.Frame(frame, bg=COL_BG)
        btn_row.pack(fill="x", pady=(14, 0))

        self.copy_btn = tk.Button(
            btn_row, text="Copy code",
            command=self._copy_code,
            bg=COL_PANEL_ALT, fg=COL_TEXT,
            activebackground=COL_PANEL, activeforeground=COL_TEXT,
            relief="flat", bd=0, padx=12, pady=6, cursor="hand2",
        )
        self.copy_btn.pack(side="left")

        self.open_btn = tk.Button(
            btn_row, text="Open in browser",
            command=self._open_url,
            bg=COL_ACCENT, fg="#1a1208",
            activebackground="#c07a4a", activeforeground="#1a1208",
            relief="flat", bd=0, padx=12, pady=6, cursor="hand2",
        )
        self.open_btn.pack(side="left", padx=(8, 0))

        cancel_btn = tk.Button(
            btn_row, text="Cancel",
            command=self._on_cancel,
            bg=COL_BG, fg=COL_MUTED,
            activebackground=COL_BG, activeforeground=COL_TEXT,
            relief="flat", bd=0, padx=12, pady=6, cursor="hand2",
        )
        cancel_btn.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _open_url(self, _event=None):
        url = self.url_var.get()
        if not url:
            return
        import webbrowser
        webbrowser.open(url)

    def _copy_code(self):
        code = self.code_var.get()
        if not code:
            return
        self.clipboard_clear()
        self.clipboard_append(code)
        self.copy_btn.config(text="Copied!")
        self.after(1200, lambda: self.copy_btn.config(text="Copy code"))

    def _on_cancel(self):
        self._cancel = True
        self.destroy()

    def _start_flow(self):
        def worker():
            try:
                dc = request_device_code()
            except Exception as e:
                self._ui_queue.put(("error", f"Failed to get device code: {e}"))
                return
            self._ui_queue.put(("code", dc))
            try:
                tokens = poll_device_authorization(dc, lambda: self._cancel)
            except Exception as e:
                self._ui_queue.put(("error", f"Sign-in failed: {e}"))
                return
            self._ui_queue.put(("success", tokens))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "code":
                    dc: DeviceCode = payload
                    self.status_var.set("Open the link and enter this code:")
                    self.code_var.set(dc.user_code)
                    self.url_var.set(dc.verification_url)
                    self._open_url()
                elif kind == "error":
                    self.status_var.set(payload)
                    self.code_lbl.config(fg=COL_ERROR)
                elif kind == "success":
                    self._on_success(payload)
                    self.destroy()
                    return
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._drain_queue)


class DarkDropdown(tk.Frame):
    """Custom dark-styled dropdown — replaces ttk.Combobox because the Windows
    ttk theme renders combos with a broken white arrow-button area and no
    way to restyle it cleanly.

    Looks like a pill: [ label · value  ▾ ]. Clicking opens a tk.Menu.
    Supports grouped items via `groups`: list of (group_name, [items]).
    If `groups` is None, falls back to a flat `values` list.
    """

    def __init__(
        self,
        master,
        *,
        label: str,
        variable: tk.StringVar,
        values: list[str] | None = None,
        groups: list[tuple[str, list[str]]] | None = None,
        on_change: Callable[[], None] | None = None,
        width: int = 18,
    ):
        super().__init__(master, bg=COL_PANEL)
        self._variable = variable
        self._on_change = on_change
        self._values = values or []
        self._groups = groups
        self._width = width

        self._btn = tk.Label(
            self,
            bg=COL_PANEL_ALT,
            fg=COL_TEXT,
            padx=10, pady=4,
            cursor="hand2",
            anchor="w",
        )
        self._btn.pack(fill="x")

        # Hover highlight
        self._btn.bind("<Enter>", lambda _e: self._btn.config(bg=COL_PANEL_HOVER))
        self._btn.bind("<Leave>", lambda _e: self._btn.config(bg=COL_PANEL_ALT))
        self._btn.bind("<Button-1>", self._open_menu)

        self._label = label
        self._refresh_label()

    def _refresh_label(self):
        text = f"{self._label}  {self._variable.get()}  ▾"
        self._btn.config(text=text, width=self._width)

    def set_values(self, values: list[str]):
        self._values = values
        self._groups = None
        if self._variable.get() not in values and values:
            self._variable.set(values[0])
        self._refresh_label()

    def _build_menu(self) -> tk.Menu:
        menu = tk.Menu(
            self,
            tearoff=0,
            bg=COL_PANEL,
            fg=COL_TEXT,
            activebackground=COL_SELECT_BG,
            activeforeground=COL_SELECT_FG,
            bd=0,
            relief="flat",
            font=("Segoe UI", 9),
        )

        def pick(value: str):
            self._variable.set(value)
            self._refresh_label()
            if self._on_change is not None:
                self._on_change()

        if self._groups:
            for i, (group_name, items) in enumerate(self._groups):
                if i > 0:
                    menu.add_separator()
                menu.add_command(
                    label=f"  {group_name}",
                    state="disabled",
                    foreground=COL_MUTED,
                    background=COL_PANEL,
                )
                for item in items:
                    check = "●  " if item == self._variable.get() else "   "
                    menu.add_command(
                        label=f"{check}{item}",
                        command=lambda v=item: pick(v),
                    )
        else:
            for item in self._values:
                check = "●  " if item == self._variable.get() else "   "
                menu.add_command(
                    label=f"{check}{item}",
                    command=lambda v=item: pick(v),
                )
        return menu

    def _open_menu(self, _event=None):
        menu = self._build_menu()
        x = self._btn.winfo_rootx()
        y = self._btn.winfo_rooty() + self._btn.winfo_height() + 2
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()


class ChatApp(tk.Tk):
    SIDEBAR_WIDTH = 240

    def __init__(self):
        super().__init__()
        self.title("ChatFast")
        self.geometry("1180x760")
        self.configure(bg=COL_BG)
        self.minsize(900, 520)

        self._ui_queue: queue.Queue = queue.Queue()
        self.tokens_ref: list[Tokens | None] = [load_tokens()]

        # Per-chat state loaded on switch
        self.current_chat_id: str | None = None
        self.history: list[dict] = []
        self.session_id = str(uuid.uuid4())

        self.model_var = tk.StringVar(value=DEFAULT_MODEL)
        self.effort_var = tk.StringVar(value=DEFAULT_EFFORT)
        self.current_assistant_text = ""
        self._reasoning_open = False
        self._reasoning_steps = 0
        self._reasoning_step_buf = ""
        self._reasoning_step_mark: str | None = None
        self._cancel_event: threading.Event | None = None
        self._streaming = False
        self._placeholder_active = False
        self._phase = ""
        self._phase_tick = 0
        self._render_pending = False
        self._title_gen_inflight = False

        self._configure_fonts()
        self._build_ui()
        self._refresh_auth_indicator()
        self.after(50, self._drain_queue)

        # Pick the newest chat, or create one.
        self._boot_chat()

        if self.tokens_ref[0] is None:
            self.after(300, self.open_sign_in)

    def _configure_fonts(self):
        self.font_body = tkfont.Font(family="Segoe UI", size=FS_CHAT)
        self.font_body_bold = tkfont.Font(family="Segoe UI", size=FS_CHAT, weight="bold")
        self.font_heading = tkfont.Font(family="Segoe UI", size=16, weight="bold")
        self.font_small = tkfont.Font(family="Segoe UI", size=FS_SMALL)
        self.font_mono = tkfont.Font(family="Consolas", size=FS_CODE)

    def _build_ui(self):
        # Top bar
        topbar = tk.Frame(self, bg=COL_BG, height=48)
        topbar.pack(fill="x", padx=0, pady=0)
        topbar.pack_propagate(False)

        title_wrap = tk.Frame(topbar, bg=COL_BG)
        title_wrap.pack(side="left", padx=18, pady=12)
        tk.Label(
            title_wrap, text="✻", bg=COL_BG, fg=COL_ACCENT,
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")
        tk.Label(
            title_wrap, text="  ChatFast",
            bg=COL_BG, fg=COL_TEXT, font=("Segoe UI", 12, "bold"),
        ).pack(side="left")

        right_wrap = tk.Frame(topbar, bg=COL_BG)
        right_wrap.pack(side="right", padx=18)

        self.auth_indicator = tk.Label(
            right_wrap, text="● signed out",
            bg=COL_BG, fg=COL_MUTED, font=self.font_small,
        )
        self.auth_indicator.pack(side="right", padx=(12, 0))

        def _mk_topbtn(text, cmd):
            b = tk.Button(
                right_wrap, text=text, command=cmd,
                bg=COL_PANEL_ALT, fg=COL_TEXT,
                activebackground=COL_PANEL_HOVER, activeforeground=COL_TEXT,
                relief="flat", bd=0, padx=12, pady=5, cursor="hand2",
                font=self.font_small,
            )
            b.bind("<Enter>", lambda _e, w=b: w.config(bg=COL_PANEL_HOVER))
            b.bind("<Leave>", lambda _e, w=b: w.config(bg=COL_PANEL_ALT))
            return b

        self.sign_btn = _mk_topbtn("Sign in", self.open_sign_in)
        self.sign_btn.pack(side="right", padx=(8, 0))

        # Separator line
        tk.Frame(self, bg=COL_BORDER, height=1).pack(fill="x")

        # Main horizontal split: sidebar + right column
        main = tk.Frame(self, bg=COL_BG)
        main.pack(fill="both", expand=True)

        # ---------- Sidebar ----------
        sidebar = tk.Frame(main, bg=COL_PANEL, width=self.SIDEBAR_WIDTH)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        side_header = tk.Frame(sidebar, bg=COL_PANEL)
        side_header.pack(fill="x", padx=12, pady=(14, 8))
        tk.Label(
            side_header, text="Chats",
            bg=COL_PANEL, fg=COL_MUTED, font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        new_chat_btn = tk.Button(
            side_header, text="+ New",
            command=self.new_chat,
            bg=COL_PANEL_ALT, fg=COL_TEXT,
            activebackground=COL_PANEL_HOVER, activeforeground=COL_TEXT,
            relief="flat", bd=0, padx=10, pady=3, cursor="hand2",
            font=self.font_small,
        )
        new_chat_btn.bind("<Enter>", lambda _e: new_chat_btn.config(bg=COL_PANEL_HOVER))
        new_chat_btn.bind("<Leave>", lambda _e: new_chat_btn.config(bg=COL_PANEL_ALT))
        new_chat_btn.pack(side="right")

        # Sidebar separator
        tk.Frame(sidebar, bg=COL_BORDER, height=1).pack(fill="x", padx=8)

        # Scrollable chat list (Canvas + inner Frame trick)
        list_wrap = tk.Frame(sidebar, bg=COL_PANEL)
        list_wrap.pack(fill="both", expand=True, padx=0, pady=(6, 12))

        self._chats_canvas = tk.Canvas(
            list_wrap, bg=COL_PANEL, highlightthickness=0, bd=0,
        )
        self._chats_inner = tk.Frame(self._chats_canvas, bg=COL_PANEL)
        self._chats_canvas.create_window((0, 0), window=self._chats_inner, anchor="nw")
        self._chats_canvas.pack(side="left", fill="both", expand=True)

        def _sync_scroll(_e=None):
            self._chats_canvas.configure(scrollregion=self._chats_canvas.bbox("all"))
            # Match inner frame width to canvas
            self._chats_canvas.itemconfigure("all", width=self._chats_canvas.winfo_width())
        self._chats_inner.bind("<Configure>", _sync_scroll)
        self._chats_canvas.bind("<Configure>", _sync_scroll)

        # Mouse wheel routing is handled by a single global binding that
        # dispatches to whichever of chat / sidebar is under the cursor.
        # (See `_on_global_mousewheel` below.)

        # Sidebar separator on the right edge
        tk.Frame(main, bg=COL_BORDER, width=1).pack(side="left", fill="y")

        # ---------- Right column: context bar + chat + input ----------
        right_col = tk.Frame(main, bg=COL_BG)
        right_col.pack(side="left", fill="both", expand=True)

        # Context bar: tokens used / model context window, with a simple ascii bar.
        ctx_bar = tk.Frame(right_col, bg=COL_PANEL, height=28)
        ctx_bar.pack(fill="x")
        ctx_bar.pack_propagate(False)
        self.ctx_left_var = tk.StringVar(value="")
        tk.Label(
            ctx_bar, textvariable=self.ctx_left_var,
            bg=COL_PANEL, fg=COL_MUTED,
            font=("Segoe UI", 8),
            padx=14, pady=6,
        ).pack(side="left")
        self.ctx_bar_var = tk.StringVar(value="")
        tk.Label(
            ctx_bar, textvariable=self.ctx_bar_var,
            bg=COL_PANEL, fg=COL_ACCENT,
            font=("Consolas", 9),
            padx=0, pady=6,
        ).pack(side="left")
        self.ctx_right_var = tk.StringVar(value="")
        tk.Label(
            ctx_bar, textvariable=self.ctx_right_var,
            bg=COL_PANEL, fg=COL_MUTED,
            font=("Segoe UI", 8),
            padx=10, pady=6,
        ).pack(side="left")
        # 1M-context toggle button (only shown when the current model supports it).
        self.ctx_toggle_btn = tk.Label(
            ctx_bar, text="",
            bg=COL_PANEL_ALT, fg=COL_TEXT,
            font=("Segoe UI", 8),
            padx=10, pady=3, cursor="hand2",
        )
        self.ctx_toggle_btn.pack(side="right", padx=(8, 14), pady=4)
        self.ctx_toggle_btn.bind("<Button-1>", lambda _e: self._toggle_context_window())
        self.ctx_toggle_btn.bind(
            "<Enter>", lambda _e: self.ctx_toggle_btn.config(bg=COL_PANEL_HOVER)
        )
        self.ctx_toggle_btn.bind(
            "<Leave>", lambda _e: self.ctx_toggle_btn.config(bg=COL_PANEL_ALT)
        )

        tk.Frame(right_col, bg=COL_BORDER, height=1).pack(fill="x")

        chat_wrap = tk.Frame(right_col, bg=COL_BG)
        chat_wrap.pack(fill="both", expand=True)

        self.chat = tk.Text(
            chat_wrap, wrap="word", bg=COL_BG, fg=COL_TEXT,
            insertbackground=COL_TEXT,
            relief="flat", bd=0, padx=42, pady=24,
            font=self.font_body, spacing1=2, spacing3=4,
            state="disabled",
        )
        self.chat.pack(side="left", fill="both", expand=True)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Dark.Vertical.TScrollbar",
            background=COL_PANEL_ALT,
            troughcolor=COL_BG,
            bordercolor=COL_BG,
            arrowcolor=COL_MUTED,
            gripcount=0,
            relief="flat",
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", COL_PANEL_HOVER)],
        )
        scrollbar = ttk.Scrollbar(
            chat_wrap, orient="vertical",
            command=self.chat.yview, style="Dark.Vertical.TScrollbar",
        )
        self.chat.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        # Structural + streaming tags. Bigger spacing1 creates clear breaks
        # between turns; labels use the accent/neutral split so the eye finds
        # user vs assistant fast.
        self.chat.tag_configure(
            "user_label",
            foreground=COL_ACCENT, font=("Segoe UI", FS_LABEL, "bold"),
            spacing1=22, spacing3=6,
        )
        self.chat.tag_configure(
            "assist_label",
            foreground=COL_TEXT, font=("Segoe UI", FS_LABEL, "bold"),
            spacing1=22, spacing3=6,
        )
        self.chat.tag_configure(
            "body",
            foreground=COL_TEXT, font=self.font_body,
            spacing3=10, lmargin1=0, lmargin2=0,
        )
        # User's own prose gets a subtle tinted background so it's visually
        # distinct from assistant replies without needing bubbles.
        self.chat.tag_configure(
            "user_body",
            foreground=COL_TEXT, font=self.font_body,
            background=COL_BG_ALT,
            lmargin1=0, lmargin2=0, spacing3=10,
        )
        # Reasoning display: a clear block with its own header + step bullets,
        # bright enough to actually read at a glance.
        reasoning_font = tkfont.Font(family="Segoe UI", size=10, slant="italic")
        self.chat.tag_configure(
            "reasoning_header",
            foreground=COL_ACCENT, font=("Segoe UI", 10, "bold"),
            spacing1=10, spacing3=4,
        )
        self.chat.tag_configure(
            "reasoning",
            foreground=COL_TEXT_DIM, font=reasoning_font,
            lmargin1=16, lmargin2=16, spacing3=2,
        )
        self.chat.tag_configure(
            "reasoning_marker",
            foreground=COL_ACCENT, font=("Segoe UI", 10, "bold"),
            lmargin1=4, spacing1=6, spacing3=2,
        )
        # Kept for backward compat in case any code path still references it.
        self.chat.tag_configure(
            "reasoning_label",
            foreground=COL_MUTED, font=self.font_small, spacing1=4,
        )
        self.chat.tag_configure("muted", foreground=COL_MUTED, font=self.font_small)
        self.chat.tag_configure("error", foreground=COL_ERROR, font=self.font_body)
        self.chat.tag_configure(
            "placeholder",
            foreground=COL_ACCENT,
            font=tkfont.Font(family="Segoe UI", size=11),
            spacing1=2, spacing3=6,
        )
        configure_markdown_tags(self.chat)
        self.chat.bind("<<CodeBlockCopied>>",
                       lambda _e: self._toast("Copied code"))

        # Bottom input area — card-style with a subtle border, generous padding.
        bot = tk.Frame(right_col, bg=COL_BG)
        bot.pack(fill="x", padx=28, pady=(0, 20))

        input_panel = tk.Frame(
            bot, bg=COL_PANEL,
            highlightbackground=COL_BORDER, highlightthickness=1,
        )
        input_panel.pack(fill="x")

        self.input_text = tk.Text(
            input_panel, height=3, wrap="word",
            bg=COL_PANEL, fg=COL_TEXT, insertbackground=COL_TEXT,
            relief="flat", bd=0,
            padx=18, pady=14,
            font=self.font_body,
        )
        self.input_text.pack(fill="x")
        self.input_text.bind("<Return>", self._on_return)
        self.input_text.bind("<Shift-Return>", lambda e: None)

        meta_row = tk.Frame(input_panel, bg=COL_PANEL)
        meta_row.pack(fill="x", padx=14, pady=(4, 12))

        self.model_dd = DarkDropdown(
            meta_row,
            label="Model",
            variable=self.model_var,
            groups=MODEL_GROUPS,
            on_change=self._on_model_changed,
            width=26,
        )
        self.model_dd.pack(side="left", padx=(0, 8))

        self.effort_dd = DarkDropdown(
            meta_row,
            label="Effort",
            variable=self.effort_var,
            values=MODEL_SPECS[DEFAULT_MODEL],
            on_change=self._on_effort_changed,
            width=14,
        )
        self.effort_dd.pack(side="left")

        self.status_var = tk.StringVar(value="")
        tk.Label(
            meta_row, textvariable=self.status_var,
            bg=COL_PANEL, fg=COL_MUTED, font=self.font_small,
        ).pack(side="left", padx=(16, 0))

        self.send_btn = tk.Button(
            meta_row, text="Send  ⏎",
            command=self._on_send_or_stop,
            bg=COL_ACCENT, fg=COL_ACCENT_FG,
            activebackground="#c07a4a", activeforeground=COL_ACCENT_FG,
            relief="flat", bd=0, padx=14, pady=4, cursor="hand2",
        )
        self.send_btn.pack(side="right")

        # Global mouse-wheel routing: scroll whichever scrollable widget the
        # cursor is currently over. Tk's default is to send wheel to the
        # focused widget, which is wrong for mixed panes like sidebar + chat.
        self.bind_all("<MouseWheel>", self._on_global_mousewheel)

    def _on_global_mousewheel(self, event):
        w = self.winfo_containing(event.x_root, event.y_root)
        delta = int(-1 * (event.delta / 120))
        cursor = w
        while cursor is not None:
            if cursor is self.chat:
                self.chat.yview_scroll(delta, "units")
                return "break"
            if cursor is self._chats_canvas or cursor is self._chats_inner:
                self._chats_canvas.yview_scroll(delta, "units")
                return "break"
            cursor = getattr(cursor, "master", None)
        return None

    def _append_greeting(self):
        self.chat.config(state="normal")
        self.chat.insert("end", "✻ ", "user_label")
        self.chat.insert("end", "New chat\n", "assist_label")
        self.chat.insert(
            "end",
            "Send a message to start. Enter to send, Shift+Enter for newline.\n",
            "muted",
        )
        self.chat.config(state="disabled")

    # ----- Chat lifecycle ---------------------------------------------------

    def _boot_chat(self) -> None:
        chats = list_chats()
        if chats:
            self._switch_to_chat(chats[0].id)
        else:
            chat = create_chat(DEFAULT_MODEL, DEFAULT_EFFORT)
            self._switch_to_chat(chat.id)

    def _switch_to_chat(self, chat_id: str) -> None:
        """Switch display + history to a given chat id."""
        # If a stream is live, cancel it before switching.
        if self._streaming:
            self.stop_streaming()
        self.current_chat_id = chat_id
        # Load chat row for model/effort
        chat = self._load_chat_row(chat_id)
        if chat is not None:
            if chat.model in MODEL_SPECS:
                self.model_var.set(chat.model)
                self.model_dd._refresh_label()
                self.effort_dd.set_values(MODEL_SPECS[chat.model])
            if chat.effort in (MODEL_SPECS.get(chat.model) or []):
                self.effort_var.set(chat.effort)
                self.effort_dd._refresh_label()
        # Load messages into history
        self.history = load_messages(chat_id)
        self.session_id = str(uuid.uuid4())
        self._render_chat_history()
        self._refresh_sidebar()
        self._update_context_bar()

    def _load_chat_row(self, chat_id: str) -> Chat | None:
        for c in list_chats():
            if c.id == chat_id:
                return c
        return None

    def _render_chat_history(self) -> None:
        """Clear the chat widget and replay self.history into it."""
        self.chat.config(state="normal")
        self.chat.delete("1.0", "end")
        # Forget any previously registered code-block copy handlers.
        if hasattr(self.chat, "_code_blocks"):
            self.chat._code_blocks.clear()
        self.chat.config(state="disabled")
        if not self.history:
            self._append_greeting()
            return
        for msg in self.history:
            role = msg.get("role")
            content = msg.get("content") or []
            text_parts: list[str] = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") in ("input_text", "output_text", "text"):
                    t = c.get("text", "")
                    if isinstance(t, str):
                        text_parts.append(t)
            text = "".join(text_parts)
            if role == "user":
                self._append_user(text)
            elif role == "assistant":
                self.chat.config(state="normal")
                self.chat.insert("end", "\nAssistant\n", "assist_label")
                render_markdown(self.chat, text)
                self.chat.insert("end", "\n", "md_body")
                self.chat.config(state="disabled")
        self.chat.see("end")

    def _refresh_sidebar(self) -> None:
        """Rebuild the chat-list buttons in the sidebar."""
        for child in self._chats_inner.winfo_children():
            child.destroy()
        for chat in list_chats():
            self._build_sidebar_item(chat)

    def _build_sidebar_item(self, chat: Chat) -> None:
        selected = chat.id == self.current_chat_id
        bg = COL_PANEL_HOVER if selected else COL_PANEL
        row = tk.Frame(self._chats_inner, bg=bg)
        row.pack(fill="x", padx=8, pady=2)

        # Selected row gets a thin accent rail on the left for visual anchor.
        rail_col = COL_ACCENT if selected else bg
        rail = tk.Frame(row, bg=rail_col, width=3)
        rail.pack(side="left", fill="y")

        fg = COL_TEXT if selected else COL_TEXT_DIM
        label = tk.Label(
            row, text=chat.title,
            bg=bg, fg=fg,
            font=("Segoe UI", FS_SMALL + 1, "bold" if selected else "normal"),
            anchor="w", padx=12, pady=9,
            cursor="hand2",
        )
        label.pack(side="left", fill="x", expand=True)

        del_btn = tk.Label(
            row, text="×",
            bg=bg, fg=COL_MUTED,
            font=("Segoe UI", 13, "bold"),
            padx=12, pady=2, cursor="hand2",
        )
        del_btn.pack(side="right")

        def on_click(_e=None, cid=chat.id):
            if cid != self.current_chat_id:
                self._switch_to_chat(cid)

        def on_row_enter(_e=None, w=row, lbl=label, btn=del_btn,
                         rl=rail, sel=selected):
            if not sel:
                for widget in (w, lbl, btn, rl):
                    widget.config(bg=COL_PANEL_ALT)

        def on_row_leave(_e=None, w=row, lbl=label, btn=del_btn,
                         rl=rail, sel=selected):
            if not sel:
                for widget in (w, lbl, btn, rl):
                    widget.config(bg=COL_PANEL)

        def on_del_click(e, cid=chat.id):
            self._delete_chat(cid)
            return "break"

        def on_double(_e=None, cid=chat.id):
            self._prompt_rename(cid)
            return "break"

        for widget in (row, label):
            widget.bind("<Button-1>", on_click)
            widget.bind("<Enter>", on_row_enter)
            widget.bind("<Leave>", on_row_leave)
            widget.bind("<Double-Button-1>", on_double)
            widget.bind("<Button-3>", lambda e, cid=chat.id: self._chat_context_menu(e, cid))

        del_btn.bind("<Button-1>", on_del_click)
        del_btn.bind("<Enter>",
                     lambda _e, b=del_btn: b.config(fg=COL_ERROR))
        del_btn.bind("<Leave>",
                     lambda _e, b=del_btn: b.config(fg=COL_MUTED))

    def _chat_context_menu(self, event, chat_id: str) -> None:
        menu = tk.Menu(
            self, tearoff=0,
            bg=COL_PANEL, fg=COL_TEXT,
            activebackground=COL_SELECT_BG, activeforeground=COL_SELECT_FG,
            bd=0, relief="flat", font=self.font_body,
        )
        menu.add_command(label="Rename…", command=lambda: self._prompt_rename(chat_id))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self._delete_chat(chat_id))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _prompt_rename(self, chat_id: str) -> None:
        # Simple inline prompt using a Toplevel.
        top = tk.Toplevel(self)
        top.title("Rename chat")
        top.configure(bg=COL_BG)
        top.transient(self)
        top.grab_set()
        tk.Label(top, text="New title:",
                 bg=COL_BG, fg=COL_TEXT, font=self.font_body).pack(padx=14, pady=(14, 4), anchor="w")
        current = ""
        for c in list_chats():
            if c.id == chat_id:
                current = c.title; break
        var = tk.StringVar(value=current)
        entry = tk.Entry(
            top, textvariable=var, bg=COL_PANEL, fg=COL_TEXT,
            insertbackground=COL_TEXT, relief="flat", font=self.font_body, width=30,
        )
        entry.pack(padx=14, pady=(0, 12))
        entry.focus_set(); entry.select_range(0, "end")

        def commit(_e=None):
            new = var.get().strip() or DEFAULT_CHAT_TITLE
            rename_chat(chat_id, new, generated=True)
            top.destroy()
            self._refresh_sidebar()
        entry.bind("<Return>", commit)
        tk.Button(
            top, text="Save", command=commit,
            bg=COL_ACCENT, fg=COL_ACCENT_FG, relief="flat", bd=0,
            padx=14, pady=4, cursor="hand2",
        ).pack(pady=(0, 14))

    def _delete_chat(self, chat_id: str) -> None:
        delete_chat(chat_id)
        if chat_id == self.current_chat_id:
            self.current_chat_id = None
            remaining = list_chats()
            if remaining:
                self._switch_to_chat(remaining[0].id)
            else:
                chat = create_chat(self.model_var.get(), self.effort_var.get())
                self._switch_to_chat(chat.id)
        else:
            self._refresh_sidebar()

    def _refresh_auth_indicator(self):
        t = self.tokens_ref[0]
        if t is None:
            self.auth_indicator.config(text="● signed out", fg=COL_MUTED)
            self.sign_btn.config(text="Sign in")
        else:
            parts = ["● signed in"]
            if t.email:
                parts.append(t.email)
            elif t.account_id:
                parts.append(t.account_id[:8] + "…")
            if t.plan_type:
                parts.append(t.plan_type)
            self.auth_indicator.config(text=" · ".join(parts), fg=COL_SUCCESS)
            self.sign_btn.config(text="Re-sign in")

    def open_sign_in(self):
        def on_success(tokens: Tokens):
            self.tokens_ref[0] = tokens
            self._refresh_auth_indicator()
            self._append_system_line("Signed in.")

        SignInDialog(self, on_success)

    def new_chat(self):
        chat = create_chat(self.model_var.get(), self.effort_var.get())
        self._switch_to_chat(chat.id)

    def _append_system_line(self, text: str, error: bool = False):
        self.chat.config(state="normal")
        tag = "error" if error else "muted"
        self.chat.insert("end", f"\n{text}\n", tag)
        self.chat.config(state="disabled")
        self.chat.see("end")

    def _append_user(self, text: str):
        self.chat.config(state="normal")
        self.chat.insert("end", "\nYou\n", "user_label")
        self.chat.insert("end", text + "\n", "user_body")
        self.chat.config(state="disabled")
        self.chat.see("end")

    def _on_model_changed(self):
        model = self.model_var.get()
        efforts = MODEL_SPECS.get(model, ["medium"])
        self.effort_dd.set_values(efforts)
        if self.current_chat_id:
            update_chat_model(self.current_chat_id, model, self.effort_var.get())
        self._update_context_bar()

    def _on_effort_changed(self):
        if self.current_chat_id:
            update_chat_model(self.current_chat_id,
                              self.model_var.get(), self.effort_var.get())
        self._update_context_bar()

    def _begin_assistant(self):
        # Two marks: `assist_start` at the whole assistant block, and
        # `assist_output_start` set on the first output delta (see
        # _append_assistant_delta). On finalize we delete only from the latter,
        # keeping any streamed reasoning preview visible.
        self.chat.config(state="normal")
        self.chat.insert("end", "\nAssistant\n", "assist_label")
        self.chat.mark_set("assist_start", "end-1c")
        self.chat.mark_gravity("assist_start", "left")
        self.chat.insert("end", "  connecting\n", "placeholder")
        self.chat.config(state="disabled")
        self.chat.see("end")
        self.current_assistant_text = ""
        self._reasoning_open = False
        self._reasoning_steps = 0
        self._reasoning_step_buf = ""
        self._reasoning_step_mark: str | None = None
        self._placeholder_active = True
        self._output_mark_set = False
        self._render_pending = False

    def _clear_placeholder(self):
        if not self._placeholder_active:
            return
        self._placeholder_active = False
        ranges = self.chat.tag_ranges("placeholder")
        if not ranges:
            return
        self.chat.config(state="normal")
        # tag_ranges returns pairs (start, end). Delete from last to first so
        # earlier indexes stay valid.
        for i in range(len(ranges) - 2, -1, -2):
            self.chat.delete(ranges[i], ranges[i + 1])
        self.chat.config(state="disabled")

    def _tick_phase(self):
        if not self._streaming:
            return
        self._phase_tick = (self._phase_tick + 1) % 4
        label = self._phase or "connecting"
        dots = "." * self._phase_tick
        self.status_var.set(f"{label}{dots}")

        if self._placeholder_active:
            frames = ["●      ", "●  ●    ", "●  ●  ●  ", "   ●  ●  "]
            glyph = frames[self._phase_tick]
            self.chat.config(state="normal")
            # Rewrite the placeholder line in place.
            ranges = self.chat.tag_ranges("placeholder")
            if ranges:
                for i in range(len(ranges) - 2, -1, -2):
                    self.chat.delete(ranges[i], ranges[i + 1])
            self.chat.insert("end", f"  {glyph}  {label}\n", "placeholder")
            self.chat.config(state="disabled")
            self.chat.see("end")
        self.after(220, self._tick_phase)

    def _open_reasoning_block(self) -> None:
        """Insert the '▌ Reasoning' header on first reasoning content."""
        if self._reasoning_open:
            return
        self.chat.config(state="normal")
        self.chat.insert("end", "▌ Reasoning\n", "reasoning_header")
        self.chat.config(state="disabled")
        self._reasoning_open = True
        self._reasoning_steps = 0

    def _append_reasoning_delta(self, delta: str):
        self._clear_placeholder()
        self._open_reasoning_block()
        # If we're getting text without a prior part.added, still show a step 1
        # marker so the content isn't a naked italic block.
        if self._reasoning_step_mark is None:
            self._insert_reasoning_step_marker()
        self._reasoning_step_buf += delta
        self._schedule_render()

    def _insert_reasoning_step_marker(self) -> None:
        self._reasoning_steps += 1
        self.chat.config(state="normal")
        if self._reasoning_steps > 1:
            self.chat.insert("end", "\n", "reasoning")
        self.chat.insert("end", f"  ▸ step {self._reasoning_steps}\n",
                         "reasoning_marker")
        # Anchor the start of this step's text so the live-renderer can
        # delete + re-insert the whole step each frame.
        mark = f"reasoning_step_{self._reasoning_steps}_start"
        self.chat.mark_set(mark, "end-1c")
        self.chat.mark_gravity(mark, "left")
        self._reasoning_step_mark = mark
        self._reasoning_step_buf = ""
        self.chat.config(state="disabled")
        self.chat.see("end")

    def _on_reasoning_step(self, kind: str) -> None:
        """Handle a reasoning_summary_part / text boundary event."""
        self._clear_placeholder()
        self._open_reasoning_block()
        if kind == "start":
            # Finalize any still-open step (shouldn't happen — server sends
            # text_done before the next part.added — but be defensive).
            if self._reasoning_step_mark is not None:
                self._finalize_reasoning_step()
            self._insert_reasoning_step_marker()
        elif kind in ("text_done", "end"):
            self._finalize_reasoning_step()

    def _finalize_reasoning_step(self) -> None:
        """Render the current step's buffer one last time (unbalanced) and close it."""
        if self._reasoning_step_mark is None:
            return
        if self._reasoning_step_buf:
            self.chat.config(state="normal")
            try:
                self.chat.delete(self._reasoning_step_mark, "end-1c")
            except tk.TclError:
                self.chat.config(state="disabled")
            else:
                _render_inline(self.chat, self._reasoning_step_buf, "reasoning")
                self.chat.insert("end", "\n", "reasoning")
                self.chat.config(state="disabled")
        self._reasoning_step_mark = None
        self._reasoning_step_buf = ""

    def _append_assistant_delta(self, delta: str):
        self._clear_placeholder()
        self.chat.config(state="normal")
        if self._reasoning_open:
            self.chat.insert("end", "\n\n", "body")
            self._reasoning_open = False
            # Reasoning is done streaming — any open step should have been
            # finalized by its text_done event already, but be defensive.
            if self._reasoning_step_mark is not None:
                self.chat.config(state="disabled")
                self._finalize_reasoning_step()
                self.chat.config(state="normal")
        if not self._output_mark_set:
            # Anchor the start of the output-text region so the live-renderer
            # and finalize step can delete only this portion.
            self.chat.mark_set("assist_output_start", "end-1c")
            self.chat.mark_gravity("assist_output_start", "left")
            self._output_mark_set = True
        self.chat.config(state="disabled")
        self.current_assistant_text += delta
        self._schedule_render()

    def _schedule_render(self) -> None:
        if self._render_pending:
            return
        self._render_pending = True
        # ~28 fps — tight enough to feel live at 1000 tok/s, loose enough to
        # let the Tk event loop breathe between full re-parses.
        self.after(35, self._flush_render)

    def _flush_render(self) -> None:
        self._render_pending = False
        if not self._streaming:
            return  # _end_assistant handles the final unbalanced render
        self._render_output_in_place()
        self._render_active_reasoning_step_in_place()

    def _viewport_touches_live_region(self, start_mark: str) -> bool:
        """True iff the chat viewport overlaps the live-streaming region.

        We only auto-scroll the chat when the user is actually watching the
        live content. If they've scrolled up into history, we leave the
        viewport alone across re-renders (no snap-to-bottom, no drift from
        fractional yview math).
        """
        try:
            live_line = int(self.chat.index(start_mark).split(".")[0])
            bottom_visible_line = int(self.chat.index("@0,10000000").split(".")[0])
        except (tk.TclError, ValueError):
            return True
        return bottom_visible_line >= live_line

    def _render_output_in_place(self) -> None:
        if not self._output_mark_set or not self.current_assistant_text:
            return
        watching = self._viewport_touches_live_region("assist_output_start")
        self.chat.config(state="normal")
        try:
            self.chat.delete("assist_output_start", "end-1c")
        except tk.TclError:
            self.chat.config(state="disabled")
            return
        render_markdown(self.chat, _balance_markdown(self.current_assistant_text))
        self.chat.config(state="disabled")
        if watching:
            self.chat.see("end")

    def _render_active_reasoning_step_in_place(self) -> None:
        if self._reasoning_step_mark is None or not self._reasoning_step_buf:
            return
        watching = self._viewport_touches_live_region(self._reasoning_step_mark)
        self.chat.config(state="normal")
        try:
            self.chat.delete(self._reasoning_step_mark, "end-1c")
        except tk.TclError:
            self.chat.config(state="disabled")
            return
        _render_inline(self.chat,
                       _balance_markdown(self._reasoning_step_buf),
                       "reasoning")
        self.chat.config(state="disabled")
        if watching:
            self.chat.see("end")

    def _end_assistant(self):
        """Finalize: keep streamed reasoning, re-render output as markdown.

        If no output text arrived (e.g. stream errored mid-reasoning), leave
        whatever was already rendered in place — don't wipe reasoning content.
        """
        # Drop any pending live-render; this method does the final, unbalanced
        # render. Any still-open reasoning step gets frozen here too.
        self._render_pending = False
        self._finalize_reasoning_step()
        self._clear_placeholder()
        self.chat.config(state="normal")
        if self.current_assistant_text and self._output_mark_set:
            try:
                self.chat.delete("assist_output_start", "end-1c")
            except tk.TclError:
                pass
            render_markdown(self.chat, self.current_assistant_text)
        self.chat.insert("end", "\n", "md_body")
        self.chat.config(state="disabled")
        self.chat.see("end")
        self._reasoning_open = False
        for m in ("assist_start", "assist_output_start"):
            try:
                self.chat.mark_unset(m)
            except tk.TclError:
                pass

    def _on_return(self, event):
        if event.state & 0x0001:  # Shift held → newline
            return None
        self.send_message()
        return "break"

    def _on_send_or_stop(self):
        if self._streaming:
            self.stop_streaming()
        else:
            self.send_message()

    def stop_streaming(self):
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._phase = "stopping"

    def _set_send_button(self, mode: str):
        # mode: "idle" | "streaming"
        if mode == "streaming":
            self.send_btn.config(
                text="Stop  ◼",
                bg=COL_PANEL_HOVER, fg=COL_TEXT,
                activebackground=COL_PANEL_ALT, activeforeground=COL_TEXT,
            )
        else:
            self.send_btn.config(
                text="Send  ⏎",
                bg=COL_ACCENT, fg=COL_ACCENT_FG,
                activebackground="#c07a4a", activeforeground=COL_ACCENT_FG,
            )

    def send_message(self):
        text = self.input_text.get("1.0", "end").strip()
        if not text:
            return
        if self.tokens_ref[0] is None:
            self._append_system_line("Sign in first.", error=True)
            self.open_sign_in()
            return
        if self.current_chat_id is None:
            chat = create_chat(self.model_var.get(), self.effort_var.get())
            self._switch_to_chat(chat.id)

        self.input_text.delete("1.0", "end")

        # Clear the greeting placeholder on first send.
        if not self.history:
            self.chat.config(state="normal")
            self.chat.delete("1.0", "end")
            self.chat.config(state="disabled")

        self._append_user(text)

        user_content = [{"type": "input_text", "text": text}]
        self.history.append({"role": "user", "content": user_content})
        save_message(self.current_chat_id, "user", user_content)
        touch_chat(self.current_chat_id)
        self._refresh_sidebar()

        self._begin_assistant()
        self._phase = "connecting"
        self._phase_tick = 0
        self.status_var.set("connecting")
        self._streaming = True
        self._cancel_event = threading.Event()
        self._set_send_button("streaming")
        self._tick_phase()

        model = self.model_var.get()
        effort = self.effort_var.get()
        history_snapshot = list(self.history)
        cancel_event = self._cancel_event

        def on_delta(d: str):
            self._ui_queue.put(("delta", d))

        def on_reasoning(d: str):
            self._ui_queue.put(("reasoning", d))

        def on_done():
            self._ui_queue.put(("done", None))

        def on_error(msg: str):
            self._ui_queue.put(("error", msg))

        def on_phase(name: str):
            self._ui_queue.put(("phase", name))

        def on_reasoning_step(kind: str):
            self._ui_queue.put(("reasoning_step", kind))

        def on_usage(usage: dict):
            self._ui_queue.put(("usage", usage))

        def worker():
            chat_stream(
                self.tokens_ref,
                history_snapshot,
                model,
                effort,
                self.session_id,
                on_delta,
                on_reasoning,
                on_done,
                on_error,
                on_phase=on_phase,
                on_reasoning_step=on_reasoning_step,
                on_usage=on_usage,
                cancel_event=cancel_event,
            )

        threading.Thread(target=worker, daemon=True).start()

    # ----- Context usage bar -------------------------------------------------

    def _effective_context_window(self) -> int:
        """Return the denominator to use for the bar for the current chat."""
        model = self.model_var.get()
        base = CONTEXT_WINDOWS.get(model, 272_000)
        # Per-chat override (set by the 1M toggle). Only respected if the model
        # actually advertises a higher max.
        if self.current_chat_id:
            chat = self._load_chat_row(self.current_chat_id)
            if chat and chat.context_window_override:
                hi = MAX_CONTEXT_WINDOWS.get(model)
                if hi and chat.context_window_override == hi:
                    return hi
        return base

    def _current_chat_total_tokens(self) -> int:
        if not self.current_chat_id:
            return 0
        chat = self._load_chat_row(self.current_chat_id)
        return chat.last_total_tokens if chat else 0

    def _update_context_bar(self) -> None:
        model = self.model_var.get()
        window = self._effective_context_window()
        used = self._current_chat_total_tokens()
        pct_left = percent_of_context_remaining(used, window)
        # ASCII bar mirrors codex-rs render_status_limit_progress_bar
        SEG = 20
        filled = round((pct_left / 100.0) * SEG)
        filled = max(0, min(SEG, filled))
        bar = "[" + "█" * filled + "░" * (SEG - filled) + "]"
        self.ctx_left_var.set(f"{model}")
        self.ctx_bar_var.set(bar)
        self.ctx_right_var.set(
            f"{format_token_count(used)} / {format_token_count(window)}  ·  {pct_left}% left"
        )
        # Toggle button — only visible when the model supports a larger window.
        hi = MAX_CONTEXT_WINDOWS.get(model)
        if hi is None:
            self.ctx_toggle_btn.config(text="")
            self.ctx_toggle_btn.pack_forget()
        else:
            on_max = window == hi
            label = "  Using 1M ⟳  " if on_max else "  → 1M context  "
            self.ctx_toggle_btn.config(text=label)
            if not self.ctx_toggle_btn.winfo_ismapped():
                self.ctx_toggle_btn.pack(side="right", padx=(8, 14), pady=4)

    def _toggle_context_window(self) -> None:
        if not self.current_chat_id:
            return
        model = self.model_var.get()
        hi = MAX_CONTEXT_WINDOWS.get(model)
        if hi is None:
            return
        chat = self._load_chat_row(self.current_chat_id)
        on_max = bool(chat and chat.context_window_override == hi)
        update_chat_context_override(
            self.current_chat_id, None if on_max else hi
        )
        self._update_context_bar()

    def _toast(self, msg: str, ms: int = 1100) -> None:
        """Tiny floating message near the cursor — doesn't disturb status_var."""
        try:
            top = tk.Toplevel(self)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            top.configure(bg=COL_PANEL_HOVER)
            tk.Label(
                top, text=msg,
                bg=COL_PANEL_HOVER, fg=COL_TEXT,
                font=self.font_small, padx=12, pady=6,
            ).pack()
            x = self.winfo_pointerx() + 12
            y = self.winfo_pointery() + 16
            top.geometry(f"+{x}+{y}")
            top.after(ms, top.destroy)
        except Exception:
            pass

    def _reset_streaming_state(self):
        self._streaming = False
        self._cancel_event = None
        self._phase = ""
        self._phase_tick = 0
        self.status_var.set("")
        self._clear_placeholder()
        self._set_send_button("idle")

    def _maybe_generate_title(self) -> None:
        """Fire a Spark title-generation request for the current chat if it's
        still using the default title and has a first user+assistant exchange."""
        if self._title_gen_inflight or not self.current_chat_id:
            return
        chat = self._load_chat_row(self.current_chat_id)
        if chat is None or chat.title_generated:
            return
        # Only kick off once we have at least one user msg and one assistant msg.
        user_text = ""
        assistant_text = ""
        for m in self.history:
            role = m.get("role")
            content = m.get("content") or []
            for c in content:
                if isinstance(c, dict):
                    t = c.get("text", "")
                    if role == "user" and not user_text and isinstance(t, str):
                        user_text = t
                    if role == "assistant" and isinstance(t, str):
                        assistant_text += t
            if user_text and assistant_text:
                break
        if not (user_text and assistant_text):
            return

        self._title_gen_inflight = True
        chat_id = self.current_chat_id
        tokens_ref = self.tokens_ref

        def worker():
            try:
                title = generate_title(tokens_ref, user_text, assistant_text)
            except Exception:
                title = ""
            self._ui_queue.put(("title", (chat_id, title)))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_queue(self):
        updated = False
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                updated = True
                if kind == "delta":
                    self._append_assistant_delta(payload)
                elif kind == "reasoning":
                    self._append_reasoning_delta(payload)
                elif kind == "phase":
                    # Server told us a new stage started. The ticker reads
                    # self._phase on its next frame; no need to redraw here.
                    self._phase = payload
                elif kind == "reasoning_step":
                    self._on_reasoning_step(payload)
                elif kind == "usage":
                    total = int((payload or {}).get("total_tokens") or 0)
                    if self.current_chat_id and total > 0:
                        update_chat_usage(self.current_chat_id, total)
                    self._update_context_bar()
                elif kind == "done":
                    self._end_assistant()
                    self._reset_streaming_state()
                    if self.current_assistant_text and self.current_chat_id:
                        assistant_content = [{
                            "type": "output_text",
                            "text": self.current_assistant_text,
                        }]
                        self.history.append({
                            "role": "assistant",
                            "content": assistant_content,
                        })
                        save_message(self.current_chat_id, "assistant", assistant_content)
                        touch_chat(self.current_chat_id)
                        self._maybe_generate_title()
                        self._refresh_sidebar()
                elif kind == "title":
                    chat_id, new_title = payload
                    if new_title:
                        rename_chat(chat_id, new_title, generated=True)
                    self._title_gen_inflight = False
                    self._refresh_sidebar()
                elif kind == "error":
                    self._end_assistant()
                    self._append_system_line(f"⚠ {payload}", error=True)
                    self._reset_streaming_state()
        except queue.Empty:
            pass
        if updated:
            # Force an immediate paint so streamed tokens visibly appear in
            # real time instead of batching to the next Tk idle cycle.
            self.update_idletasks()
        # 16ms ≈ 60fps, fast enough for Spark's 1000 tok/s output rate.
        self.after(16, self._drain_queue)


def main():
    app = ChatApp()
    app.mainloop()


if __name__ == "__main__":
    main()
