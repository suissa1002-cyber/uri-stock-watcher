"""
mobile_listener.py — ‫poller שמאזין להודעות WhatsApp חדשות במצב 'אורי בחוץ'.‬

‫כשmobile_mode.active=True:‬
  ‫1. ‏פעם ב-30 ‏שניות, ‏שולף שיחות עם last_active > cursor.‬
  ‫2. ‏לכל לקוח חדש, ‏מושך את 12 ‏ההודעות האחרונות.‬
  ‫3. ‏אם ההודעה האחרונה היא inbound + ‏טקסט חופשי + ‏שעברו >5s ‏מאז קבלתה‬
     ‫(שהבוט לא יעניין) — ‫מעביר ל-`mobile_assistant.draft_response()` ‎ומפיק טיוטה.‬
  ‫4. ‏שולח ‏עם הטיוטה לאסי בטלגרם, ‏ושומר ב-pending_replies.‬

‫כשmobile_mode.active=False — ‏הthread יושן (loops sleep) ‏בלי לעשות work.‬
"""
from __future__ import annotations

import os
import time
import logging
import threading
from typing import Optional

from shared.chatrace_dashboard_client import ChatRaceDashboardClient
from db import (
    get_mobile_mode, update_cursor, list_waiting_replies,
    list_due_actions, mark_action_done, cancel_scheduled_for_phone,
)

log = logging.getLogger("stock_watcher.mobile_listener")

# How often to poll ConnectOp for new conversations (seconds)
POLL_INTERVAL_SEC = int(os.environ.get("MOBILE_POLL_INTERVAL_SEC", "30"))

# Skip "stale" inbound that's already older than this when listener catches up.
# ‫**מ-06/2026** — ‫הוגדל ‫ל-30 ‫דק' ‫כי ‫במצב ‫Notify-Only ‫עלות ‫ההתראה ‫היא ‫$0.
# ‫אין ‫סיבה ‫לדלג ‫על ‫הודעות ‫שלא ‫עברו ‫הרבה ‫זמן. ‫מקרה ‫שזה ‫תפס: ‫deploy ‫טרי
# ‫והbot ‫חזר ‫אחרי 11 ‫דק' — ‫הודעה ‫מ-14:00 ‫שעובדה ‫ב-14:11 ‫דולגה ‫בגלל ‫10 ‫דק' ‫סף.
SKIP_OLDER_THAN_SEC = 1800   # 30 min

# Wait this long after a customer message before processing (let the bot finish)
DEBOUNCE_SEC = 8


_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _classify_conversation(msgs_sorted_desc: list, phone: str) -> tuple[str, str]:
    """
    ‫קביעת ‫סיווג ‫שיחה: 'new' ‫או 'followup'.
    ‫הקריטריון: ‫אם ‫**אסי/אנושי ‫כבר ‫שלח ‫תשובה** ‫בשיחה ‫בתוך 24h ‫האחרונות → 'followup'.
    ‫אחרת — 'new'.

    ‫מחזיר ‫גם ‫`previous_context` ‫(summary ‫מהdraft ‫האחרון, ‫אם ‫קיים) ‫כדי ‫לחסוך tool ‫calls.
    """
    import time as _time
    from db import session_scope as _ss, PendingReply as _PR
    from sqlalchemy import select as _select, desc as _desc

    now_ts = int(_time.time())
    cutoff_ts = now_ts - 24 * 3600  # 24h ‫אחורה

    # ‫חיפוש ‫הודעה ‫יוצאת ‫אנושית (sent_by != 0) ‫תוך 24h
    has_human_reply = False
    for m in msgs_sorted_desc:
        if m.get("direction") != "out":
            continue
        m_ts = int(m.get("ts") or 0)
        if m_ts < cutoff_ts:
            break  # ‫רשימה ‫ממוינת ‫desc — ‫מכאן ‫הכל ‫ישן ‫יותר
        sb = m.get("sent_by")
        if sb not in (None, 0, "0", ""):
            has_human_reply = True
            break

    if not has_human_reply:
        return ("new", "")

    # ‫זה ‫המשך — ‫נביא ‫context_summary ‫מהdraft ‫האחרון
    prev_ctx = ""
    with _ss() as _s:
        prev = _s.execute(
            _select(_PR).where(
                _PR.customer_phone == phone,
            ).order_by(_desc(_PR.created_at)).limit(1)
        ).scalars().first()
        if prev and prev.context_summary:
            prev_ctx = prev.context_summary
    return ("followup", prev_ctx)


