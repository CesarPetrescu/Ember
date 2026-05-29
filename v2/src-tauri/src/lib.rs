//! Tauri v2 backend for Ember v2.
//!
//! This mirrors the existing ChatFast feature set in a Rust + Svelte stack:
//!   - Codex OAuth device-code sign-in
//!   - Chat/chunk persistence in SQLite under ~/.chatfast/chatfast.sqlite3
//!   - Streamed responses from /backend-api/codex/responses
//!   - Model + effort selection + context usage tracking

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use base64::{engine::general_purpose, Engine as _};
use bytes::Bytes;
use chrono::Utc;
use futures_util::StreamExt;
use reqwest::{
    header::{HeaderMap, HeaderValue, ACCEPT, AUTHORIZATION, CONTENT_TYPE, USER_AGENT},
    Client, StatusCode,
};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{Emitter, State};
use tokio::time;
use uuid::Uuid;

#[derive(Clone)]
struct AppState {
    db_path: PathBuf,
    cancelled: Arc<AtomicBool>,
}

#[derive(Serialize, Deserialize, Clone)]
struct Tokens {
    id_token: String,
    access_token: String,
    refresh_token: String,
    account_id: Option<String>,
    email: Option<String>,
    plan_type: Option<String>,
    last_refresh: String,
}

#[derive(Serialize)]
struct Chat {
    id: String,
    title: String,
    model: String,
    effort: String,
    created_at: String,
    updated_at: String,
    title_generated: bool,
    last_total_tokens: i64,
    context_window_override: Option<i64>,
}

#[derive(Serialize, Deserialize, Clone)]
struct MessagePart {
    #[serde(rename = "type")]
    kind: String,
    text: String,
}

#[derive(Serialize, Deserialize, Clone)]
struct Message {
    role: String,
    content: Vec<MessagePart>,
}

#[derive(Serialize, Deserialize)]
struct DeviceCode {
    verification_url: String,
    user_code: String,
    device_auth_id: String,
    interval: i64,
}

#[derive(Serialize)]
struct ModelCatalog {
    models: HashMap<String, Vec<String>>,
    groups: Vec<(String, Vec<String>)>,
    defaults: Vec<String>,
    default_model: String,
    default_effort: String,
}

#[derive(Serialize)]
struct AuthStatus {
    signed_in: bool,
    email: Option<String>,
    account_id: Option<String>,
    plan_type: Option<String>,
}

#[derive(Debug)]
enum StreamResult {
    Done(String, Option<i64>),
    RetryWithRefresh,
    Cancelled,
    Failure(String),
}

// -----------------------------------------------------------------------------
// Constants, copied from codex-rs login constants and ChatFast defaults.
// -----------------------------------------------------------------------------
const CLIENT_ID: &str = "app_EMoamEEZ73f0CkXaXp7hrann";
const DEVICE_USERCODE_URL: &str = "https://auth.openai.com/api/accounts/deviceauth/usercode";
const DEVICE_TOKEN_URL: &str = "https://auth.openai.com/api/accounts/deviceauth/token";
const OAUTH_TOKEN_URL: &str = "https://auth.openai.com/oauth/token";
const DEVICE_REDIRECT_URI: &str = "https://auth.openai.com/deviceauth/callback";
const VERIFICATION_URL: &str = "https://auth.openai.com/codex/device";
const BACKEND_RESPONSES_URL: &str = "https://chatgpt.com/backend-api/codex/responses";

const ORIGINATOR: &str = "codex_cli_rs";
const CODEX_CLI_VERSION: &str = "0.129.0";
const SPARK_MODEL: &str = "gpt-5.3-codex-spark";

const DEFAULT_MODEL: &str = "gpt-5.4";
const DEFAULT_EFFORT: &str = "medium";
const DEFAULT_CHAT_TITLE: &str = "New chat";
const TITLE_INSTRUCTIONS: &str = "You generate short chat titles. Reply with 1 to 3 words capturing the core topic. Title Case. No punctuation, no quotes, no emoji, no trailing period. Absolutely do not explain; output only the title.";

#[cfg(test)]
const BASELINE_TOKENS: i64 = 12_000;
const CONTEXT_WINDOWS: &[(&str, i64)] = &[
    ("gpt-5.5", 272_000),
    ("gpt-5.4", 272_000),
    ("gpt-5.4-mini", 272_000),
    ("gpt-5.3-codex", 272_000),
    (SPARK_MODEL, 128_000),
    ("gpt-5.2", 272_000),
];

fn max_context_windows(model: &str) -> Option<i64> {
    match model {
        "gpt-5.5" | "gpt-5.4" => Some(1_000_000),
        _ => None,
    }
}

fn model_specs() -> HashMap<String, Vec<String>> {
    let mut out = HashMap::new();
    out.insert(
        "gpt-5.5".into(),
        vec!["low".into(), "medium".into(), "high".into(), "xhigh".into()],
    );
    out.insert(
        "gpt-5.4".into(),
        vec!["low".into(), "medium".into(), "high".into(), "xhigh".into()],
    );
    out.insert(
        "gpt-5.4-mini".into(),
        vec!["low".into(), "medium".into(), "high".into(), "xhigh".into()],
    );
    out.insert(
        "gpt-5.3-codex".into(),
        vec!["low".into(), "medium".into(), "high".into(), "xhigh".into()],
    );
    out.insert(
        SPARK_MODEL.into(),
        vec!["low".into(), "medium".into(), "high".into(), "xhigh".into()],
    );
    out.insert(
        "gpt-5.2".into(),
        vec!["low".into(), "medium".into(), "high".into(), "xhigh".into()],
    );
    out
}

fn model_groups() -> Vec<(String, Vec<String>)> {
    vec![
        (
            "Recommended".into(),
            vec![
                "gpt-5.5".into(),
                "gpt-5.4".into(),
                "gpt-5.4-mini".into(),
                "gpt-5.3-codex".into(),
                SPARK_MODEL.into(),
            ],
        ),
        ("Alternative".into(), vec!["gpt-5.2".into()]),
    ]
}

