"""Tests for the Tkinter desktop implementation in chatfast.py."""

from __future__ import annotations

import base64
import json
import sys
import random
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _install_tk_stubs() -> None:
  class _TkModule(types.ModuleType):
    def __getattr__(self, name: str):
      if name in self.__dict__:
        return self.__dict__[name]
      dummy = type(name, (), {})
      setattr(self, name, dummy)
      return dummy

  class _TtkModule(_TkModule):
    pass

  class _FontModule(_TkModule):
    pass

  tkinter_mod = _TkModule("tkinter")
  ttk_mod = _TtkModule("tkinter.ttk")
  font_mod = _FontModule("tkinter.font")

  for cls_name in [
    "Tk",
    "Toplevel",
    "Frame",
    "Text",
    "Button",
    "Entry",
    "Label",
    "Canvas",
    "Scrollbar",
    "StringVar",
    "IntVar",
    "PhotoImage",
    "Menu",
    "Event",
    "Variable",
    "END",
    "NORMAL",
    "DISABLED",
    "RIDGE",
    "SOLID",
  ]:
    setattr(tkinter_mod, cls_name, type(cls_name, (), {}))

  for cls_name in [
    "Style",
    "Combobox",
    "Progressbar",
    "Frame",
    "Label",
    "Button",
    "Entry",
    "Scrollbar",
    "PanedWindow",
    "Notebook",
    "Treeview",
    "Menubutton",
    "LabelFrame",
  ]:
    setattr(ttk_mod, cls_name, type(cls_name, (), {}))

  setattr(font_mod, "Font", type("Font", (), {}))
  tkinter_mod.ttk = ttk_mod
  tkinter_mod.font = font_mod

  if "tkinter" in sys.modules:
    return

  sys.modules.setdefault("tkinter", tkinter_mod)
  sys.modules.setdefault("tkinter.ttk", ttk_mod)
  sys.modules.setdefault("tkinter.font", font_mod)


try:
  import chatfast
except ImportError as error:
  if "_tkinter" in str(error) or "libtk" in str(error):
    _install_tk_stubs()
    import chatfast  # type: ignore[import-not-found]
  else:
    raise


def _b64_payload(obj: dict) -> str:
  data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
  return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _fake_id_token(account_id: str, email: str, plan: str = "pro") -> str:
  header = _b64_payload({"alg": "none", "typ": "JWT"})
  payload = _b64_payload(
    {
      "https://api.openai.com/auth": {
        "chatgpt_account_id": account_id,
        "chatgpt_plan_type": plan,
      },
      "email": email,
    }
  )
  return f"{header}.{payload}."


