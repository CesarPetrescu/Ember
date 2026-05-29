# Ember

Desktop ChatGPT Pro client built on the Codex OAuth flow.

Ember is a Tkinter desktop app that talks to the ChatGPT / Codex backend
using the same OAuth device-code flow as the official `codex` CLI. If you
already pay for ChatGPT Pro, Ember uses that subscription directly — no
extra API key, no extra subscription.

## Features

- Live streaming markdown — headings, bullets, bold/italic, inline code,
  fenced code blocks with pygments syntax highlighting. Partial tokens
  auto-close during streaming so formatting develops as the model writes.
- Reasoning-step previews — `▌ Reasoning` header, per-step bullets, italic
  body. Kept visible after the final answer renders.
- Context-usage bar at the top of the chat area: tokens used / model
  window, percent remaining. Per-chat 1M-context toggle for `gpt-5.5` and
  `gpt-5.4`.
- All models entitled on your account (probed from `/codex/models`):
  `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`,
  `gpt-5.3-codex-spark`, `gpt-5.2`.
- Multi-chat sidebar — click to switch, `×` to delete, double-click to
  rename. First turn auto-titles the chat in 1-3 words via Spark.
- Click any rendered code block to copy it.

## Requirements

- Python 3.10+
- `pip install pygments`
- A ChatGPT account that can sign in to the Codex CLI

## Run

```bash
python chatfast.py
```

First launch opens the device-code sign-in. Tokens live in
`~/.chatfast/chatfast.sqlite3`.

## Caveats

- `gpt-5.3-codex-spark` runs on Cerebras and rejects `reasoning.summary`
  at the protocol level — no reasoning preview for Spark.
- Reasoning previews for other models only surface at higher effort
  (`high` / `xhigh`). At `medium` on short prompts the backend often skips
  summary deltas entirely.
- Uses OpenAI's internal `/codex/responses` endpoint via the Codex OAuth
  device flow — same path the real `codex` CLI uses. Treat it as
  unofficial and subject to change.