fn context_window_for_model(model: &str) -> i64 {
    CONTEXT_WINDOWS
        .iter()
        .find_map(|(name, v)| if name == &model { Some(*v) } else { None })
        .unwrap_or(272_000)
}

#[cfg(test)]
fn format_token_count(n: i64) -> String {
    if n >= 1_000_000 {
        let value = (n as f64) / 1_000_000.0;
        if (value.fract() - 0.0).abs() < f64::EPSILON {
            return format!("{:.0}M", value);
        } else {
            return format!("{:.1}M", value)
                .trim_end_matches('0')
                .trim_end_matches('.')
                .to_string();
        }
    }
    if n >= 1_000 {
        let value = (n as f64) / 1_000.0;
        if (value.fract() - 0.0).abs() < f64::EPSILON {
            return format!("{:.0}k", value);
        } else {
            return format!("{:.1}k", value)
                .trim_end_matches('0')
                .trim_end_matches('.')
                .to_string();
        }
    }
    n.to_string()
}

#[cfg(test)]
fn percent_of_context_remaining(total_tokens: i64, context_window: i64) -> i64 {
    if context_window <= BASELINE_TOKENS {
        return 0;
    }
    let effective = context_window - BASELINE_TOKENS;
    let used = std::cmp::max(0, total_tokens - BASELINE_TOKENS);
    let remaining = std::cmp::max(0, effective - used);
    let pct = ((remaining as f64) * 100.0 / (effective as f64)).round();
    pct.clamp(0.0, 100.0) as i64
}

fn now_iso() -> String {
    Utc::now().to_rfc3339()
}

fn ensure_db(path: &Path) -> Result<Connection, String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }

    let conn = Connection::open(path).map_err(|e| e.to_string())?;
    conn.pragma_update(None, "journal_mode", "WAL")
        .map_err(|e| e.to_string())?;
    conn.pragma_update(None, "synchronous", "NORMAL")
        .map_err(|e| e.to_string())?;
    conn.pragma_update(None, "foreign_keys", "ON")
        .map_err(|e| e.to_string())?;

    conn.execute(
    "CREATE TABLE IF NOT EXISTS auth (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    client_id TEXT NOT NULL,\n    id_token TEXT NOT NULL,\n    access_token TEXT NOT NULL,\n    refresh_token TEXT NOT NULL,\n    account_id TEXT,\n    email TEXT,\n    plan_type TEXT,\n    last_refresh TEXT NOT NULL,\n    updated_at TEXT NOT NULL\n);",
    (),
  )
  .map_err(|e| e.to_string())?;

    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings (\n    key TEXT PRIMARY KEY,\n    value TEXT\n);",
        (),
    )
    .map_err(|e| e.to_string())?;

    conn.execute(
    "CREATE TABLE IF NOT EXISTS chats (\n    id TEXT PRIMARY KEY,\n    title TEXT NOT NULL,\n    model TEXT NOT NULL,\n    effort TEXT NOT NULL,\n    created_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL,\n    title_generated INTEGER NOT NULL DEFAULT 0,\n    last_total_tokens INTEGER NOT NULL DEFAULT 0,\n    context_window_override INTEGER\n);",
    (),
  )
  .map_err(|e| e.to_string())?;

    conn.execute(
    "CREATE TABLE IF NOT EXISTS messages (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    chat_id TEXT NOT NULL,\n    role TEXT NOT NULL,\n    content TEXT NOT NULL,\n    created_at TEXT NOT NULL,\n    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE\n);",
    (),
  )
  .map_err(|e| e.to_string())?;

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id)",
        (),
    )
    .map_err(|e| e.to_string())?;

    let mut cols: HashMap<String, bool> = HashMap::new();
    {
        let mut stmt = conn
            .prepare("PRAGMA table_info(chats)")
            .map_err(|e| e.to_string())?;
        let col_iter = stmt
            .query_map([], |row| Ok((row.get::<_, String>(1)?,)))
            .map_err(|e| e.to_string())?;
        for c in col_iter {
            if let Ok((name,)) = c {
                cols.insert(name, true);
            }
        }
    }

    if !cols.contains_key("last_total_tokens") {
        conn.execute(
            "ALTER TABLE chats ADD COLUMN last_total_tokens INTEGER NOT NULL DEFAULT 0",
            (),
        )
        .map_err(|e| e.to_string())?;
    }

    if !cols.contains_key("context_window_override") {
        conn.execute(
            "ALTER TABLE chats ADD COLUMN context_window_override INTEGER",
            (),
        )
        .map_err(|e| e.to_string())?;
    }

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_singleton ON auth(id)",
        (),
    )
    .ok();
    Ok(conn)
}

fn default_db_path() -> PathBuf {
    let mut path = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
    path.push(".chatfast");
    path.push("chatfast.sqlite3");
    path
}

#[cfg(test)]
fn b64_payload(obj: &serde_json::Value) -> String {
    let json = obj.to_string();
    general_purpose::URL_SAFE_NO_PAD.encode(json.as_bytes())
}

fn parse_id_token_info(id_token: &str) -> (Option<String>, Option<String>, Option<String>) {
    let payload = match id_token.split('.').nth(1) {
        Some(p) => p,
        None => return (None, None, None),
    };

    let mut normalized = payload.to_string();
    let missing = normalized.len() % 4;
    if missing != 0 {
        normalized.push_str(&"=".repeat(4 - missing));
    }

    let decoded = match general_purpose::URL_SAFE.decode(normalized.as_bytes()) {
        Ok(v) => v,
        Err(_) => return (None, None, None),
    };

    let claims: Value = match serde_json::from_slice(&decoded) {
        Ok(v) => v,
        Err(_) => return (None, None, None),
    };

    let auth = claims
        .get("https://api.openai.com/auth")
        .and_then(Value::as_object);
    let profile = claims
        .get("https://api.openai.com/profile")
        .and_then(Value::as_object);

    let account_id = auth
        .and_then(|a| a.get("chatgpt_account_id"))
        .and_then(Value::as_str)
        .map(|v| v.to_string());
    let plan_type = auth
        .and_then(|a| a.get("chatgpt_plan_type"))
        .and_then(Value::as_str)
        .map(|v| v.to_string());
    let email = claims
        .get("email")
        .and_then(Value::as_str)
        .map(|v| v.to_string())
        .or_else(|| {
            profile
                .and_then(|p| p.get("email"))
                .and_then(Value::as_str)
                .map(|v| v.to_string())
        });

    (account_id, email, plan_type)
}