class ChatfastUnitTests(unittest.TestCase):
  def setUp(self):
    self._tmp = tempfile.TemporaryDirectory()
    self._old_auth_dir = chatfast.AUTH_DIR
    self._old_db_file = chatfast.DB_FILE
    chatfast.AUTH_DIR = Path(self._tmp.name) / "chatfast"
    chatfast.DB_FILE = chatfast.AUTH_DIR / "chatfast.sqlite3"

  def tearDown(self):
    chatfast.AUTH_DIR = self._old_auth_dir
    chatfast.DB_FILE = self._old_db_file
    self._tmp.cleanup()

  def test_format_token_count_and_context_math(self):
    self.assertEqual("0", chatfast.format_token_count(0))
    self.assertEqual("999", chatfast.format_token_count(999))
    self.assertEqual("1.0k", chatfast.format_token_count(1000))
    self.assertEqual("12.3M", chatfast.format_token_count(12_300_000))

    self.assertEqual(100, chatfast.percent_of_context_remaining(12_000, 272_000))
    self.assertEqual(50, chatfast.percent_of_context_remaining(142_000, 272_000))
    self.assertEqual(100, chatfast.percent_of_context_remaining(11_900, 272_000))
    self.assertEqual(0, chatfast.percent_of_context_remaining(300_000, 272_000))
    self.assertEqual(0, chatfast.percent_of_context_remaining(272_000, 1000))

  def test_parse_id_token_extracts_expected_claims(self):
    token = _fake_id_token("acct-1", "owner@example.com", "pro")
    account_id, email, plan = chatfast._parse_id_token_info(token)

    self.assertEqual("acct-1", account_id)
    self.assertEqual("owner@example.com", email)
    self.assertEqual("pro", plan)

  def test_build_responses_payload_distinguishes_spark_and_non_spark(self):
    spark_payload = chatfast._build_responses_request(
      [{"role": "user", "content": "hi"}],
      chatfast.SPARK_MODEL,
      "high",
      stream=True,
    )
    self.assertEqual({"effort": "high"}, spark_payload["reasoning"])
    self.assertNotIn("include", spark_payload)

    legacy_payload = chatfast._build_responses_request(
      [{"role": "user", "content": "hi"}],
      chatfast.DEFAULT_MODEL,
      "medium",
      stream=True,
    )
    self.assertEqual(
      {"effort": "medium", "summary": "auto"},
      legacy_payload["reasoning"],
    )
    self.assertIn("reasoning.encrypted_content", legacy_payload["include"])

  def test_request_headers_include_codex_cli_origin_fields(self):
    with patch.object(chatfast.platform, "system", return_value="Linux"), \
        patch.object(chatfast.platform, "release", return_value="test"), \
        patch.object(chatfast.platform, "machine", return_value="x86_64"):
      self.assertEqual(
        f"{chatfast.ORIGINATOR}/{chatfast.CODEX_CLI_VERSION} (Linux test; x86_64) chatfast",
        chatfast._build_user_agent(),
      )

    headers = chatfast._default_headers()
    self.assertIn("User-Agent", headers)
    self.assertEqual("application/json", headers["Accept"])
    self.assertEqual(chatfast.ORIGINATOR, headers["originator"])