def _extract_image_url(content) -> str:
    """‫מחפש ‫URL ‫של ‫תמונה ‫בblocks ‫של ‫הודעת ‫WhatsApp (אם ‫קיים)."""
    if not isinstance(content, list):
        return ""
    for block in content:
        if not isinstance(block, dict):
            continue
        # ‫פורמט ‫א: ‫{type: 'image', image: {link: ...}}
        if block.get("type") == "image":
            img = block.get("image", {})
            if isinstance(img, dict):
                url = img.get("link") or img.get("url")
                if url:
                    return url
        # ‫פורמט ‫ב: ‫{attachment: {type: 'image', payload: {url: ...}}}
        att = block.get("attachment")
        if isinstance(att, dict) and att.get("type") == "image":
            payload = att.get("payload") or {}
            if isinstance(payload, dict):
                url = payload.get("url") or payload.get("link")
                if url:
                    return url
    return ""


def _conversation_thread(msgs_sorted_desc: list, current_in_ts: int,
                          max_items: int = 12) -> list[dict]:
    """
    ‫בונה ‫"thread ‫view" ‫של ‫השיחה — ‫כל ‫ההודעות ‫הרלוונטיות ‫הקרובות ‫להודעה
    ‫הנוכחית, ‫כדי ‫שאסי ‫יבין ‫את ‫ההקשר ‫המלא ‫בלי ‫להרים ‫טלפון.

    ‫**הלוגיקה**:
    ‫- ‫מציגים ‫כל ‫הודעת ‫לקוח (in) ‫וכל ‫תשובה ‫אנושית ‫שלך (out + sent_by != 0)
    ‫- ‫**מסננים**: ‫תפריטים ‫אוטומטיים ‫של ‫הbot, ‫templates, ‫הודעות ‫ריקות
    ‫- ‫**מזהים ‫תמונות** ‫ושומרים ‫את ‫ה-URL ‫שלהן ‫כ-`image_url`
    ‫- ‫מסדרים ‫מהישן ‫לחדש (סדר ‫טבעי ‫לקריאה)
    ‫- ‫מגביל ‫ל-`max_items` ‫אחרונות

    ‫מחזיר ‫רשימה ‫של ‫dicts: ‫`{role: 'in'|'out', text, ts, is_new, image_url}`.
    """
    if not msgs_sorted_desc:
        return []
    items = []
    for m in msgs_sorted_desc:
        direction = m.get("direction")
        text = (m.get("text") or "").strip()
        ts = int(m.get("ts") or 0)
        content = m.get("content")
        image_url = _extract_image_url(content)
        if direction == "in":
            if image_url:
                items.append({"role": "in", "text": "[תמונה]", "ts": ts,
                               "is_new": ts == current_in_ts,
                               "image_url": image_url})
                if len(items) >= max_items:
                    break
                continue
            if not text or text.startswith("[image"):
                # ‫תמונה ‫בלי ‫URL ‫שזיהינו — ‫מציינים ‫אבל ‫בלי ‫קישור
                if text.startswith("[image"):
                    items.append({"role": "in", "text": "[תמונה]", "ts": ts,
                                   "is_new": ts == current_in_ts})
                    if len(items) >= max_items:
                        break
                continue
            items.append({"role": "in", "text": text, "ts": ts,
                           "is_new": ts == current_in_ts})
        elif direction == "out":
            # ‫בודקים ‫אם ‫זו ‫תשובה ‫אמיתית ‫שלנו, ‫או ‫רק ‫bot ‫אוטומטי.
            # ‫הלוגיקה ‫זהה ‫לזו ‫בסיווג ‫_is_real_reply ‫למניעת ‫כפילות ‫התראה:
            # ‫sent_by != 0 → ‫אנושי ‫מפורש. ‫sent_by=0 + ‫טקסט > 50 ‫תווים ‫שלא ‫מתחיל
            # ‫במחרוזת ‫bot ‫מוכרת → ‫זו ‫תשובה ‫שלנו ‫שיצאה ‫דרך ‫send_text_as_human.‬
            sb = m.get("sent_by")
            is_human_explicit = sb not in (None, 0, "0", "")
            BOT_AUTO_PREFIXES = (
                "[interactive", "[template:", "[image]", "[file]",
                "‫תודה, פנייתך התקבלה", "תודה, פנייתך התקבלה",
                "‫הנה ‫מה ‫שמצאתי", "הנה מה שמצאתי",
                "‫מה ‫השם ‫המלא", "מה השם המלא",
                "‫אנא ‫פרטו ‫לגביי", "אנא פרטו לגביי",
            )
            is_long_freeform = (
                not is_human_explicit
                and len(text) > 50
                and not any(text.startswith(p) for p in BOT_AUTO_PREFIXES)
            )
            if not (is_human_explicit or is_long_freeform):
                continue  # ‫bot ‫auto (תפריט/template) — ‫מדלגים
            if not text or text.startswith("[template:") or text.startswith("[interactive"):
                continue
            items.append({"role": "out", "text": text, "ts": ts, "is_new": False})
        if len(items) >= max_items:
            break
    items.reverse()
    return items


