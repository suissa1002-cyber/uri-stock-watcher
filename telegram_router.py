"""
telegram_router.py — ‫שליחה ופירוש פקודות מ-Telegram.‬

‫שולח אליך טיוטות → ‏אתה משיב "שלח" / "שנה: X" / "עצור" → ‏מתפעלים את הזרימה.‬

‫**שליחת הודעה לאסי** (outgoing) — ‏עובד עם הbot הקיים (TELEGRAM_BOT_TOKEN).‬
‫**קבלת תגובה מאסי** (incoming) — ‏דרך POST /telegram-webhook ‏(ראה main.py).‬
"""
from __future__ import annotations

import os
import re
import logging
import requests
from typing import Optional

from db import (
    PendingReply, get_pending_reply, mark_reply_sent, mark_reply_cancelled,
    update_reply_draft, get_latest_waiting, set_mobile_mode, get_mobile_mode,
    get_pending_by_telegram_id,
    record_telegram_message, get_recent_telegram_messages,
    find_telegram_message_by_id,
)

log = logging.getLogger("stock_watcher.telegram_router")

# Mobile mode uses a dedicated bot (`@green_uri_whatsapp_bot`) so it's
# completely separated from the invoices bot used by Itzik + tasks_reminder.
# Falls back to TELEGRAM_BOT_TOKEN if URI_MOBILE_BOT_TOKEN isn't set yet.
TELEGRAM_BOT_TOKEN     = os.environ.get("URI_MOBILE_BOT_TOKEN") \
                          or os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_TASKS_CHAT_ID = os.environ.get("TELEGRAM_TASKS_CHAT_ID", "")
# Only Asi's chat_id can send commands — security check.
ALLOWED_CHAT_IDS = {
    int(TELEGRAM_TASKS_CHAT_ID) if TELEGRAM_TASKS_CHAT_ID.isdigit() else None,
} - {None}


def _send(text: str, reply_to: Optional[int] = None,
          parse_mode: str = "HTML") -> Optional[int]:
    """Send a Telegram message. Returns message_id on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_TASKS_CHAT_ID:
        log.warning("Telegram env missing — skip send")
        return None
    payload = {
        "chat_id":    int(TELEGRAM_TASKS_CHAT_ID),
        "text":       text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json=payload, timeout=15,
    )
    if r.status_code != 200 or not r.json().get("ok"):
        log.error(f"Telegram send failed: {r.status_code} {r.text[:200]}")
        return None
    return r.json()["result"]["message_id"]


def send_draft_to_asi(reply: PendingReply) -> Optional[int]:
    """
    ‫שולח לאסי הודעת טיוטה — ‏עם summary + ‏draft + ‏אפשרויות תגובה.‬

    ‫עיצוב: ‫מבלי `<code>` ‫על הטיוטה (כדי שלא ייראה רובוטי) — ‫משתמש‬
    ‫ב-`<blockquote>` ‫שמעצב נכון את ההצעה לקריאה אנושית.‬
    """
    # Escape ONCE per content block — draft separately so we don't mangle it
    msg_esc    = _escape_html(reply.customer_message)
    summary_esc = _escape_html(reply.context_summary)
    draft_esc  = _escape_html(reply.claude_draft)

    body = (
        f"📥  <b>שיחה חדשה</b>\n"
        f"👤  {reply.customer_name}\n"
        f"📞  {reply.customer_phone}\n"
        f"\n"
        f"━━━━━━━━━━━━━━\n"
        f"\n"
        f"💬  <b>הודעת הלקוח</b>\n"
        f"<blockquote>{msg_esc}</blockquote>\n"
        f"\n"
        f"🎯  <b>הקשר</b>\n"
        f"<i>{summary_esc}</i>\n"
        f"\n"
        f"━━━━━━━━━━━━━━\n"
        f"\n"
        f"📝  <b>טיוטה לאישור</b>  ·  <code>#{reply.id}</code>\n"
        f"<blockquote>{draft_esc}</blockquote>\n"
        f"\n"
        f"━━━━━━━━━━━━━━\n"
        f"✏️  <b>שלח</b>  /  <b>עצור</b>  /  <b>שנה:</b> ..."
    )
    return _send(body)


def send_confirmation(reply: PendingReply, status: str) -> None:
    if status == "sent":
        _send(f"✅ נשלח ל-{reply.customer_name} (#{reply.id}).")
    elif status == "cancelled":
        _send(f"🚫 בוטל (#{reply.id}). הלקוח יחכה לטיפול ידני.")


def send_edit_confirmation(reply: PendingReply) -> Optional[int]:
    """After Asi requested an edit and Claude re-drafted."""
    body = (
        f"✏️  <b>טיוטה מעודכנת</b>  ·  <code>#{reply.id}</code>\n"
        f"<blockquote>{_escape_html(reply.claude_draft)}</blockquote>\n"
        f"\n"
        f"━━━━━━━━━━━━━━\n"
        f"✏️  <b>שלח</b>  /  <b>עצור</b>  /  <b>שנה:</b> ..."
    )
    return _send(body)


def send_mobile_status(active: bool) -> None:
    if active:
        _send("📲 <b>מצב נייד פעיל.</b>\n"
              "אאזין להודעות חדשות מ-WhatsApp ואשלח טיוטות לכל אחת.\n\n"
              "כיבוי: <i>אורי, חזרתי</i>")
    else:
        _send("📱 <b>מצב רגיל.</b> ה-poller נעצר.\n"
              "כל ההודעות מהלקוחות יחכו לטיפול ידני ב-Inbox.")


def _escape_html(text: str) -> str:
    """Escape HTML special chars for Telegram parse_mode=HTML."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─────────────────────────────────────────────────────────────────────