fn default_headers() -> HeaderMap {
    let mut headers = HeaderMap::new();
    let ua = format!(
        "{} /{} ({}; {}) chatfast",
        ORIGINATOR,
        CODEX_CLI_VERSION,
        std::env::consts::OS,
        std::env::consts::ARCH
    );
    headers.insert(
        USER_AGENT,
        HeaderValue::from_str(&ua).unwrap_or_else(|_| HeaderValue::from_static("chatfast")),
    );
    headers.insert("originator", HeaderValue::from_static(ORIGINATOR));
    headers.insert(ACCEPT, HeaderValue::from_static("application/json"));
    headers
}

fn extract_text_from_item(item: &Value) -> String {
    let fallback: Vec<Value> = Vec::new();
    let content = match item.get("content").and_then(Value::as_array) {
        Some(v) => v,
        None => fallback.as_slice(),
    };
    let mut out = String::new();
    for part in content {
        let kind = part.get("type").and_then(Value::as_str).unwrap_or("");
        if kind == "output_text" || kind == "text" {
            if let Some(t) = part.get("text").and_then(Value::as_str) {
                out.push_str(t);
            }
        }
    }
    out
}

fn build_responses_request(history: &[Message], model: &str, effort: &str, stream: bool) -> Value {
    let is_spark = model == SPARK_MODEL;
    let mut reasoning = serde_json::json!({"effort": effort});
    if !is_spark {
        reasoning["summary"] = json!("auto");
    }

    let mut payload = json!({
      "model": model,
      "input": history,
      "stream": stream,
      "store": false,
      "reasoning": reasoning,
      "instructions": "You are a helpful, concise conversational assistant. Answer directly. Do not use tools or file operations.",
    });

    if !is_spark {
        payload["include"] = json!(["reasoning.encrypted_content"]);
    }

    payload
}

fn auth_headers(tokens: &Tokens, session_id: &str) -> HeaderMap {
    let mut headers = default_headers();
    headers.insert(
        AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {}", tokens.access_token)).expect("auth"),
    );
    headers.insert(
        "OpenAI-Beta",
        HeaderValue::from_static("responses=experimental"),
    );
    headers.insert("version", HeaderValue::from_static(CODEX_CLI_VERSION));
    headers.insert(
        "session_id",
        HeaderValue::from_str(session_id).unwrap_or_else(|_| HeaderValue::from_static("")),
    );
    if let Some(account_id) = &tokens.account_id {
        if let Ok(value) = HeaderValue::from_str(account_id) {
            headers.insert("chatgpt-account-id", value);
        }
    }
    headers
}

fn load_tokens(conn: &Connection) -> Result<Option<Tokens>, String> {
    let row = conn
    .query_row(
      "SELECT id_token, access_token, refresh_token, account_id, email, plan_type, last_refresh FROM auth WHERE id = 1",
      (),
      |row| {
        Ok(Tokens {
          id_token: row.get(0)?,
          access_token: row.get(1)?,
          refresh_token: row.get(2)?,
          account_id: row.get(3)?,
          email: row.get(4)?,
          plan_type: row.get(5)?,
          last_refresh: row.get(6)?,
        })
      },
    )
    .optional()
    .map_err(|e| e.to_string())?;
    Ok(row)
}

fn save_tokens(conn: &Connection, t: &Tokens) -> Result<(), String> {
    conn.execute(
    "INSERT INTO auth (id, client_id, id_token, access_token, refresh_token, account_id, email, plan_type, last_refresh, updated_at)\n     VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n     ON CONFLICT(id) DO UPDATE SET\n       client_id = excluded.client_id,\n       id_token = excluded.id_token,\n       access_token = excluded.access_token,\n       refresh_token = excluded.refresh_token,\n       account_id = excluded.account_id,\n       email = excluded.email,\n       plan_type = excluded.plan_type,\n       last_refresh = excluded.last_refresh,\n       updated_at = excluded.updated_at",
    params![
      CLIENT_ID,
      t.id_token,
      t.access_token,
      t.refresh_token,
      t.account_id,
      t.email,
      t.plan_type,
      t.last_refresh,
      now_iso(),
    ],
  )
  .map_err(|e| e.to_string())?;
    Ok(())
}

fn clear_tokens(conn: &Connection) -> Result<(), String> {
    conn.execute("DELETE FROM auth WHERE id = 1", ())
        .map_err(|e| e.to_string())?;
    Ok(())
}

fn list_chats(conn: &Connection) -> Result<Vec<Chat>, String> {
    let mut stmt = conn
    .prepare(
      "SELECT id, title, model, effort, created_at, updated_at, title_generated, last_total_tokens, context_window_override       FROM chats ORDER BY updated_at DESC",
    )
    .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map((), |r| {
            Ok(Chat {
                id: r.get(0)?,
                title: r.get(1)?,
                model: r.get(2)?,
                effort: r.get(3)?,
                created_at: r.get(4)?,
                updated_at: r.get(5)?,
                title_generated: r.get::<_, i64>(6)? == 1,
                last_total_tokens: r.get(7)?,
                context_window_override: r.get(8)?,
            })
        })
        .map_err(|e| e.to_string())?;

    let mut out = Vec::new();
    for row in rows {
        out.push(row.map_err(|e| e.to_string())?);
    }
    Ok(out)
}

