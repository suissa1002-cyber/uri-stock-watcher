"""
ConnectOp / ChatRace WhatsApp API Client
=========================================
שילוב עם ConnectOp (WhatsApp BSP) לשליחת הודעות לקוחות / התראות.

API base: https://api.chatrace.com
Auth: X-ACCESS-TOKEN header

Usage:
    from connectop_client import ConnectOpClient
    c = ConnectOpClient.from_env()  # קורא CONNECTOP_API_TOKEN מ-.env
    c.send_text(contact_id, "טקסט")
    contact_id = c.find_or_create_contact(phone="+972501234567", name="ישראל ישראלי")
"""
from __future__ import annotations
import os
import logging
from typing import Optional, Union
import requests

log = logging.getLogger("connectop")


class ConnectOpError(Exception):
    """Raised on non-2xx or error-body responses from ConnectOp API."""
    pass


class ConnectOpClient:
    def __init__(self, token: str, base_url: str = "https://api.chatrace.com", timeout: int = 15):
        if not token:
            raise ValueError("ConnectOp token is required")
        self.token = token
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.h = {
            "X-ACCESS-TOKEN": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @classmethod
    def from_env(cls) -> "ConnectOpClient":
        """Build from env vars CONNECTOP_API_TOKEN + CONNECTOP_BASE_URL."""
        tok = os.environ.get("CONNECTOP_API_TOKEN", "")
        base = os.environ.get("CONNECTOP_BASE_URL", "https://api.chatrace.com")
        return cls(token=tok, base_url=base)

    # ── Low-level HTTP ────────────────────────────────────────────────
    def _req(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base}{path}"
        kwargs.setdefault("timeout", self.timeout)
        r = requests.request(method, url, headers=self.h, **kwargs)
        # ChatRace returns 200 even on error, with body {"error": {...}}
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
            return {"raw": r.text}
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            raise ConnectOpError(f"API error {err.get('code')}: {err.get('message')} (path={path})")
        if r.status_code >= 400:
            r.raise_for_status()
        return data

    # ── Account metadata ──────────────────────────────────────────────
    def get_tags(self) -> list[dict]:
        """Return all account tags: [{id, name}]"""
        return self._req("GET", "/accounts/tags")

    def get_custom_fields(self) -> list[dict]:
        """Return all custom fields available for contacts."""
        return self._req("GET", "/accounts/custom_fields")

    def get_flows(self) -> list[dict]:
        """Return all flows that can be triggered for a contact."""
        return self._req("GET", "/accounts/flows")

    # ── Contacts ──────────────────────────────────────────────────────
    def create_contact(self, phone: str, first_name: str = "", last_name: str = "",
                        gender: Optional[str] = None, locale: Optional[str] = None) -> dict:
        """
        Create a new contact. `phone` should be E.164 (e.g. "972501234567" without +).
        Returns the created contact dict.
        """
        body: dict = {"phone": phone}
        if first_name: body["first_name"] = first_name
        if last_name:  body["last_name"]  = last_name
        if gender:     body["gender"]     = gender
        if locale:     body["locale"]     = locale
        return self._req("POST", "/contacts", json=body)

    def get_contact(self, contact_id: Union[str, int]) -> dict:
        return self._req("GET", f"/contacts/{contact_id}")

    def find_by_custom_field(self, custom_field_id: Union[str, int], value: str) -> list[dict]:
        """
        Find contacts where a custom field equals a given value.
        Example: find by Phone Number (cf id -8): find_by_custom_field(-8, "972501234567")
        """
        return self._req("GET", "/contacts/find_by_custom_field",
                         params={"custom_field_id": custom_field_id, "value": value})

    def find_or_create_by_phone(self, phone: str, first_name: str = "") -> Optional[Union[str, int]]:
        """
        Find a contact by phone or create one. Returns contact_id.
        Phone format: international without "+" (e.g. "972501234567").
        """
        phone_clean = phone.lstrip("+").replace("-", "").replace(" ", "")
        try:
            results = self.find_by_custom_field(-8, phone_clean)  # -8 = Phone Number CF
            if results:
                return results[0].get("id") or results[0].get("contact_id")
        except ConnectOpError as e:
            log.warning(f"find_by_custom_field failed (will try create): {e}")
        # not found → create
        created = self.create_contact(phone=phone_clean, first_name=first_name)
        return created.get("id") or created.get("contact_id")

    # ── Tags ─────────────────────────────────────────────────────────
    def add_tag(self, contact_id: Union[str, int], tag_id: Union[str, int]) -> dict:
        return self._req("POST", f"/contacts/{contact_id}/tags/{tag_id}")

    def remove_tag(self, contact_id: Union[str, int], tag_id: Union[str, int]) -> dict:
        return self._req("DELETE", f"/contacts/{contact_id}/tags/{tag_id}")

    def get_contact_tags(self, contact_id: Union[str, int]) -> list[dict]:
        return self._req("GET", f"/contacts/{contact_id}/tags")

    # ── Custom fields ─────────────────────────────────────────────────
    def set_field(self, contact_id: Union[str, int], field_id: Union[str, int], value: str) -> dict:
        return self._req("POST", f"/contacts/{contact_id}/custom_fields/{field_id}",
                         json={"value": value})

    # ── Send messages ─────────────────────────────────────────────────
    def send_text(self, contact_id: Union[str, int], text: str) -> dict:
        """
        Send a free-form text WhatsApp message to a contact.

        IMPORTANT — bot interference: the public API marks outbound
        messages with sentBy=0 (bot identity). This does NOT flip the
        conversation's `live_chat` flag, so any bot flows you have set
        up will keep auto-answering customer replies. If you are
        replying AS A HUMAN agent, use `send_text_as_human()` instead
        — it pauses the bot before sending.

        NOTE: WhatsApp requires the contact to have messaged you in the
        last 24h to receive free-form text. Outside the window, use
        send_flow with a template.

        The `contact_id` can be either ConnectOp's numeric contact id or
        the bare phone number (international, no `+`, e.g. "972501234567")
        — the API accepts both as URL segments.
        """
        return self._req("POST", f"/contacts/{contact_id}/send/text",
                         json={"text": text})

    def send_text_as_human(
        self,
        phone: Union[str, int],
        text: str,
        dashboard_client=None,
        toggle_human_mode: bool = False,
    ) -> dict:
        """
        Send a free-form text reply.

        ⚠️ HISTORICAL NOTE (03/06/2026): this function originally also
        called `set_human_mode(phone, True)` on the dashboard to prevent
        the ConnectOp bot from auto-replying after the customer responds.
        That `set_human_mode` call broadcasts a WebSocket update that
        crashes the dashboard's `wn.hasUser` JS handler (`Uncaught
        TypeError: Cannot read properties of null (reading 'length')`),
        which freezes the entire inbox view. The dashboard then needs a
        `saveFilter` reset to recover — extremely disruptive in practice.

        We disabled the auto-toggle by default. If you still want the
        old behavior (and accept the dashboard-breaking risk), pass
        `toggle_human_mode=True`.

        Recommended workflow instead:
          • Manually click "אנושי" in the dashboard BEFORE Claude sends
            a reply, OR
          • Accept that the bot may interject on the customer's next
            message and handle each case ad-hoc.

        `phone` should be international-formatted without `+`
        (e.g. "972501234567"). Used both as the dashboard `psid` and
        as the public API contact id.
        """
        if toggle_human_mode:
            if dashboard_client is None:
                from chatrace_dashboard_client import ChatRaceDashboardClient
                dashboard_client = ChatRaceDashboardClient.from_env()
            dashboard_client.set_human_mode(phone, enable=True)
        return self.send_text(phone, text)

    def send_file(self, contact_id: Union[str, int], url: str, caption: str = "",
                   file_type: str = "image") -> dict:
        """
        Send a media file by URL.
        file_type: image | video | audio | document
        """
        body = {"url": url, "type": file_type}
        if caption:
            body["caption"] = caption
        return self._req("POST", f"/contacts/{contact_id}/send/file", json=body)

    def send_flow(self, contact_id: Union[str, int], flow_id: Union[str, int]) -> dict:
        """
        Trigger a predefined flow (template/sequence) for a contact.
        Use this for messages outside the 24h window — flows can include
        WhatsApp templates that are pre-approved by Meta.
        """
        return self._req("POST", f"/contacts/{contact_id}/send/{flow_id}")