# Incoming command parsing
# ─────────────────────────────────────────────────────────────────────

# Match #REPLY-N in text (Asi can reference a specific draft)
_REPLY_REF_RE = re.compile(r"#REPLY[-\s]?(\d+)|#(\d+)", re.IGNORECASE)

# Mode commands
_ACTIVATE_RE = re.compile(
    r"(אורי.*בחוץ|אני בחוץ|מצב נייד|mobile mode on|listen)",
    re.IGNORECASE,
)
_DEACTIVATE_RE = re.compile(
    r"(אורי.*חזרתי|חזרתי|במשרד|מצב רגיל|mobile mode off|stop listening)",
    re.IGNORECASE,
)

# Action commands
_SEND_RE   = re.compile(r"^שלח\b|^send\b", re.IGNORECASE)
_CANCEL_RE = re.compile(r"^עצור\b|^בטל\b|^cancel\b", re.IGNORECASE)
_EDIT_RE   = re.compile(r"^שנה\s*[:\-]?\s*(.+)|^edit\s*[:\-]?\s*(.+)", re.IGNORECASE | re.DOTALL)


def parse_command(text: str) -> dict:
    """
    Parse Asi's message into a structured command.

    Returns one of:
      {"action": "activate"}
      {"action": "deactivate"}
      {"action": "send",   "reply_id": int|None}
      {"action": "cancel", "reply_id": int|None}
      {"action": "edit",   "reply_id": int|None, "instructions": str}
      {"action": "unknown"}
    """
    text = (text or "").strip()
    if not text:
        return {"action": "unknown"}

    # 1) Mode toggles
    if _ACTIVATE_RE.search(text):
        return {"action": "activate"}
    if _DEACTIVATE_RE.search(text):
        return {"action": "deactivate"}

    # 2) Extract optional #REPLY-N reference
    reply_id = None
    m = _REPLY_REF_RE.search(text)
    if m:
        reply_id = int(m.group(1) or m.group(2))

    # 3) Action commands
    if _SEND_RE.search(text):
        return {"action": "send", "reply_id": reply_id}
    if _CANCEL_RE.search(text):
        return {"action": "cancel", "reply_id": reply_id}

    m = _EDIT_RE.search(text)
    if m:
        instructions = (m.group(1) or m.group(2) or "").strip()
        return {"action": "edit", "reply_id": reply_id, "instructions": instructions}

    return {"action": "unknown"}


