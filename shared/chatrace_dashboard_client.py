"""
chatrace_dashboard_client.py — Internal ConnectOp dashboard API
================================================================
Reverse-engineered from the newapp.connectop.co.il browser session.

WHY THIS EXISTS:
  The public ChatRace API (api.chatrace.com) does NOT expose conversation
  reading — only sending messages and managing contacts. To read incoming
  messages without waiting for a webhook, we use the INTERNAL API that the
  ConnectOp dashboard UI uses.

CAVEATS:
  • This is NOT a documented/supported API — it can change without notice.
  • Auth is via a JWT token stored in a browser cookie (10-day expiry).
  • When the token expires, sign in to newapp.connectop.co.il in a browser,
    open DevTools → Network → grab the `token` cookie → update .env.
  • Don't hammer it — be respectful, throttle if doing bulk reads.

PROTOCOL (POST /php/user.php):
  Content-Type: application/x-www-form-urlencoded
  Body:    param=<JSON-encoded array with single dict>
  Cookies: token=<JWT>, last_page_id=<account_id>, lang=he

  param structure (a list with one dict):
    [{
      "id":         "<contact_phone_or_id>",
      "page_id":    "<account_id>",
      "op":         "conversations" | "contacts" | "users" | ...,
      "op1":        "get" | "list" | ...,
      "offset":     0,
      "limit":      20,
      "expand":     { "comments":{}, "refs":{}, "orders":{}, ... },
      "pageName":   "inbox"
    }]
"""
from __future__ import annotations
import os
import json
import time
import logging
from typing import Optional, Union, List, Dict, Any
from pathlib import Path

import requests

log = logging.getLogger("chatrace_dashboard")


class ChatRaceDashboardError(Exception):
    """Raised when the internal dashboard API returns an error."""