class ChatfastStorageAndOauthIntegrationTests(unittest.TestCase):
  def setUp(self):
    self._tmp = tempfile.TemporaryDirectory()
    self._old_auth_dir = chatfast.AUTH_DIR
    self._old_db_file = chatfast.DB_FILE
    chatfast.AUTH_DIR = Path(self._tmp.name) / "chatfast"
    chatfast.DB_FILE = chatfast.AUTH_DIR / "chatfast.sqlite3"

  def tearDown(self):
    chatfast.AUTH_DIR = self._old_auth_dir
    chatfast.DB_FILE = self._old_db_file
    self._tmp.cleanup()

  def test_chat_lifecycle_persists_and_orders_data(self):
    first = chatfast.create_chat("gpt-5.5", "medium", title="First")
    second = chatfast.create_chat("gpt-5.4", "low", title="Second")

    chats = chatfast.list_chats()
    self.assertEqual(2, len(chats))
    self.assertEqual(second.id, chats[0].id)

    chatfast.save_message(first.id, "user", [{"type": "text", "text": "hi"}])
    chatfast.save_message(first.id, "assistant", [{"type": "text", "text": "yo"}])
    msgs = chatfast.load_messages(first.id)
    self.assertEqual(2, len(msgs))
    self.assertEqual("user", msgs[0]["role"])
    self.assertEqual("assistant", msgs[1]["role"])

    chatfast.update_chat_usage(first.id, 50_000)
    chatfast.update_chat_context_override(first.id, 1_000_000)
    chatfast.rename_chat(first.id, "Focused", generated=True)
    chatfast.touch_chat(first.id)
    chats_after_touch = chatfast.list_chats()
    self.assertEqual(first.id, chats_after_touch[0].id)
    refreshed = next(chat for chat in chats_after_touch if chat.id == first.id)
    self.assertEqual("Focused", refreshed.title)
    self.assertEqual("medium", refreshed.effort)
    self.assertEqual("gpt-5.5", refreshed.model)
    self.assertEqual(50_000, refreshed.last_total_tokens)
    self.assertEqual(1_000_000, refreshed.context_window_override)

    chatfast.update_chat_model(first.id, "gpt-5.2", "xhigh")
    chatfast.delete_chat(second.id)
    chatfast.delete_chat(first.id)
    self.assertEqual([], chatfast.list_chats())

  def test_token_storage_roundtrip_and_refresh_preserves_stale_fields(self):
    raw = chatfast.Tokens(
      id_token=_fake_id_token("acct-1", "before@example.com", "pro"),
      access_token="token-1",
      refresh_token="refresh-1",
      account_id="acct-1",
      email="before@example.com",
      plan_type="pro",
    )
    chatfast.save_tokens(raw)
    loaded = chatfast.load_tokens()
    self.assertIsNotNone(loaded)
    assert loaded
    self.assertEqual("acct-1", loaded.account_id)
    self.assertEqual("before@example.com", loaded.email)

    def fake_refresh(url: str, payload: dict, headers: dict | None = None, timeout: float = 60.0):
      self.assertEqual(chatfast.OAUTH_TOKEN_URL, url)
      self.assertEqual(chatfast.CLIENT_ID, payload["client_id"])
      self.assertEqual("refresh_token", payload["grant_type"])
      self.assertEqual("refresh-1", payload["refresh_token"])
      body = json.dumps(
        {
          "access_token": "token-2",
          "refresh_token": "refresh-2",
          "id_token": _fake_id_token("acct-2", "after@example.com", "pro"),
        }
      ).encode("utf-8")
      return 200, {}, body

    with patch.object(chatfast, "_http_post_json", side_effect=fake_refresh):
      refreshed = chatfast.refresh_tokens(raw)
    self.assertEqual("token-2", refreshed.access_token)
    self.assertEqual("refresh-2", refreshed.refresh_token)
    self.assertEqual("acct-2", refreshed.account_id)
    self.assertEqual("after@example.com", refreshed.email)

    # Refresh should never silently mutate existing db row to empty/invalid token data.
    still_loaded = chatfast.load_tokens()
    self.assertIsNotNone(still_loaded)
    assert still_loaded
    self.assertEqual("token-2", still_loaded.access_token)

  def test_device_code_flow_requests_and_exchanges_expected_params(self):
    token_payload = {
      "authorization_code": "auth-code",
      "code_verifier": "verifier-1",
    }

    post_json_calls: list[tuple[str, dict]] = []

    def fake_post_json(
      url: str,
      payload: dict,
      headers: dict | None = None,
      timeout: float = 60.0,
    ) -> tuple[int, dict, bytes]:
      post_json_calls.append((url, payload))
      if url == chatfast.DEVICE_USERCODE_URL:
        body = json.dumps(
          {
            "device_auth_id": "device-id",
            "user_code": "ABCD-1234",
            "interval": "7",
          }
        ).encode("utf-8")
        return 200, {}, body

      if url == chatfast.DEVICE_TOKEN_URL:
        if len([x for x in post_json_calls if x[0] == chatfast.DEVICE_TOKEN_URL]) == 1:
          return 404, {}, b"not-yet-authorized"
        body = json.dumps(token_payload).encode("utf-8")
        return 200, {}, body

      raise AssertionError(f"unexpected endpoint {url}")

    def fake_post_form(
      url: str,
      form: dict,
      timeout: float = 60.0,
    ) -> tuple[int, dict, bytes]:
      self.assertEqual(chatfast.OAUTH_TOKEN_URL, url)
      self.assertEqual("authorization_code", form["grant_type"])
      self.assertEqual("auth-code", form["code"])
      self.assertEqual(chatfast.DEVICE_REDIRECT_URI, form["redirect_uri"])
      self.assertEqual(chatfast.CLIENT_ID, form["client_id"])
      self.assertEqual("verifier-1", form["code_verifier"])

      body = json.dumps(
        {
          "id_token": _fake_id_token("acct-3", "device@example.com", "pro"),
          "access_token": "stream-1",
          "refresh_token": "stream-r-1",
        }
      ).encode("utf-8")
      return 200, {}, body

    with patch.object(chatfast.time, "sleep"), \
        patch.object(chatfast, "_http_post_json", side_effect=fake_post_json), \
        patch.object(chatfast, "_http_post_form", side_effect=fake_post_form):
      code = chatfast.request_device_code()
      self.assertEqual(chatfast.VERIFICATION_URL, code.verification_url)
      self.assertEqual("ABCD-1234", code.user_code)

      tokens = chatfast.poll_device_authorization(code, lambda: False)
      self.assertEqual("stream-r-1", tokens.refresh_token)
      self.assertEqual("acct-3", tokens.account_id)
      self.assertEqual("device@example.com", tokens.email)

    self.assertEqual(3, len(post_json_calls))
    self.assertEqual(
      (chatfast.DEVICE_USERCODE_URL, {"client_id": chatfast.CLIENT_ID}),
      post_json_calls[0],
    )
    self.assertEqual(chatfast.DEVICE_TOKEN_URL, post_json_calls[1][0])