def handle_command(text: str, chat_id: int,
                    reply_to_telegram_msg_id: Optional[int] = None,
                    incoming_telegram_msg_id: Optional[int] = None) -> dict:
    """
    ‫מבצע את הפקודה שאסי שלח. ‏מחזיר dict עם תוצאה.‬

    ‫אם ‏`reply_to_telegram_msg_id` ‫מסופק — ‫מנסה לאתר את הPendingReply‬
    ‫המתאים. ‫הקשר הזה זמין ל-send/cancel/edit ‫(לזיהוי טיוטה ספציפית)‬
    ‫וגם ל-_handle_query (כדי שClaude ידע על איזה לקוח אסי מדבר).‬
    """
    # Security: only allow whitelisted chat_ids
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        log.warning(f"Unauthorized command from chat_id={chat_id}")
        return {"ok": False, "error": "unauthorized"}

    # ‫שמור את ההודעה לזיכרון השיחה (לפני שמטפלים)‬
    try:
        record_telegram_message(chat_id, "user", text,
                                  telegram_msg_id=incoming_telegram_msg_id)
    except Exception as e:
        log.warning(f"failed to record user msg: {e}")

    # ‫נסה למצוא את הPendingReply שעליו אסי הגיב‬
    reply_context = None
    if reply_to_telegram_msg_id:
        reply_context = get_pending_by_telegram_id(int(reply_to_telegram_msg_id))

    cmd = parse_command(text)
    action = cmd.get("action")

    # ─── Mode toggles ───
    if action == "activate":
        m = set_mobile_mode(True)
        send_mobile_status(True)
        return {"ok": True, "action": "activate", "active": bool(m.active)}

    if action == "deactivate":
        m = set_mobile_mode(False)
        send_mobile_status(False)
        return {"ok": True, "action": "deactivate", "active": bool(m.active)}

    # ─── Action on a pending reply ───
    # ‫רק אם הפעולה היא באמת send/cancel/edit נדאג לטיוטה ממתינה.‬
    # ‫אחרת — ‏תיפול לבסוף ל-_handle_query (ad-hoc Q&A).‬
    if action in ("send", "cancel", "edit"):
        from db import list_waiting_replies
        reply_id = cmd.get("reply_id")

        # ‫סדר עדיפויות לזיהוי הטיוטה:‬
        # ‫1. ‫reply_id מפורש (#REPLY-N) → ‫הכי ספציפי‬
        # ‫2. ‫תגובת Reply ‫בטלגרם → ‫זיהוי לפי message_id‬
        # ‫3. ‫אם יש בדיוק טיוטה אחת ממתינה → ‫היא הברירת מחדל‬
        # ‫4. ‫אם יש כמה ‫ולא הגיב כReply — ‫מבקש להבהיר‬
        reply = None
        if reply_id:
            reply = get_pending_reply(reply_id)
            if not reply or reply.status != "waiting":
                _send(f"❓ לא מצאתי טיוטה <code>#{reply_id}</code> במצב ממתין.")
                return {"ok": False, "error": "reply_id_not_waiting"}
        elif reply_context and reply_context.status == "waiting":
            reply = reply_context
        else:
            waiting = list_waiting_replies()
            if not waiting:
                _send(f"❓ אין טיוטות ממתינות כרגע.")
                return {"ok": False, "error": "no_waiting_replies"}
            elif len(waiting) == 1:
                reply = waiting[0]  # ‫רק טיוטה אחת — ‫אין בלבול‬
            else:
                # ‫כמה ממתינות — ‫אסור לנחש. ‫שלח רשימה ובקש שיגיב כReply.‬
                lines = [
                    f"⚠️ <b>{len(waiting)} ‫טיוטות ממתינות.</b> ‫כדי לבצע פעולה ספציפית,",
                    f"‫השב (Reply) ‫על ההודעה של הטיוטה הרצויה ‫עם הפקודה.",
                    "",
                ]
                for w in waiting:
                    lines.append(f"  • <code>#{w.id}</code>  {w.customer_name}  <code>{w.customer_phone}</code>")
                _send("\n".join(lines))
                return {"ok": False, "error": "ambiguous_reply_target",
                         "waiting_count": len(waiting)}

    if action == "send":
        from shared.connectop_client import ConnectOpClient
        try:
            co = ConnectOpClient.from_env()
            co.send_text_as_human(reply.customer_phone, reply.claude_draft)
            mark_reply_sent(reply.id)
            send_confirmation(reply, "sent")
            return {"ok": True, "action": "send", "reply_id": reply.id}
        except Exception as e:
            log.exception(f"send failed: {e}")
            _send(f"❌ שליחה נכשלה: {e}")
            return {"ok": False, "error": str(e)}

    if action == "cancel":
        mark_reply_cancelled(reply.id)
        send_confirmation(reply, "cancelled")
        return {"ok": True, "action": "cancel", "reply_id": reply.id}

    if action == "edit":
        instructions = cmd.get("instructions", "")
        try:
            from mobile_assistant import draft_response
            # Re-draft with the edit instructions appended as context
            edit_msg = f"{reply.customer_message}\n\n[בקשת אסי לעריכה: {instructions}]"
            from shared.chatrace_dashboard_client import ChatRaceDashboardClient
            dc = ChatRaceDashboardClient.from_env()
            _, new_draft = draft_response(
                reply.customer_phone, reply.customer_name, edit_msg, dashboard=dc,
            )
            update_reply_draft(reply.id, new_draft)
            # Reload to get fresh draft
            reply = get_pending_reply(reply.id)
            send_edit_confirmation(reply)
            return {"ok": True, "action": "edit", "reply_id": reply.id}
        except Exception as e:
            log.exception(f"edit failed: {e}")
            _send(f"❌ עריכה נכשלה: {e}")
            return {"ok": False, "error": str(e)}

    # ─── Free-form query → ‫עובר ל-Claude כשאלה כללית ───
    # ‫כל הודעה שלא תאמה לפקודה (activate/deactivate/send/cancel/edit)‬
    # ‫מטופלת ‏כשאילתה. ‫אם זו תגובה (Reply) ‫להודעת בוט קודמת — ‫נשלוף את התוכן‬
    # ‫של ההודעה ההיא ונכניס אותה כקונטקסט מרכזי.‬
    replied_to_text = None
    if reply_to_telegram_msg_id:
        tm = find_telegram_message_by_id(chat_id, reply_to_telegram_msg_id)
        if tm:
            replied_to_text = tm.text
    return _handle_query(text, context=reply_context, chat_id=chat_id,
                          replied_to_text=replied_to_text)