def _process_new_inbound(phone: str, name: str, text: str, ts: int,
                          dc: ChatRaceDashboardClient,
                          msgs_sorted_desc: list = None) -> None:
    """
    Notify-Only mode (06/2026): ‫כשמגיעה ‫הודעה ‫חדשה — ‫**לא ‫מפעילים ‫Claude**.
    ‫שומרים ‫ב-DB ‫עם ‫status="notify_only" ‫ושולחים ‫התראה ‫גולמית ‫בטלגרם.
    ‫אסי ‫מחליט ‫אם ‫זה ‫שווה ‫טיוטה — ‫אם ‫כן, ‫עושה ‫Reply ‫עם ‫"טיוטה" ‫ואז
    ‫Claude ‫רץ ‫ויוצר ‫טיוטה ‫מלאה.

    ‫06/2026 ‫בונוס: ‫אם ‫יש ‫היסטוריית ‫תשובות ‫אנושיות ‫בשיחה ‫(אסי/אורי
    ‫כתבו ‫קודם ‫ללקוח), ‫מציגים ‫עד 2 ‫שורות ‫כדי ‫שאסי ‫יבין ‫מיד ‫על ‫מה
    ‫הלקוח ‫מגיב — ‫בלי ‫שיצטרך ‫ללחוץ ‫על ‫hashtag.
    """
    from telegram_router import send_inbound_notification
    from db import add_pending_reply, update_reply_telegram_id

    # ‫שלוף ‫thread ‫של ‫השיחה ‫(זול ‫מאוד — ‫רק ‫ConnectOp, ‫לא ‫Claude)‬
    thread = _conversation_thread(msgs_sorted_desc or [], current_in_ts=ts, max_items=12)

    log.info(f"[mobile] notify-only inbound: {phone} ({name}) — {text[:60]!r}"
              f"  thread={len(thread)}")

    # 1) Persist as notify_only (no Claude yet)
    reply = add_pending_reply(
        customer_phone=phone,
        customer_name=name,
        customer_message=text,
        context_summary="",      # ‫ייווצר ‫מאוחר ‫יותר ‫אם ‫תתבקש ‫טיוטה
        claude_draft="",         # ‫אותו ‫דבר
    )
    # ‫סמן ‫סטטוס ‫`notify_only` ‫(במקום ‫`waiting` ‫ברירת ‫המחדל)‬
    try:
        from db import set_reply_status
        set_reply_status(reply.id, "notify_only")
    except Exception as e:
        # ‫fallback ‫אם ‫set_reply_status ‫לא ‫קיים ‫עדיין ‫(שדרוג ‫הדרגתי)‬
        log.warning(f"set_reply_status not available: {e}")

    # 2) Send raw notification to Asi (no Claude, no cost)
    try:
        msg_id = send_inbound_notification(reply, thread=thread)
        if msg_id:
            update_reply_telegram_id(reply.id, msg_id)
    except Exception as e:
        log.exception(f"Telegram notify failed: {e}")