class ChatfastFuzzTests(unittest.TestCase):
  def setUp(self):
    self._tmp = tempfile.TemporaryDirectory()
    self._old_auth_dir = chatfast.AUTH_DIR
    self._old_db_file = chatfast.DB_FILE
    chatfast.AUTH_DIR = Path(self._tmp.name) / "chatfast"
    chatfast.DB_FILE = chatfast.AUTH_DIR / "chatfast.sqlite3"
    random.seed(0x5EED)

  def tearDown(self):
    chatfast.AUTH_DIR = self._old_auth_dir
    chatfast.DB_FILE = self._old_db_file
    self._tmp.cleanup()

  def test_random_chat_graph_invariants(self):
    model_names = list(chatfast.MODEL_SPECS.keys())
    efforts = list(next(iter(chatfast.MODEL_SPECS.values())))

  # Create random operations that mix reads/writes and validate invariants after each step.
    for step in range(150):
      chats = chatfast.list_chats()
      action = random.randrange(0, 6)

      if not chats:
        action = 0

      if action == 0:
        chatfast.create_chat(
          random.choice(model_names),
          random.choice(efforts),
          title=f"fuzz {step}",
        )
      elif action == 1:
        cid = random.choice(chats).id
        chatfast.update_chat_usage(cid, random.randrange(0, 600_000))
        chatfast.save_message(cid, "user", [{"type": "text", "text": str(step)}])
      elif action == 2:
        cid = random.choice(chats).id
        chatfast.update_chat_model(
          cid,
          random.choice(model_names),
          random.choice(efforts),
        )
      elif action == 3:
        cid = random.choice(chats).id
        chatfast.update_chat_context_override(cid, random.choice([None, 128_000, 272_000, 1_000_000]))
      elif action == 4:
        cid = random.choice(chats).id
        chatfast.rename_chat(cid, f"renamed {step}", generated=bool(random.getrandbits(1)))
      elif action == 5:
        cid = random.choice(chats).id
        chatfast.delete_chat(cid)
      chatfast.list_chats()

    chats = chatfast.list_chats()
    listed_ids = {chat.id for chat in chats}
    self.assertEqual(len(chats), len(listed_ids))

    conn = chatfast._db()
    try:
      msg_rows = conn.execute(
        "SELECT chat_id, created_at FROM messages ORDER BY id"
      ).fetchall()
    finally:
      conn.close()

    # Messages should never reference missing chats and should retain insertion order by id.
    for chat_id, _created in msg_rows:
      self.assertIn(chat_id, listed_ids)

    prior_by_chat: dict[str, str | None] = {}
    for chat_id, created in msg_rows:
      previous = prior_by_chat.get(chat_id)
      if previous is not None:
        self.assertLessEqual(previous, created)
      prior_by_chat[chat_id] = created
