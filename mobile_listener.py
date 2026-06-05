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

# Skip "stale" inbound that's already older than this when listener catches up
SKIP_OLDER_THAN_SEC = 600   # 10 min — anything older we don't backfill

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


def _process_new_inbound(phone: str, name: str, text: str, ts: int,
                          dc: ChatRaceDashboardClient,
                          msgs_sorted_desc: list = None) -> None:
    """Handle a single newly-detected customer inbound message."""
    # Import lazily so the module loads even without the optional Claude dep
    try:
        from mobile_assistant import draft_response, draft_followup
    except ImportError as e:
        log.warning(f"mobile_assistant not available: {e}")
        return
    from telegram_router import send_draft_to_asi, send_followup_to_asi
    from db import add_pending_reply, update_reply_telegram_id

    # ‫קביעת ‫סיווג: ‫חדשה ‫או ‫המשך
    mode, prev_ctx = ("new", "")
    if msgs_sorted_desc:
        mode, prev_ctx = _classify_conversation(msgs_sorted_desc, phone)

    log.info(f"[mobile] processing {mode} inbound: {phone} ({name}) — {text[:60]!r}")

    # 1) Generate draft via Claude (cheap or full path)
    summary = ""
    draft   = ""
    try:
        if mode == "followup":
            # ‫קצר ‫וזול — ‫השתמש ‫בcontext ‫קיים
            draft = draft_followup(phone, name, text,
                                    previous_context=prev_ctx, dashboard=dc)
            summary = "(המשך שיחה — context מהdraft הקודם)"
        else:
            summary, draft = draft_response(phone, name, text, dashboard=dc)
    except Exception as e:
        log.exception(f"draft generation failed ({mode}): {e}")
        summary = f"⚠️ Claude draft failed: {e}"
        draft   = "(לא הצלחתי לנסח טיוטה אוטומטית — אנא טפל ידנית)"

    # 2) Persist
    reply = add_pending_reply(
        customer_phone=phone,
        customer_name=name,
        customer_message=text,
        context_summary=summary,
        claude_draft=draft,
    )

    # 3) Send to Asi — different format per mode
    try:
        if mode == "followup":
            msg_id = send_followup_to_asi(reply)
        else:
            msg_id = send_draft_to_asi(reply)
        if msg_id:
            update_reply_telegram_id(reply.id, msg_id)
    except Exception as e:
        log.exception(f"Telegram send failed: {e}")


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

        # Skip if a **human agent** already replied AFTER this customer message.
        # Bot auto-replies (sent_by=0 — e.g. "תודה, פנייתך התקבלה",
        # interactive menus, `[template:...]`) are NOT a real reply — they're
        # the welcome flow. We still want to surface the customer's question
        # to Asi in mobile mode.
        last_in_ts = int(last_in.get("ts") or 0)
        last_human_out = next(
            (m for m in msgs_sorted
              if m.get("direction") == "out"
              and m.get("sent_by") not in (None, 0, "0", "")),
            None,
        )
        if last_human_out and int(last_human_out.get("ts") or 0) > last_in_ts:
            # A human already replied. Move cursor and skip.
            max_ts = max(max_ts, la)
            continue

        # ─── Cost guard: ‫dedup ‫על ‫טקסט ‫זהה ‫בלבד ‫─────────────────
        # ‫הבעיה ‫שראינו ‫(05/06/2026): ‫אם ‫אסי ‫לא ‫מטפל ‫בdraft, ‫הוא ‫נשאר ‫`waiting`
        # ‫אבל ‫הdedup ‫הישן ‫בודק ‫רק ‫status=waiting — ‫אם ‫הסטטוס ‫כבר ‫sent/cancelled
        # ‫ואותו ‫טקסט ‫מגיע ‫שוב, ‫זה ‫היה ‫יוצר ‫עוד ‫draft. 8x ‫על ‫אותו ‫טקסט.
        # ‫**הגנה ‫שהוספנו**: ‫אם ‫קיים ‫**אותו ‫טקסט ‫בדיוק** ‫מאותו ‫טלפון ‫תוך 30 ‫דק'
        # ‫(בכל ‫סטטוס) — ‫מדלגים. ‫תשובה ‫**שונה** ‫של ‫הלקוח ‫כן ‫עוברת ‫(זה ‫context ‫חדש).‬
        from db import session_scope as _ss, PendingReply as _PR
        from sqlalchemy import select as _select
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from sqlalchemy import desc as _desc
        cutoff = _dt.now(_tz.utc) - _td(minutes=30)
        with _ss() as _s:
            recent = _s.execute(
                _select(_PR).where(
                    _PR.customer_phone == phone,
                    _PR.customer_message == text,
                    _PR.created_at >= cutoff,
                ).order_by(_desc(_PR.created_at)).limit(1)
            ).scalars().first()
        if recent:
            log.info(f"  cost-guard: skipping {phone} — same text already "
                     f"drafted <30min ago (status={recent.status})")
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