def _poll_once(dc: ChatRaceDashboardClient) -> int:
    """One polling iteration. Returns number of conversations processed."""
    m = get_mobile_mode()
    if not m.active:
        return 0

    cursor = m.last_processed_ts or 0
    now    = int(time.time())

    # Fetch recent conversations from ConnectOp
    try:
        resp = dc._post_user_php({
            "op":       "conversations",
            "op1":      "get",
            "offset":   0,
            "limit":    50,
            "pageName": "inbox",
        })
    except Exception as e:
        log.warning(f"poll failed: {e}")
        return 0

    data = resp.get("data", []) if isinstance(resp, dict) else []
    new_count = 0
    max_ts    = cursor

    for conv in data:
        la = int(conv.get("last_active") or 0)
        if la <= cursor:
            continue
        if (now - la) > SKIP_OLDER_THAN_SEC:
            # Stale — was probably handled while listener was off; skip
            max_ts = max(max_ts, la)
            continue
        if (now - la) < DEBOUNCE_SEC:
            # Too fresh — give bot/customer a moment, will catch next round
            continue

        # Found a fresh active conversation. Check the LAST inbound message —
        # only process if it's a real text question (not a button click /
        # menu selection that the bot can handle).
        phone  = str(conv.get("ms_id", "")).strip()
        name   = (conv.get("full_name") or "").strip() or "לקוח/ה"
        if not phone:
            continue

        try:
            msgs = dc.get_conversation(phone, limit=8)
        except Exception as e:
            log.warning(f"get_conversation failed for {phone}: {e}")
            continue

        # Find the most recent INBOUND message
        msgs_sorted = sorted(msgs, key=lambda m: int(m.get("ts") or 0), reverse=True)
        last_in = next((m for m in msgs_sorted if m.get("direction") == "in"), None)
        if not last_in:
            max_ts = max(max_ts, la)
            continue

        text = (last_in.get("text") or "").strip()
        if not text or len(text) < 2:
            # Empty or button-click; let the bot handle
            max_ts = max(max_ts, la)
            continue

        # ‫Skip ‫if ‫someone ‫already ‫replied ‫AFTER ‫this ‫customer ‫message.
        # ‫"Reply" ‫כולל:
        #  ‫(א) ‫הודעה ‫אנושית ‫מפורשת ‫(sent_by != 0) ‫— ‫עובד ‫מהדשבורד
        #  ‫(ב) ‫הודעת ‫טקסט ‫חופשי ‫מהbot ‫(sent_by=0) ‫**עם ‫אורך > 50 ‫תווים**‬
        #      ‫שאינה ‫template/interactive. ‫זה ‫תופס ‫תשובות ‫שcalled ‫"send_text_as_human"
        #      ‫שולחות ‫בפועל ‫(אורי-תזמון, ‫אורי-Claude ‫מהסנטופ) ‫שיוצאות ‫עם ‫sent_by=0.
        # ‫תפריטי ‫bot ‫(`[interactive]...`, ‫`[template:...]`, ‫"תודה ‫פנייתך ‫התקבלה")
        # ‫עוברים ‫סף ‫50 ‫תווים? ‫"תודה, ‫פנייתך ‫התקבלה ‫בהצלחה ‫אחד ‫מנציגנו ‫יחזור ‫אליך
        # ‫בהקדם ‫האפשרי." ‫זה ‫~80 ‫תווים — ‫אז ‫ננטרל ‫אותם ‫במפורש.‬
        BOT_AUTO_PREFIXES = (
            "[interactive", "[template:", "[image]", "[file]",
            "‫תודה, פנייתך התקבלה", "תודה, פנייתך התקבלה",
            "‫הנה ‫מה ‫שמצאתי", "הנה מה שמצאתי",
            "‫מה ‫השם ‫המלא", "מה השם המלא",
            "‫אנא ‫פרטו ‫לגביי", "אנא פרטו לגביי",
        )
        last_in_ts = int(last_in.get("ts") or 0)

        def _is_real_reply(m):
            if m.get("direction") != "out":
                return False
            sb = m.get("sent_by")
            tx = (m.get("text") or "").strip()
            if sb not in (None, 0, "0", ""):
                return True  # ‫אנושי ‫מפורש
            # ‫bot (sent_by=0): ‫רק ‫אם ‫טקסט ‫ארוך ‫ולא ‫מתחיל ‫במחרוזת ‫מוכרת ‫של ‫bot
            if len(tx) <= 50:
                return False
            if any(tx.startswith(p) for p in BOT_AUTO_PREFIXES):
                return False
            return True

        last_reply = next((m for m in msgs_sorted if _is_real_reply(m)), None)
        if last_reply and int(last_reply.get("ts") or 0) > last_in_ts:
            max_ts = max(max_ts, la)
            continue

        # ─── Cost guard: ‫dedup ‫על ‫טקסט ‫זהה ‫─────────────────
        # ‫(05/06/2026 ‫→ 06/06/2026)‬: ‫הגדלנו ‫מ-30 ‫דק' ‫ל-24 ‫שעות ‫אחרי ‫שהבחנו
        # ‫שתשובה ‫מתוזמנת ‫שיצאה ‫שעות ‫אחרי ‫הinbound ‫המקורי ‫גרמה ‫להתראה ‫כפולה:
        # ‫`send_text_as_human` ‫שולח ‫עם ‫`sent_by=0` (נחשב ‫bot ‫אצל ‫הdashboard),
        # ‫אז ‫הליסנר ‫לא ‫מזהה ‫שטיפלנו, ‫ובדוק ‫הdedup ‫הישן ‫כבר ‫מחוץ ‫לחלון ‫30 ‫דק'.
        # ‫24 ‫שעות ‫מספיק ‫לכסות ‫תזמונים ‫מתוזמנים ‫ועדיין ‫מאפשר ‫למישהו ‫לחזור ‫אחרי
        # ‫כמה ‫שעות ‫עם ‫שאלה ‫**שונה**.‬
        from db import session_scope as _ss, PendingReply as _PR
        from sqlalchemy import select as _select
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from sqlalchemy import desc as _desc
        cutoff = _dt.now(_tz.utc) - _td(hours=24)
        with _ss() as _s:
            recent = _s.execute(
                _select(_PR).where(
                    _PR.customer_phone == phone,
                    _PR.customer_message == text,
                    _PR.created_at >= cutoff,
                ).order_by(_desc(_PR.created_at)).limit(1)
            ).scalars().first()
        if recent:
            log.info(f"  cost-guard: skipping {phone} — same inbound text already "
                     f"drafted <24h ago (status={recent.status})")
            max_ts = max(max_ts, la)
            continue

        # ‫הלקוח השיב — ‫בטל כל מתזמן ‫מותנה ‫שהיה פתוח עליו‬
        # ‫(archive_if_no_reply + send_message_if_no_reply)‬
        cancelled = 0
        for cond_type in ("archive_if_no_reply", "send_message_if_no_reply"):
            cancelled += cancel_scheduled_for_phone(phone, cond_type)
        if cancelled:
            log.info(f"  cancelled {cancelled} pending conditional action(s) for {phone} (customer replied)")

        # ─── It's a real new inbound that needs attention ───
        # ‫מעבירים ‫את ‫רשימת ‫ההודעות ‫ממוינת ‫desc ‫כדי ‫שהsiווג ‫ידע ‫אם ‫זו ‫שיחה ‫חדשה ‫או ‫המשך‬
        _process_new_inbound(phone, name, text, last_in_ts, dc,
                              msgs_sorted_desc=msgs_sorted)
        new_count += 1
        max_ts = max(max_ts, la)

    if max_ts > cursor:
        update_cursor(max_ts)

    return new_count