fn create_chat(conn: &Connection, model: &str, effort: &str, title: &str) -> Result<Chat, String> {
    let now = now_iso();
    let cid = Uuid::new_v4().to_string();
    conn.execute(
    "INSERT INTO chats (id, title, model, effort, created_at, updated_at, title_generated) VALUES (?, ?, ?, ?, ?, ?, 0)",
    params![cid, title, model, effort, now, now],
  )
  .map_err(|e| e.to_string())?;
    Ok(Chat {
        id: cid,
        title: title.to_string(),
        model: model.to_string(),
        effort: effort.to_string(),
        created_at: now.clone(),
        updated_at: now,
        title_generated: false,
        last_total_tokens: 0,
        context_window_override: None,
    })
}

fn update_chat_usage(conn: &Connection, chat_id: &str, total_tokens: i64) -> Result<(), String> {
    conn.execute(
        "UPDATE chats SET last_total_tokens = ?, updated_at = ? WHERE id = ?",
        params![total_tokens, now_iso(), chat_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn update_chat_model(
    conn: &Connection,
    chat_id: &str,
    model: &str,
    effort: &str,
) -> Result<(), String> {
    conn.execute(
        "UPDATE chats SET model = ?, effort = ?, updated_at = ? WHERE id = ?",
        params![model, effort, now_iso(), chat_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn update_chat_title(
    conn: &Connection,
    chat_id: &str,
    title: &str,
    generated: bool,
) -> Result<(), String> {
    conn.execute(
        "UPDATE chats SET title = ?, title_generated = ?, updated_at = ? WHERE id = ?",
        params![title, if generated { 1 } else { 0 }, now_iso(), chat_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn get_chat(conn: &Connection, chat_id: &str) -> Result<Option<Chat>, String> {
    conn
    .query_row(
      "SELECT id, title, model, effort, created_at, updated_at, title_generated, last_total_tokens, context_window_override FROM chats WHERE id = ?",
      [chat_id],
      |r| {
        Ok(Chat {
          id: r.get(0)?,
          title: r.get(1)?,
          model: r.get(2)?,
          effort: r.get(3)?,
          created_at: r.get(4)?,
          updated_at: r.get(5)?,
          title_generated: r.get::<_, i64>(6)? == 1,
          last_total_tokens: r.get(7)?,
          context_window_override: r.get(8)?,
        })
      },
    )
    .optional()
    .map_err(|e| e.to_string())
}

fn update_chat_context_override(
    conn: &Connection,
    chat_id: &str,
    value: Option<i64>,
) -> Result<(), String> {
    conn.execute(
        "UPDATE chats SET context_window_override = ? WHERE id = ?",
        params![value, chat_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn touch_chat(conn: &Connection, chat_id: &str) -> Result<(), String> {
    conn.execute(
        "UPDATE chats SET updated_at = ? WHERE id = ?",
        params![now_iso(), chat_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn delete_chat(conn: &Connection, chat_id: &str) -> Result<(), String> {
    conn.execute("DELETE FROM chats WHERE id = ?", params![chat_id])
        .map_err(|e| e.to_string())?;
    Ok(())
}

fn save_message(
    conn: &Connection,
    chat_id: &str,
    role: &str,
    content: &[MessagePart],
) -> Result<(), String> {
    let text = serde_json::to_string(content).map_err(|e| e.to_string())?;
    conn.execute(
        "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        params![chat_id, role, text, now_iso()],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn load_messages(conn: &Connection, chat_id: &str) -> Result<Vec<Message>, String> {
    let mut stmt = conn
        .prepare("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id")
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([chat_id], |r| {
            let role: String = r.get(0)?;
            let content_json: String = r.get(1)?;
            let content: Vec<MessagePart> = serde_json::from_str(&content_json).unwrap_or_default();
            Ok(Message { role, content })
        })
        .map_err(|e| e.to_string())?;

    let mut out = Vec::new();
    for r in rows {
        out.push(r.map_err(|e| e.to_string())?);
    }
    Ok(out)
}

async fn http_post_json(
    client: &Client,
    url: &str,
    payload: &Value,
    headers: HeaderMap,
) -> Result<(u16, Vec<u8>), String> {
    let res = client
        .post(url)
        .headers(headers)
        .json(payload)
        .send()
        .await
        .map_err(|e| e.to_string())?;

    let status = res.status().as_u16();
    let body = res.bytes().await.map_err(|e| e.to_string())?.to_vec();
    Ok((status, body))
}

async fn http_post_form(url: &str, body: &HashMap<&str, String>) -> Result<(u16, Vec<u8>), String> {
    let client = Client::new();
    let res = client
        .post(url)
        .header(CONTENT_TYPE, "application/x-www-form-urlencoded")
        .form(body)
        .send()
        .await
        .map_err(|e| e.to_string())?;

    let status = res.status().as_u16();
    let bytes = res.bytes().await.map_err(|e| e.to_string())?.to_vec();
    Ok((status, bytes))
}

async fn refresh_tokens(conn: &Connection, current: &Tokens) -> Result<Tokens, String> {
    let mut body = HashMap::new();
    body.insert("client_id", CLIENT_ID.to_string());
    body.insert("grant_type", "refresh_token".to_string());
    body.insert("refresh_token", current.refresh_token.clone());

    let (status, bytes) = http_post_form(OAUTH_TOKEN_URL, &body).await?;
    if status != 200 {
        return Err(format!(
            "refresh failed [{}]: {}",
            status,
            String::from_utf8_lossy(&bytes)
        ));
    }

    let response: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    let new_id = response
        .get("id_token")
        .and_then(Value::as_str)
        .unwrap_or(&current.id_token)
        .to_string();
    let (aid, email, plan) = parse_id_token_info(&new_id);

    let next = Tokens {
        id_token: new_id,
        access_token: response
            .get("access_token")
            .and_then(Value::as_str)
            .unwrap_or(&current.access_token)
            .to_string(),
        refresh_token: response
            .get("refresh_token")
            .and_then(Value::as_str)
            .unwrap_or(&current.refresh_token)
            .to_string(),
        account_id: aid.or_else(|| current.account_id.clone()),
        email: email.or_else(|| current.email.clone()),
        plan_type: plan.or_else(|| current.plan_type.clone()),
        last_refresh: now_iso(),
    };

    save_tokens(conn, &next)?;
    Ok(next)
}

fn sanitize_title(raw: &str) -> String {
    let first_line = raw.lines().next().unwrap_or("").trim();
    let trimmed = first_line.trim_matches(|c: char| c.is_whitespace() || "\"'`.,:;!?".contains(c));
    trimmed
        .split_whitespace()
        .take(3)
        .collect::<Vec<_>>()
        .join(" ")
}

async fn generate_title(
    conn: &Connection,
    client: &Client,
    user_text: &str,
    assistant_text: &str,
) -> Result<String, String> {
    let history = vec![
        Message {
            role: "user".to_string(),
            content: vec![MessagePart {
                kind: "input_text".to_string(),
                text: user_text.chars().take(2000).collect(),
            }],
        },
        Message {
            role: "assistant".to_string(),
            content: vec![MessagePart {
                kind: "output_text".to_string(),
                text: assistant_text.chars().take(2000).collect(),
            }],
        },
        Message {
            role: "user".to_string(),
            content: vec![MessagePart {
                kind: "input_text".to_string(),
                text: "Title this chat in 1-3 words.".to_string(),
            }],
        },
    ];

    let mut payload = build_responses_request(&history, SPARK_MODEL, "low", true);
    payload["instructions"] = json!(TITLE_INSTRUCTIONS);

    for attempt in 0..2 {
        let tokens = load_tokens(conn)?.ok_or_else(|| "no auth".to_string())?;
        let response = client
            .post(BACKEND_RESPONSES_URL)
            .headers(auth_headers(&tokens, &Uuid::new_v4().to_string()))
            .json(&payload)
            .send()
            .await
            .map_err(|e| e.to_string())?;

        if response.status() == StatusCode::UNAUTHORIZED && attempt == 0 {
            let _ = refresh_tokens(conn, &tokens).await?;
            continue;
        }

        if response.status() != StatusCode::OK {
            return Ok(String::new());
        }

        let mut stream = response.bytes_stream();
        let mut buffer: Vec<u8> = Vec::new();
        let mut collected = String::new();

        while let Some(frame) = stream.next().await {
            let chunk = frame.map_err(|e| e.to_string())?;
            buffer.extend_from_slice(&chunk);
            while let Some(pos) = buffer.iter().position(|b| *b == b'\n') {
                let line = String::from_utf8_lossy(&buffer[..pos]).to_string();
                buffer.drain(..=pos);
                let event = match decode_stream_line(&line) {
                    Some(v) => v,
                    None => continue,
                };
                if event.get("type").and_then(Value::as_str) == Some("response.output_text.delta") {
                    if let Some(delta) = event.get("delta").and_then(Value::as_str) {
                        collected.push_str(delta);
                    }
                }
            }
        }

        return Ok(sanitize_title(&collected));
    }

    Ok(String::new())
}

fn decode_stream_line(line: &str) -> Option<Value> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return None;
    }
    if !trimmed.starts_with("data:") {
        return None;
    }
    let payload = trimmed.trim_start_matches("data:").trim();
    if payload == "[DONE]" || payload.is_empty() {
        return None;
    }
    serde_json::from_str(payload).ok()
}

async fn chat_stream_once(
    window: &tauri::Window,
    client: &Client,
    conn: &Connection,
    state: &State<'_, AppState>,
    _chat_id: &str,
    model: &str,
    effort: &str,
    history: &[Message],
) -> StreamResult {
    state.cancelled.store(false, Ordering::SeqCst);
    let token = match load_tokens(conn).and_then(|t| t.ok_or_else(|| "no auth".to_string())) {
        Ok(t) => t,
        Err(e) => return StreamResult::Failure(e),
    };

    let session_id = Uuid::new_v4().to_string();
    let payload = build_responses_request(history, model, effort, true);

    let headers = auth_headers(&token, &session_id);

    let response = match client
        .post(BACKEND_RESPONSES_URL)
        .headers(headers.clone())
        .json(&payload)
        .send()
        .await
    {
        Ok(res) => res,
        Err(e) => return StreamResult::Failure(e.to_string()),
    };

    let status = response.status();
    if status == StatusCode::UNAUTHORIZED {
        return StreamResult::RetryWithRefresh;
    }
    if status != StatusCode::OK {
        let body = match response.bytes().await {
            Ok(bytes) => String::from_utf8_lossy(&bytes).to_string(),
            Err(_) => String::new(),
        };
        let _ = window.emit(
            "chat-event",
            json!({"type":"error", "text": format!("HTTP {}: {}", status.as_u16(), body)}),
        );
        return StreamResult::Failure(format!("HTTP {}", status));
    }

    let mut stream = response.bytes_stream();
    let mut buffer: Vec<u8> = Vec::new();
    let mut got_output = false;
    let mut collected_text = String::new();
    let mut fallback_text = String::new();
    let mut total_tokens: Option<i64> = None;

    loop {
        if state.cancelled.load(Ordering::SeqCst) {
            let _ = window.emit("chat-event", json!({"type":"error", "text":"cancelled"}));
            return StreamResult::Cancelled;
        }

        match stream.next().await {
            Some(frame) => {
                let chunk: Bytes = match frame {
                    Ok(c) => c,
                    Err(e) => return StreamResult::Failure(e.to_string()),
                };
                buffer.extend_from_slice(&chunk);

                while let Some(pos) = buffer.iter().position(|b| *b == b'\n') {
                    let line = String::from_utf8_lossy(&buffer[..pos]).to_string();
                    buffer.drain(..=pos);
                    let event = match decode_stream_line(&line) {
                        Some(v) => v,
                        None => continue,
                    };

                    let etype = event.get("type").and_then(Value::as_str).unwrap_or("");
                    if etype == "response.output_text.delta" {
                        if let Some(delta) = event.get("delta").and_then(Value::as_str) {
                            got_output = true;
                            collected_text.push_str(delta);
                            let _ =
                                window.emit("chat-event", json!({"type":"delta", "text":delta}));
                        }
                    } else if etype == "response.reasoning_summary_text.delta"
                        || etype == "response.reasoning_text.delta"
                    {
                        if let Some(delta) = event.get("delta").and_then(Value::as_str) {
                            let _ = window
                                .emit("chat-event", json!({"type":"reasoning", "text":delta}));
                        }
                    } else if etype == "response.output_item.added" {
                        let item = event.get("item").and_then(Value::as_object);
                        if let Some(item) = item {
                            let kind = item.get("type").and_then(Value::as_str).unwrap_or("");
                            let _ = window.emit("chat-event", json!({"type":"phase", "phase": if kind == "reasoning" {"reasoning"} else {"writing"} }));
                        }
                    } else if etype == "response.reasoning_summary_part.added" {
                        let _ = window.emit(
                            "chat-event",
                            json!({"type":"reasoning_step", "phase":"start"}),
                        );
                    } else if etype == "response.reasoning_summary_part.done"
                        || etype == "response.reasoning_summary_text.done"
                    {
                        let _ = window.emit(
                            "chat-event",
                            json!({"type":"reasoning_step", "phase":"end"}),
                        );
                    } else if etype == "response.output_item.done" {
                        let item = event.get("item").and_then(Value::as_object);
                        if let Some(msg) = item {
                            let kind = msg.get("type").and_then(Value::as_str).unwrap_or("");
                            if kind == "message" {
                                fallback_text.push_str(&extract_text_from_item(&json!(msg)));
                            }
                        }
                    } else if etype == "response.completed" {
                        if let Some(total) = event
                            .get("response")
                            .and_then(|r| r.get("usage"))
                            .and_then(|u| u.get("total_tokens"))
                            .and_then(Value::as_i64)
                        {
                            total_tokens = Some(total);
                            let _ = window
                                .emit("chat-event", json!({"type":"usage", "total_tokens": total}));
                        }
                    } else if etype == "response.failed" {
                        let msg = event
                            .pointer("/response/error/message")
                            .and_then(Value::as_str)
                            .unwrap_or("response failed")
                            .to_string();
                        let _ = window.emit("chat-event", json!({"type":"error", "text": msg}));
                        return StreamResult::Failure(msg);
                    } else if etype == "response.incomplete" {
                        let reason = event
                            .pointer("/response/incomplete_details/reason")
                            .and_then(Value::as_str)
                            .unwrap_or("incomplete")
                            .to_string();
                        let _ = window.emit("chat-event", json!({"type":"error", "text": reason}));
                        return StreamResult::Failure(reason);
                    }
                }
            }
            None => {
                if !got_output && !fallback_text.is_empty() {
                    let _ = window.emit(
                        "chat-event",
                        json!({"type":"delta", "text": fallback_text.clone()}),
                    );
                    return StreamResult::Done(fallback_text, total_tokens);
                }
                if got_output {
                    return StreamResult::Done(collected_text, total_tokens);
                }
                return StreamResult::Failure("no output produced (empty stream)".to_string());
            }
        }
    }
}

#[tauri::command]
fn get_model_catalog() -> ModelCatalog {
    ModelCatalog {
        models: model_specs(),
        groups: model_groups(),
        defaults: vec![DEFAULT_MODEL.to_string(), DEFAULT_EFFORT.to_string()],
        default_model: DEFAULT_MODEL.to_string(),
        default_effort: DEFAULT_EFFORT.to_string(),
    }
}

#[tauri::command]
fn get_auth_status(state: State<'_, AppState>) -> Result<AuthStatus, String> {
    let conn = ensure_db(&state.db_path)?;
    let tokens = load_tokens(&conn)?;
    conn.close().map_err(|(_, e)| e.to_string())?;
    let t = tokens;
    if let Some(tok) = t {
        Ok(AuthStatus {
            signed_in: true,
            email: tok.email,
            account_id: tok.account_id,
            plan_type: tok.plan_type,
        })
    } else {
        Ok(AuthStatus {
            signed_in: false,
            email: None,
            account_id: None,
            plan_type: None,
        })
    }
}

#[tauri::command]
fn list_models() -> Vec<String> {
    model_specs().into_keys().collect()
}

#[tauri::command]
fn list_chats_cmd(state: State<'_, AppState>) -> Result<Vec<Chat>, String> {
    let conn = ensure_db(&state.db_path)?;
    let result = list_chats(&conn);
    let chats = result?;
    conn.close().map_err(|(_, e)| e.to_string())?;
    Ok(chats)
}

#[tauri::command]
fn create_chat_cmd(
    state: State<'_, AppState>,
    model: String,
    effort: String,
) -> Result<Chat, String> {
    let conn = ensure_db(&state.db_path)?;
    let chat = create_chat(&conn, &model, &effort, DEFAULT_CHAT_TITLE);
    let chat = chat?;
    conn.close().map_err(|(_, e)| e.to_string())?;
    Ok(chat)
}

#[tauri::command]
fn load_messages_cmd(state: State<'_, AppState>, chat_id: String) -> Result<Vec<Message>, String> {
    let conn = ensure_db(&state.db_path)?;
    let msgs = load_messages(&conn, &chat_id)?;
    conn.close().map_err(|(_, e)| e.to_string())?;
    Ok(msgs)
}

#[tauri::command]
fn update_chat_model_cmd(
    state: State<'_, AppState>,
    chat_id: String,
    model: String,
    effort: String,
) -> Result<(), String> {
    let conn = ensure_db(&state.db_path)?;
    update_chat_model(&conn, &chat_id, &model, &effort)?;
    conn.close().map_err(|(_, e)| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn rename_chat_cmd(
    state: State<'_, AppState>,
    chat_id: String,
    title: String,
    generated: bool,
) -> Result<(), String> {
    let conn = ensure_db(&state.db_path)?;
    update_chat_title(&conn, &chat_id, &title, generated)?;
    conn.close().map_err(|(_, e)| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn delete_chat_cmd(state: State<'_, AppState>, chat_id: String) -> Result<(), String> {
    let conn = ensure_db(&state.db_path)?;
    delete_chat(&conn, &chat_id)?;
    conn.close().map_err(|(_, e)| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn touch_chat_cmd(state: State<'_, AppState>, chat_id: String) -> Result<(), String> {
    let conn = ensure_db(&state.db_path)?;
    touch_chat(&conn, &chat_id)?;
    conn.close().map_err(|(_, e)| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn toggle_context_window_cmd(
    state: State<'_, AppState>,
    chat_id: String,
) -> Result<(i64, Option<i64>), String> {
    let conn = ensure_db(&state.db_path)?;
    let chats = list_chats(&conn)?;
    let chat = chats.into_iter().find(|c| c.id == chat_id);
    let current = chat.as_ref().and_then(|chat| chat.context_window_override);
    let model = chat
        .as_ref()
        .map(|c| c.model.clone())
        .ok_or_else(|| "chat not found".to_string())?;
    let current_base = context_window_for_model(&model);

    let next = match max_context_windows(&model) {
        Some(max) => {
            if let Some(v) = current {
                if v == max {
                    Some(current_base)
                } else {
                    Some(max)
                }
            } else {
                Some(max)
            }
        }
        None => None,
    };

    update_chat_context_override(&conn, &chat_id, next)?;
    conn.close().map_err(|(_, e)| e.to_string())?;

    let window = next.unwrap_or(current_base);
    let override_active = if let Some(v) = next { Some(v) } else { None };
    Ok((window, override_active))
}

#[tauri::command]
async fn request_device_code_cmd() -> Result<DeviceCode, String> {
    let client = Client::new();
    let mut body = HashMap::new();
    body.insert("client_id", CLIENT_ID.to_string());
    let payload = json!({"client_id": CLIENT_ID});
    let (status, body_bytes) =
        http_post_json(&client, DEVICE_USERCODE_URL, &payload, default_headers()).await?;

    if status != 200 {
        return Err(format!("device code request failed [{}]", status));
    }

    let value: Value = serde_json::from_slice(&body_bytes).map_err(|e| e.to_string())?;
    let interval = value
        .get("interval")
        .and_then(Value::as_i64)
        .unwrap_or(5)
        .max(5);

    Ok(DeviceCode {
        verification_url: VERIFICATION_URL.to_string(),
        user_code: value
            .get("user_code")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        device_auth_id: value
            .get("device_auth_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        interval,
    })
}

#[tauri::command]
async fn wait_device_authorization_cmd(code: DeviceCode) -> Result<Tokens, String> {
    let _client = Client::new();
    let deadline = std::time::Instant::now() + Duration::from_secs(15 * 60);
    let conn = ensure_db(&default_db_path())?;

    loop {
        if std::time::Instant::now() > deadline {
            conn.close().map_err(|(_, e)| e.to_string())?;
            return Err("device auth timed out after 15 minutes".to_string());
        }

        let mut payload = HashMap::new();
        payload.insert("device_auth_id", code.device_auth_id.clone());
        payload.insert("user_code", code.user_code.clone());
        let (status, bytes) = http_post_form(DEVICE_TOKEN_URL, &payload).await?;

        if status == 200 {
            let token_data: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
            let authorization_code = token_data
                .get("authorization_code")
                .and_then(Value::as_str)
                .unwrap_or("");
            let code_verifier = token_data
                .get("code_verifier")
                .and_then(Value::as_str)
                .unwrap_or("");

            let mut x = HashMap::new();
            x.insert("grant_type", "authorization_code".to_string());
            x.insert("code", authorization_code.to_string());
            x.insert("redirect_uri", DEVICE_REDIRECT_URI.to_string());
            x.insert("client_id", CLIENT_ID.to_string());
            x.insert("code_verifier", code_verifier.to_string());

            let (status2, exchange) = http_post_form(OAUTH_TOKEN_URL, &x).await?;
            if status2 != 200 {
                conn.close().map_err(|(_, e)| e.to_string())?;
                return Err(format!("token exchange failed [{}]", status2));
            }

            let data: Value = serde_json::from_slice(&exchange).map_err(|e| e.to_string())?;
            let id_token = data
                .get("id_token")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let (account_id, email, plan) = parse_id_token_info(&id_token);

            let tokens = Tokens {
                id_token: id_token.clone(),
                access_token: data
                    .get("access_token")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                refresh_token: data
                    .get("refresh_token")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                account_id,
                email,
                plan_type: plan,
                last_refresh: now_iso(),
            };
            save_tokens(&conn, &tokens)?;
            conn.close().map_err(|(_, e)| e.to_string())?;
            return Ok(tokens);
        }

        if status == 403 || status == 404 {
            time::sleep(Duration::from_secs(code.interval as u64)).await;
            continue;
        }

        conn.close().map_err(|(_, e)| e.to_string())?;
        return Err(format!(
            "device auth failed [{}]: {}",
            status,
            String::from_utf8_lossy(&bytes)
        ));
    }
}

#[tauri::command]
fn clear_tokens_cmd(state: State<'_, AppState>) -> Result<(), String> {
    let conn = ensure_db(&state.db_path)?;
    clear_tokens(&conn)?;
    conn.close().map_err(|(_, e)| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn context_window_cmd(
    state: State<'_, AppState>,
    chat_id: String,
    model: String,
) -> Result<i64, String> {
    let conn = ensure_db(&state.db_path)?;
    let chat = {
        let chats = list_chats(&conn)?;
        chats.into_iter().find(|c| c.id == chat_id)
    };
    let model_override = model_specs().into_keys();
    let selected = chat.map(|c| c.context_window_override).unwrap_or(None);
    let base = context_window_for_model(&model);
    let result = if let Some(v) = selected {
        if v == base {
            base
        } else if let Some(max) = max_context_windows(&model) {
            if max == v {
                max
            } else {
                base
            }
        } else {
            base
        }
    } else {
        base
    };
    conn.close().map_err(|(_, e)| e.to_string())?;
    drop(model_override);
    Ok(result)
}

#[tauri::command]
fn send_message(
    window: tauri::Window,
    state: State<'_, AppState>,
    chat_id: String,
    text: String,
    model: String,
    effort: String,
) -> Result<(), String> {
    let conn = ensure_db(&state.db_path)?;

    let parsed_effort = if model_specs().get(&model).is_some() && effort.is_empty() {
        "medium".to_string()
    } else {
        effort
    };

    let mut history = load_messages(&conn, &chat_id)?;
    let user_payload = vec![MessagePart {
        kind: "input_text".to_string(),
        text: text.clone(),
    }];

    save_message(&conn, &chat_id, "user", &user_payload)?;
    history.push(Message {
        role: "user".to_string(),
        content: user_payload,
    });

    touch_chat(&conn, &chat_id)?;

    // Notify UI immediately we have local user content on disk.
    let _ = window.emit(
        "chat-event",
        json!({"type":"user", "text": text, "chat_id": chat_id.clone()}),
    );

    let result = loop {
        let stream_result = tauri::async_runtime::block_on(chat_stream_once(
            &window,
            &Client::new(),
            &conn,
            &state,
            &chat_id,
            &model,
            &parsed_effort,
            &history,
        ));
        match stream_result {
            StreamResult::Done(raw_text, usage) => {
                if !raw_text.is_empty() {
                    let assistant_payload = vec![MessagePart {
                        kind: "output_text".to_string(),
                        text: raw_text.clone(),
                    }];
                    save_message(&conn, &chat_id, "assistant", &assistant_payload)?;
                    if let Some(total) = usage {
                        update_chat_usage(&conn, &chat_id, total)?;
                    }
                    if let Some(chat) = get_chat(&conn, &chat_id)? {
                        if !chat.title_generated && chat.title == DEFAULT_CHAT_TITLE {
                            if let Ok(title) = tauri::async_runtime::block_on(generate_title(
                                &conn,
                                &Client::new(),
                                &text,
                                &raw_text,
                            )) {
                                if !title.is_empty() {
                                    let _ = update_chat_title(&conn, &chat_id, &title, true);
                                }
                            }
                        }
                    }
                }
                let _ = window.emit("chat-event", json!({"type":"done"}));
                break Ok(());
            }
            StreamResult::RetryWithRefresh => {
                let existing = load_tokens(&conn)?.ok_or("no auth")?;
                let refreshed = tauri::async_runtime::block_on(refresh_tokens(&conn, &existing))?;
                let _ = state.cancelled.store(false, Ordering::SeqCst);
                if refreshed.access_token.is_empty() {
                    break Err("token refresh failed".to_string());
                }
                continue;
            }
            StreamResult::Cancelled => break Err("cancelled".to_string()),
            StreamResult::Failure(msg) => {
                break Err(msg);
            }
        }
    };

    conn.close().map_err(|(_, e)| e.to_string())?;
    result
}

#[tauri::command]
fn stop_stream(state: State<'_, AppState>) {
    state.cancelled.store(true, Ordering::SeqCst);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            db_path: default_db_path(),
            cancelled: Arc::new(AtomicBool::new(false)),
        })
        .invoke_handler(tauri::generate_handler![
            get_model_catalog,
            get_auth_status,
            list_models,
            list_chats_cmd,
            create_chat_cmd,
            load_messages_cmd,
            update_chat_model_cmd,
            rename_chat_cmd,
            delete_chat_cmd,
            touch_chat_cmd,
            toggle_context_window_cmd,
            context_window_cmd,
            request_device_code_cmd,
            wait_device_authorization_cmd,
            clear_tokens_cmd,
            send_message,
            stop_stream,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn format_and_percent_math() {
        assert_eq!(format_token_count(999), "999");
        assert_eq!(format_token_count(1_000), "1k");
        assert_eq!(format_token_count(1_200), "1.2k");
        assert_eq!(format_token_count(12_300_000), "12.3M");

        assert_eq!(percent_of_context_remaining(12_000, 272_000), 100);
        assert_eq!(percent_of_context_remaining(272_000, 272_000), 0);
        assert_eq!(percent_of_context_remaining(100_000, 272_000), 66);
    }

    #[test]
    fn parse_id_token_claims() {
        let header = b64_payload(&json!({"alg": "none", "typ": "JWT"}));
        let payload = b64_payload(&json!({
          "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-123",
            "chatgpt_plan_type": "pro",
          },
          "email": "owner@example.com"
        }));
        let token = format!("{}.{}.", header, payload);
        let (account_id, email, plan) = parse_id_token_info(&token);
        assert_eq!(account_id.as_deref(), Some("acct-123"));
        assert_eq!(email.as_deref(), Some("owner@example.com"));
        assert_eq!(plan.as_deref(), Some("pro"));
    }

    #[test]
    fn db_workflow_smoke() {
        use tempfile::tempdir;
        let dir = tempdir().expect("tmp");
        let path = dir.path().join("chatfast.sqlite3");

        let conn = ensure_db(&path).expect("db");
        let chat = create_chat(&conn, DEFAULT_MODEL, DEFAULT_EFFORT, "Test").expect("chat");
        let chats = list_chats(&conn).expect("list");
        assert!(!chats.is_empty());
        assert_eq!(chats[0].id, chat.id);

        save_message(
            &conn,
            &chat.id,
            "user",
            &[MessagePart {
                kind: "input_text".into(),
                text: "hello".into(),
            }],
        )
        .expect("msg");

        let messages = load_messages(&conn, &chat.id).expect("load");
        assert_eq!(messages.len(), 1);

        let now = now_iso();
        update_chat_usage(&conn, &chat.id, 512).expect("usage");
        let after = list_chats(&conn).expect("after");
        assert_eq!(after[0].last_total_tokens, 512);
        assert!(after[0].updated_at >= now);

        delete_chat(&conn, &chat.id).expect("del");
        assert!(list_chats(&conn).expect("left").is_empty());
    }
}