class ChatRaceDashboardClient:
    """
    Read-only-ish client for ConnectOp's internal dashboard API.
    Lets you fetch conversation history that the public API doesn't expose.
    """

    DEFAULT_BASE_URL = "https://newapp.connectop.co.il"

    def __init__(
        self,
        token: str,
        account_id: Union[str, int],
        user_id: Union[str, int] = "",
        base_url: str = DEFAULT_BASE_URL,
        extra_cookies: Optional[Dict[str, str]] = None,
    ):
        if not token:
            raise ValueError("token is required")
        self.token = token
        self.account_id = str(account_id)
        self.user_id = str(user_id) if user_id else ""
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        # Cookies that the dashboard expects. Most are optional but token +
        # last_page_id are critical, and `lang=he` keeps responses in Hebrew.
        self._session.cookies.update({
            "token":        self.token,
            "last_page_id": self.account_id,
            "lang":         "he",
        })
        if extra_cookies:
            self._session.cookies.update(extra_cookies)

    # ── Class helpers ───────────────────────────────────────────────
    @classmethod
    def from_env(cls) -> "ChatRaceDashboardClient":
        """Build a client from .env: CHATRACE_DASHBOARD_TOKEN / _ACCOUNT_ID / _USER_ID."""
        token = os.environ.get("CHATRACE_DASHBOARD_TOKEN", "")
        account_id = os.environ.get("CHATRACE_DASHBOARD_ACCOUNT_ID", "")
        user_id = os.environ.get("CHATRACE_DASHBOARD_USER_ID", "")
        base_url = os.environ.get("CHATRACE_DASHBOARD_BASE_URL", cls.DEFAULT_BASE_URL)
        if not token or not account_id:
            raise RuntimeError(
                "CHATRACE_DASHBOARD_TOKEN / _ACCOUNT_ID missing in env. "
                "Get them from newapp.connectop.co.il DevTools cookies."
            )
        return cls(token=token, account_id=account_id, user_id=user_id, base_url=base_url)

    # ── Low-level request ───────────────────────────────────────────
    def _post_user_php(self, payload: Dict[str, Any]) -> Any:
        """
        POST to /php/user.php with form-encoded param=<json array>.
        Auto-fills page_id and pageName if not provided.
        Raises ChatRaceDashboardError on non-OK response.
        """
        payload.setdefault("page_id", self.account_id)
        payload.setdefault("pageName", "inbox")
        # Spec: param is a JSON-encoded array containing a single dict
        body = {"param": json.dumps([payload], ensure_ascii=False)}

        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/en/inbox?acc={self.account_id}",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        }
        url = f"{self.base_url}/php/user.php"
        log.debug(f"POST {url}  op={payload.get('op')}/{payload.get('op1')}  id={payload.get('id')}")
        try:
            r = self._session.post(url, data=body, headers=headers, timeout=20)
        except requests.RequestException as e:
            raise ChatRaceDashboardError(f"network error: {e}") from e

        if r.status_code != 200:
            raise ChatRaceDashboardError(f"HTTP {r.status_code}: {r.text[:300]}")

        try:
            data = r.json()
        except ValueError as e:
            raise ChatRaceDashboardError(f"non-JSON response: {r.text[:300]}") from e

        # Response is typically [{"status":"OK","data":[...]}]
        # Unwrap the outer list if it's a single-item wrapper.
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if isinstance(data, dict):
            status = data.get("status")
            if status and status != "OK":
                raise ChatRaceDashboardError(f"API status={status}: {data}")
        return data

    # ── High-level: messages ────────────────────────────────────────
    def get_conversation_raw(
        self,
        contact_id: Union[str, int],
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """
        Fetch raw message records for one conversation, newest first.
        `contact_id` is whatever the dashboard uses to identify a contact —
        for WhatsApp it's the phone number without the leading + (e.g.
        "972505539377").
        """
        resp = self._post_user_php({
            "id":     str(contact_id),
            "op":     "conversations",
            "op1":    "get",
            "offset": offset,
            "limit":  limit,
            "expand": {
                "comments":          {},
                "refs":              {},
                "appointments":      None,
                "orders":            {},
                "scheduledMessages": True,
            },
        })
        return resp.get("data", []) if isinstance(resp, dict) else []

    @staticmethod
    def _decode_message(raw_msg: Dict) -> Dict:
        """
        Decode a single message row into a friendly format.
          dir: "0" = OUTBOUND (from us/bot to customer)
               "1" = INBOUND  (from customer to us)
          channel: "5" = WhatsApp
          message: JSON-encoded list of content blocks (text/template/image/...)
        """
        direction = "out" if str(raw_msg.get("dir", "1")) == "0" else "in"
        out = {
            "id":         raw_msg.get("id"),
            "direction":  direction,
            "channel_id": raw_msg.get("channel"),
            "channel":    {"5": "whatsapp"}.get(str(raw_msg.get("channel", "")), str(raw_msg.get("channel", ""))),
            "sent_by":    raw_msg.get("sentBy"),
        }
        # message is sometimes a JSON string, sometimes already a list
        msg = raw_msg.get("message")
        if isinstance(msg, str):
            try:
                msg = json.loads(msg)
            except Exception:
                pass
        out["content"] = msg
        # Try to pull a plain-text rendering of the content
        out["text"] = ChatRaceDashboardClient._extract_text(msg)
        # Surface a timestamp if present at any common key (it's in milliseconds)
        from datetime import datetime, timezone
        ts_ms = None
        for k in ("timestamp", "ts", "created_at", "date", "time", "created"):
            if k in raw_msg:
                try:
                    ts_ms = int(raw_msg[k])
                    break
                except (ValueError, TypeError):
                    pass
        if ts_ms is not None:
            # If looks like seconds (10 digits), keep as seconds; if 13 digits, it's ms
            ts_s = ts_ms / 1000 if ts_ms > 10_000_000_000 else ts_ms
            out["ts"] = int(ts_s)
            out["iso"] = datetime.fromtimestamp(ts_s, tz=timezone.utc).isoformat()
        # Keep all extra fields for debugging — useful while we learn the schema
        known = {"id", "dir", "channel", "sentBy", "message", "ts", "timestamp",
                 "created_at", "date", "time", "created"}
        extras = {k: v for k, v in raw_msg.items() if k not in known}
        if extras:
            out["_extras"] = extras
        return out

    @staticmethod
    def _extract_text(content: Any) -> str:
        """Best-effort plain-text rendering of a message's content blocks."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "text":
                    # text/body can be a plain string OR a {body: "..."} dict
                    val = block.get("text") or block.get("body") or ""
                    if isinstance(val, dict):
                        val = val.get("body") or val.get("text") or ""
                    if val:
                        parts.append(str(val))
                elif t == "template":
                    tpl = block.get("template") or {}
                    name = tpl.get("name") or "(template)"
                    parts.append(f"[template:{name}]")
                elif t in ("image", "video", "audio", "document", "file", "sticker"):
                    caption = block.get(t, {})
                    cap_txt = ""
                    if isinstance(caption, dict):
                        cap_txt = caption.get("caption") or ""
                    parts.append(f"[{t}{':' + cap_txt if cap_txt else ''}]")
                elif t == "interactive":
                    # WhatsApp interactive buttons/lists — try to surface the title
                    inter = block.get("interactive") or {}
                    body = inter.get("body") or {}
                    body_txt = body.get("text") if isinstance(body, dict) else ""
                    parts.append(f"[interactive]{(' ' + body_txt) if body_txt else ''}")
                elif t == "button":
                    btn = block.get("button") or {}
                    title = btn.get("text") if isinstance(btn, dict) else ""
                    parts.append(f"[button:{title or '?'}]")
                else:
                    # Unknown block — try common text-bearing keys
                    for key in ("text", "body", "caption", "title"):
                        val = block.get(key)
                        if isinstance(val, str) and val:
                            parts.append(val)
                            break
                        if isinstance(val, dict):
                            inner = val.get("body") or val.get("text")
                            if isinstance(inner, str) and inner:
                                parts.append(inner)
                                break
            return " ".join(parts)
        if isinstance(content, dict):
            return content.get("text") or content.get("body") or ""
        return str(content)

    def get_conversation(
        self,
        contact_id: Union[str, int],
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """
        Friendly version: returns a list of decoded messages.
        Each item has: id, direction (in/out), channel, text, content, ts.
        """
        raw = self.get_conversation_raw(contact_id, limit=limit, offset=offset)
        return [self._decode_message(m) for m in raw]

    # ── Sending: WhatsApp templates via the internal dashboard API ──
    # The public API doesn't expose template sends with parameters; this
    # internal op uses /php/user.php with op="conversations", op1="send",
    # op2="waTemplate". Discovered by capturing the dashboard's outgoing
    # request when a human clicks "Send" in the Template dialog (01/06/2026).

    def send_whatsapp_template(
        self,
        ms_id: Union[str, int],
        template_name: str,
        parameters: Optional[List[str]] = None,
        language: str = "he",
        namespace: str = "",
        channel: int = 5,
    ) -> Dict:
        """
        Send a pre-approved WhatsApp template message. Works at ANY time —
        bypasses the 24-hour service window because templates are pre-approved
        by Meta.

        Args:
          ms_id:         Recipient's WhatsApp id (phone digits, e.g. "972528515334").
          template_name: Approved template id (e.g. "new_message").
          parameters:    Ordered list of strings for {{1}}, {{2}}, ... in the body.
          language:      Template language code ("he", "en", etc).
          namespace:     Optional WABA template namespace. Empty string in most cases.
          channel:       5 = WhatsApp (default), other ints map to other channels.

        Returns the parsed response dict from the server.
        """
        params = parameters or []
        pers = [
            {"label": "{{" + str(i + 1) + "}}", "value": str(v)}
            for i, v in enumerate(params)
        ]
        data: Dict[str, Any] = {
            "channel":    channel,
            "id":         template_name,
            "language":   language,
            "components": {
                "body":   {"pers": pers},
                "header": None,
            },
        }
        if namespace:
            data["namespace"] = namespace
        payload = {
            "ms_id":   str(ms_id),
            "op":      "conversations",
            "op1":     "send",
            "op2":     "waTemplate",
            "channel": channel,
            "data":    data,
        }
        return self._post_user_php(payload)

    # ── Conversation state: archive, human/bot mode ─────────────────
    # These ops mirror what the dashboard UI sends when the agent clicks
    # the "Archive" icon or the "אנושי / בוט" toggle in the header.
    # Discovered via DevTools capture on 2026-06-03.

    def update_conversation_field(
        self,
        ms_id: Union[str, int],
        field: str,
        value: Union[int, str],
        channel: Union[int, str] = 5,
    ) -> bool:
        """
        Generic helper for the `conversations/update/<field>` op pattern.
        Used by `archive_conversation`. Posts to /php/user.php; the server
        returns an empty body on success.

        ⚠️ Note the deliberate typo in `curentChannel` (missing first `r`) —
        that is how the dashboard actually spells it, and the API rejects
        the spelling-correct version. Don't fix it.

        Confirmed working for field="archived" on 2026-06-03.
        """
        self._post_user_php({
            "ms_id":         str(ms_id),
            "op":            "conversations",
            "op1":           "update",
            "op2":           field,
            "data":          {"value": value},
            "curentChannel": str(channel),   # intentional typo
            "pageName":      "inbox",
        })
        return True

    def archive_conversation(
        self,
        ms_id: Union[str, int],
        archive: bool = True,
    ) -> bool:
        """
        Move a conversation into the archive (archive=True) or bring it
        back to the inbox (archive=False).
        """
        return self.update_conversation_field(ms_id, "archived", 1 if archive else 0)

    def set_human_mode(
        self,
        phones: Union[str, int, List[Union[str, int]]],
        enable: bool = True,
    ) -> bool:
        """
        Toggle the human / bot mode of one or more conversations. This is
        the same action as clicking the "אנושי / בוט" header toggle in the
        dashboard.

        When enable=True the conversation enters live-chat mode — bot
        flows that check the `live_chat` field will skip their triggers,
        so the customer's next inbound message will NOT be auto-answered
        by the bot.

        Prefer calling this BEFORE sending a free-text reply via the
        public ConnectOp `send_text` API — otherwise the reply goes out
        with sentBy=0 (bot identity) and the bot keeps treating the
        conversation as automated.

        `phones` accepts either a single value or a list — the underlying
        API supports batch updates via the `psid` array.

        Discovered via DevTools on 2026-06-03 by capturing the dashboard's
        'אנושי' button click. Note that this op uses a DIFFERENT pattern
        than archive: op="users", op2="live-chat" (hyphen, not underscore),
        and the field name in the payload is `enable` (bool), not
        `data.value` (int).
        """
        if not isinstance(phones, list):
            phones = [phones]
        psid_list = [str(p) for p in phones]
        self._post_user_php({
            "op":            "users",
            "op1":           "update",
            "op2":           "live-chat",       # hyphen, not underscore!
            "enable":        bool(enable),
            "psid":          psid_list,
            "curentChannel": "5",                # intentional typo, same as archive
            "pageName":      "inbox",
        })
        return True

    def get_full_conversation(
        self,
        contact_id: Union[str, int],
        max_messages: int = 500,
        batch_size: int = 50,
        sleep_between: float = 0.5,
    ) -> List[Dict]:
        """
        Paginate through the full conversation history.
        Stops at max_messages or when a batch comes back empty.
        """
        out: List[Dict] = []
        offset = 0
        while len(out) < max_messages:
            batch = self.get_conversation(contact_id, limit=batch_size, offset=offset)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < batch_size:
                break
            offset += batch_size
            time.sleep(sleep_between)
        return out[:max_messages]


# ── Quick smoke test when run directly ──
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Load .env (same trick the other agents use)
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

    client = ChatRaceDashboardClient.from_env()
    # Default test: Yitzhak Hadad's known contact id from the user's screenshots
    contact_id = sys.argv[1] if len(sys.argv) > 1 else "972505539377"
    print(f"Fetching last 5 messages for contact {contact_id}…")
    msgs = client.get_conversation(contact_id, limit=5)
    print(f"Got {len(msgs)} messages")
    for m in msgs:
        arrow = "←" if m["direction"] == "in" else "→"
        text = (m["text"] or "")[:80]
        print(f"  {arrow}  [{m['channel']}] {text}")
