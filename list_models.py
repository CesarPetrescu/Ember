"""Fetch the live entitled model list from chatgpt.com/backend-api/codex/models
using the OAuth access token ChatFast stored in ~/.chatfast/chatfast.sqlite3.
"""
from __future__ import annotations
import json
import sqlite3
import sys
import urllib.request
import urllib.error
from pathlib import Path

DB = Path.home() / ".chatfast" / "chatfast.sqlite3"
URL = "https://chatgpt.com/backend-api/codex/models?client_version=0.60.0"

conn = sqlite3.connect(DB)
row = conn.execute(
    "SELECT access_token, refresh_token, account_id, email, plan_type FROM auth WHERE id=1"
).fetchone()
conn.close()
if not row:
    sys.exit("No auth row — sign in via ChatFast first.")

access_token, refresh_token, account_id, email, plan_type = row
print(f"Account: {email}  plan={plan_type}  account_id={account_id}")
print(f"GET {URL}\n")

req = urllib.request.Request(URL, method="GET")
req.add_header("Authorization", f"Bearer {access_token}")
req.add_header("OpenAI-Beta", "responses=experimental")
req.add_header("version", "0.60.0")
req.add_header("originator", "codex_cli_rs")
req.add_header("User-Agent", "codex_cli_rs/0.60.0 chatfast-probe")
req.add_header("Accept", "application/json")
if account_id:
    req.add_header("chatgpt-account-id", account_id)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        status = resp.status
except urllib.error.HTTPError as e:
    body = e.read()
    status = e.code

print(f"HTTP {status}")
if status != 200:
    print(body[:2000].decode("utf-8", "replace"))
    sys.exit(1)

data = json.loads(body)
models = data.get("models", [])
print(f"Returned {len(models)} models:\n")
for m in models:
    slug = m.get("slug") or m.get("id") or "?"
    display = m.get("display_name") or ""
    vis = m.get("visibility") or ""
    speed = m.get("additional_speed_tiers") or []
    efforts = [e.get("effort") for e in (m.get("supported_reasoning_levels") or [])]
    extras = []
    if speed: extras.append(f"fast_tiers={speed}")
    if vis: extras.append(f"vis={vis}")
    if efforts: extras.append(f"efforts={efforts}")
    extra = "  (" + ", ".join(extras) + ")" if extras else ""
    print(f"  - {slug:30}  {display}{extra}")