def _execute_due_actions(dc: ChatRaceDashboardClient) -> int:
    """‫עובר על פעולות מתוזמנות שהדדליין שלהן עבר, ‫מבצע אותן.‬"""
    due = list_due_actions()
    done = 0
    for a in due:
        try:
            if a.action_type == "send_message_if_no_reply":
                # ‫זהה ל-send_message — ‫הביטול אוטומטי קורה ב-_poll_once‬
                # ‫כשהלקוח עונה. ‫אם הגענו לכאן, ‫הלקוח לא ענה → ‫שלח.‬
                text = (a.note or "").split("text:", 1)[-1] if a.note else ""
                from shared.connectop_client import ConnectOpClient
                co = ConnectOpClient.from_env()
                co.send_text_as_human(a.target_phone, text)
                mark_action_done(a.id, "done", note="sent (no customer reply)")
                try:
                    from telegram_router import _send
                    _send(f"📨 <b>הודעה ‫מותנית נשלחה</b> ‫(הלקוח לא ‫ענה)\n"
                          f"👤 {a.target_name}\n"
                          f"📞 <code>{a.target_phone}</code>\n"
                          f"<blockquote>{text[:300]}</blockquote>")
                except Exception: pass
                done += 1
                continue
            if a.action_type == "send_message":
                # ‫`note` ‫מתחיל ב-"text:..." ‫(שמרתי שם את הטקסט)‬
                text = (a.note or "").split("text:", 1)[-1] if a.note else ""
                from shared.connectop_client import ConnectOpClient
                co = ConnectOpClient.from_env()
                co.send_text_as_human(a.target_phone, text)
                mark_action_done(a.id, "done", note="sent")
                try:
                    from telegram_router import _send
                    _send(f"📨 <b>הודעה נשלחה ‫אוטומטית</b>\n"
                          f"👤 {a.target_name}\n"
                          f"📞 <code>{a.target_phone}</code>\n"
                          f"<blockquote>{text[:300]}</blockquote>")
                except Exception: pass
                done += 1
                continue
            if a.action_type == "personal_reminder":
                # ‫תזכורת ‫אישית — ‫רק ‫שולח ‫טלגרם ‫עם ‫הקונטקסט. ‫לא ‫עושה ‫פעולה ‫בשיחה.‬
                # ‫`note` ‫מכיל ‫את ‫הטקסט ‫של ‫התזכורת ‫(למה ‫חוזרים, ‫על ‫מה).‬
                context_text = (a.note or "").strip() or "(ללא הקשר)"
                due_str = a.due_at.strftime("%d/%m %H:%M")
                # ‫אם ‫יש ‫טלפון ‫"NA" — ‫זו ‫תזכורת ‫כללית ‫בלי ‫לקוח ‫ספציפי.‬
                phone_line = ""
                name_line = ""
                if a.target_phone and a.target_phone not in ("NA", "-", ""):
                    phone_line = f"📞 <code>{a.target_phone}</code>\n"
                if a.target_name:
                    name_line = f"👤 <b>{a.target_name}</b>\n"
                try:
                    from telegram_router import _send
                    _send(
                        f"⏰ <b>תזכורת אישית</b>  ({due_str})\n"
                        f"{name_line}{phone_line}"
                        f"<blockquote>{context_text[:1000]}</blockquote>"
                    )
                    mark_action_done(a.id, "done", note="reminder sent")
                except Exception as e:
                    log.exception(f"reminder send failed: {e}")
                    mark_action_done(a.id, "skipped", note=f"telegram failed: {str(e)[:200]}")
                done += 1
                continue
            if a.action_type == "archive_if_no_reply":
                ok = dc.archive_conversation(a.target_phone, archive=True)
                mark_action_done(a.id, "done" if ok else "skipped",
                                  note="archived" if ok else "archive failed")
                # ‫הודיע לאסי שזה קרה‬
                try:
                    from telegram_router import _send
                    _send(f"📦 <b>שיחה ארכובה אוטומטית</b>\n"
                          f"👤 {a.target_name or '(ללא שם)'}\n"
                          f"📞 <code>{a.target_phone}</code>\n"
                          f"<i>מתזמן הסתיים — ‫הלקוח לא ענה.</i>")
                except Exception as e:
                    log.warning(f"failed to notify Asi: {e}")
                done += 1
            elif a.action_type == "archive_now":
                ok = dc.archive_conversation(a.target_phone, archive=True)
                mark_action_done(a.id, "done" if ok else "skipped")
                done += 1
            else:
                mark_action_done(a.id, "skipped", note=f"unknown type {a.action_type}")
        except Exception as e:
            log.exception(f"action {a.id} failed: {e}")
            mark_action_done(a.id, "skipped", note=str(e)[:200])
    return done