def _handle_query(question: str, context: Optional[PendingReply] = None,
                   chat_id: Optional[int] = None,
                   replied_to_text: Optional[str] = None) -> dict:
    """
    ‫עונה לשאלה כללית של אסי דרך טלגרם.‬
    ‫אם ‏`context` ‫מסופק (אסי הגיב לטיוטה ספציפית) — ‫מוסיף את פרטי הלקוח‬
    ‫בתחילת השאלה כך שClaude לא יצטרך לשאול "‫על איזה לקוח?".‬
    """
    from mobile_assistant import answer_query
    from shared.chatrace_dashboard_client import ChatRaceDashboardClient

    # ‫הודעה מקדימה — ‫שיהיה משוב חזותי שאנחנו עובדים‬
    _send("🔍 <i>חושב...</i>")

    try:
        dc = ChatRaceDashboardClient.from_env()
    except Exception:
        dc = None

    # ‫בנה השאלה עם הקשר (אם יש)‬
    full_question = question
    if context:
        prefix = (
            f"[‫הקשר: ‏אסי מגיב לטיוטה שבנינו עבור הלקוח "
            f"{context.customer_name} (טלפון {context.customer_phone}). "
            f"‫השאלה / ‫הפקודה שלו מתייחסת ללקוח הזה.]\n\n"
        )
        full_question = prefix + question
    elif replied_to_text:
        # ‫אסי הגיב להודעה רגילה — ‫השאלה הנוכחית מתייחסת אליה.‬
        prefix = (
            f"[‫אסי מגיב להודעה הקודמת שלך:\n\n"
            f"\"{replied_to_text[:1500]}\"\n\n"
            f"‫שאלתו הנוכחית מתייחסת ‫**ישירות** ‫להודעה למעלה. "
            f"‫אל תשאל 'על איזה מוצר/לקוח' — ‫זה מה שמופיע למעלה.]\n\n"
        )
        full_question = prefix + question

    # ‫שלוף 5 ‏הודעות אחרונות בchat לזיכרון שיחה‬
    # ‫(מסיר את ההודעה הנוכחית — ‫היא תהיה ה-input)‬
    history = []
    if chat_id is not None:
        try:
            recent = get_recent_telegram_messages(chat_id, limit=8, minutes_back=30)
            # ‫הכל חוץ מההודעה האחרונה (היא הנוכחית — ‫כבר נשמרה)‬
            history = recent[:-1] if recent else []
        except Exception as e:
            log.warning(f"failed to load chat history: {e}")

    try:
        answer = answer_query(full_question, dashboard=dc, history=history)
        # ‫שולח את התשובה — ‫מוגבל ל-4000 ‏תווים מסוג HTML של Telegram‬
        if len(answer) > 4000:
            answer = answer[:3900] + "\n\n<i>(תשובה ארוכה — קוצרה)</i>"
        msg_id = _send(answer)
        # ‫שמור את התשובה לזיכרון השיחה (כולל message_id כדי שאסי יוכל לעשות Reply)‬
        if chat_id is not None:
            try:
                record_telegram_message(chat_id, "assistant", answer[:4000],
                                          telegram_msg_id=msg_id)
            except Exception as e:
                log.warning(f"failed to record assistant msg: {e}")
        return {"ok": True, "action": "query", "answer_length": len(answer)}
    except Exception as e:
        log.exception(f"query failed: {e}")
        _send(f"❌ שגיאה במתן תשובה: <code>{_escape_html(str(e))}</code>")
        return {"ok": False, "error": str(e)}