def _listener_loop():
    """Background thread main loop."""
    log.info("Mobile listener thread started")
    dc = None
    while not _stop_event.is_set():
        try:
            m = get_mobile_mode()
            if m.active:
                if dc is None:
                    dc = ChatRaceDashboardClient.from_env()
                count = _poll_once(dc)
                if count:
                    log.info(f"poll: handled {count} new inbound")
                # ‫בכל סבב גם מטפלים בפעולות מתוזמנות (גם אם mobile_mode פעיל)‬
                done = _execute_due_actions(dc)
                if done:
                    log.info(f"executed {done} scheduled action(s)")
            else:
                # ‫גם כשmobile mode כבוי — ‫פעולות מתוזמנות עוד צריכות לרוץ‬
                # ‫(לדוגמה: ‫אסי הפעיל schedule_archive ‫ואז עזב את מצב נייד)‬
                if dc is None:
                    dc = ChatRaceDashboardClient.from_env()
                _execute_due_actions(dc)
        except Exception as e:
            log.exception(f"listener loop iteration error: {e}")
            dc = None  # reset on error
        # Sleep with cancellation responsiveness
        _stop_event.wait(POLL_INTERVAL_SEC)
    log.info("Mobile listener thread exiting")


def start_listener():
    """Start the background listener thread. Idempotent."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_listener_loop, daemon=True, name="mobile-listener")
    _thread.start()


def stop_listener():
    """Signal the listener to stop."""
    _stop_event.set()
