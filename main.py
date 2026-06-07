"""
main.py — ‫השירות עצמו: FastAPI + APScheduler.‬

Endpoints:
    GET  /health                  → UptimeRobot ping endpoint (no auth)
    POST /watch                   → ‫הוסף לקוח לרשימה‬ (Bearer auth)
    GET  /watches                 → ‫הצג את כל הרשומות‬ (Bearer auth)
    DELETE /watches/{id}          → ‫בטל מעקב‬ (Bearer auth)
    POST /run-check               → ‫הרץ בדיקה ידנית עכשיו‬ (Bearer auth)
    GET  /                        → ‫עמוד דף נחיתה פשוט עם status‬

Scheduler:
    ‫רץ בכל יום ב-09:00 שעון ישראל (Sun-Thu — ‫ימי עסקים).‬
    ‫אפשר להחליף עם משתנה ‎CRON_HOUR ו-‎CRON_DAYS אם רוצים.‬
"""
from __future__ import annotations

import os
import sys
import logging
from datetime import datetime
from typing import Optional

import pytz
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Make `shared/*` importable as a top-level package
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from db import (
    init_db, add_watch, list_all_watches, mark_cancelled,
    get_mobile_mode, set_mobile_mode, list_waiting_replies,
    add_scheduled_action, ScheduledAction, session_scope,
)
from sqlalchemy import select
from checker import run_check
from tasks_reminder import run_reminder
from mobile_listener import start_listener, stop_listener
from telegram_router import handle_command

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("stock_watcher.main")

# ── Config from env ──
API_TOKEN  = os.environ.get("STOCK_WATCHER_TOKEN", "")
CRON_HOUR  = int(os.environ.get("CRON_HOUR", "9"))
# ‫ימי עסקים בישראל: ‫ראשון-חמישי (cron 0-4)‬
CRON_DOW   = os.environ.get("CRON_DAYS", "0-4")
TZ_NAME    = os.environ.get("TZ", "Asia/Jerusalem")
PORT       = int(os.environ.get("PORT", "8000"))

# ── FastAPI app ──
app = FastAPI(
    title="Uri Stock Watcher",
    description="‫שירות מעקב מלאי + ‏שליחת WhatsApp לכשהמוצר חוזר.‬",
    version="0.1.0",
)


# ── Auth dependency ──
def require_token(authorization: Optional[str] = Header(None)):
    if not API_TOKEN:
        # Token not configured → allow (dev mode). Warn in logs.
        log.warning("STOCK_WATCHER_TOKEN not set — accepting all requests")
        return
    expected = f"Bearer {API_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid_token")


# ── Request models ──
class WatchAddRequest(BaseModel):
    customer_phone: str = Field(..., description="WhatsApp phone, e.g. '972522514332'")
    customer_name:  str = Field(..., description="Full name in Hebrew")
    neworder_id:    int = Field(..., description="NewOrder product id (not SKU)")
    product_name:   str = Field(..., description="Human-readable product name")
    product_url:    str = Field("",   description="Link to product page (shortened)")
    notes:          str = Field("",   description="Free-text notes (color, version, etc.)")


class ReminderAddRequest(BaseModel):
    """‫תזכורת אישית — שולחת רק טלגרם ל-Agent Tasks chat בזמן ה-due_at שצוין."""
    due_at:         str = Field(..., description="ISO datetime IL time, e.g. '2026-06-10T11:00:00' or '2026-06-10 11:00'")
    context:        str = Field(..., description="‫למה לחזור / על מה התזכורת (טקסט חופשי)")
    customer_name:  str = Field("",   description="‫שם הלקוח (אופציונלי)")
    customer_phone: str = Field("NA", description="‫טלפון הלקוח (אופציונלי — 'NA' אם תזכורת כללית)")


# ── Endpoints ──
@app.get("/health")
def health():
    """Pingable by UptimeRobot — no auth, just status."""
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


@app.get("/", response_class=HTMLResponse)
def index():
    """Simple landing page — stock watches + personal reminders."""
    # ‫מציגים ‫רק ‫רשומות ‫פעילות ‫(watching/notified) — ‫מבוטלות ‫נשמרות ‫ב-DB ‫אך ‫לא ‫מוצגות‬
    watches = [w for w in list_all_watches(limit=50)
               if w.status not in ("cancelled", "cancelled_stale", "stale")]
    rows_html = ""
    for w in watches:
        rows_html += (
            f"<tr><td>{w.id}</td><td>{w.customer_name}</td>"
            f"<td>{w.product_name}</td><td>{w.status}</td>"
            f"<td>{w.added_at.strftime('%d/%m %H:%M') if w.added_at else ''}</td></tr>"
        )

    # ‫תזכורות ‫אישיות ‫קרובות ‫(pending only)‬
    il_tz = pytz.timezone(TZ_NAME)
    rem_rows_html = ""
    reminders_count = 0
    with session_scope() as s:
        items = list(s.execute(
            select(ScheduledAction).where(
                ScheduledAction.action_type == "personal_reminder",
                ScheduledAction.status == "pending",
            ).order_by(ScheduledAction.due_at.asc()).limit(30)
        ).scalars().all())
        reminders_count = len(items)
        for a in items:
            due_il = pytz.UTC.localize(a.due_at).astimezone(il_tz)
            cust_label = a.target_name or "—"
            if a.target_phone and a.target_phone not in ("NA", "-", ""):
                cust_label = f"{cust_label} <small>({a.target_phone})</small>"
            ctx = (a.note or "")[:200].replace("<", "&lt;").replace(">", "&gt;")
            rem_rows_html += (
                f"<tr><td>{a.id}</td>"
                f"<td><strong>{due_il.strftime('%d/%m %H:%M')}</strong></td>"
                f"<td>{cust_label}</td><td>{ctx}</td></tr>"
            )

    return f"""
    <html dir="rtl"><head><meta charset="utf-8"><title>Uri Stock Watcher</title>
    <style>body{{font-family:system-ui;max-width:1000px;margin:40px auto;padding:0 20px}}
    table{{border-collapse:collapse;width:100%;margin-bottom:32px}}
    th,td{{padding:8px;border-bottom:1px solid #eee;text-align:right}}
    th{{background:#f6f6f6}} small{{color:#888}}
    h2{{margin-top:32px}}</style></head><body>
    <h1>Uri Stock Watcher</h1>
    <p>‫בדיקת ‫מלאי ‫אוטומטית: ‫<strong>{CRON_HOUR}:00 — ‫ימי {CRON_DOW}</strong> · ‫תזכורת ‫משימות ‫טלגרם: ‫<strong>{REMIND_HOUR}:{REMIND_MINUTE:02d} — ‫ימי {REMIND_DAYS}</strong> · timezone: {TZ_NAME}</p>

    <h2>📡 ‫מעקב ‫מלאי ‫({len(watches)} ‫רשומות)</h2>
    <table><thead><tr><th>id</th><th>‫שם</th><th>‫מוצר</th><th>‫סטטוס</th><th>‫נוסף</th></tr></thead>
    <tbody>{rows_html}</tbody></table>

    <h2>⏰ ‫תזכורות ‫אישיות ‫קרובות ‫({reminders_count})</h2>
    <table><thead><tr><th>id</th><th>‫מתי</th><th>‫לקוח</th><th>‫על ‫מה</th></tr></thead>
    <tbody>{rem_rows_html or '<tr><td colspan="4" style="text-align:center;color:#888">‫אין ‫תזכורות ‫פעילות</td></tr>'}</tbody></table>
    </body></html>
    """


@app.post("/watch", dependencies=[Depends(require_token)])
def add_watch_endpoint(req: WatchAddRequest):
    item = add_watch(
        customer_phone=req.customer_phone,
        customer_name=req.customer_name,
        neworder_id=req.neworder_id,
        product_name=req.product_name,
        product_url=req.product_url,
        notes=req.notes,
    )
    return item.to_dict()


@app.get("/watches", dependencies=[Depends(require_token)])
def list_watches_endpoint(limit: int = 200):
    items = list_all_watches(limit=limit)
    return {"count": len(items), "items": [i.to_dict() for i in items]}


@app.delete("/watches/{item_id}", dependencies=[Depends(require_token)])
def cancel_watch_endpoint(item_id: int):
    mark_cancelled(item_id)
    return {"ok": True, "id": item_id, "status": "cancelled"}


# ─── Personal reminders (Agent Tasks Telegram) ──────────────────────

def _parse_due_at(s: str) -> datetime:
    """Accept ISO-like '2026-06-10T11:00:00' or '2026-06-10 11:00'. IL time → UTC."""
    s = s.strip().replace("T", " ")
    fmts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]
    dt_local = None
    for f in fmts:
        try:
            dt_local = datetime.strptime(s, f)
            break
        except ValueError:
            continue
    if dt_local is None:
        raise HTTPException(400, f"could not parse due_at: {s!r} (use 'YYYY-MM-DD HH:MM')")
    # Treat as Asia/Jerusalem, convert to UTC
    tz = pytz.timezone(TZ_NAME)
    return tz.localize(dt_local).astimezone(pytz.UTC).replace(tzinfo=None)


@app.post("/reminders", dependencies=[Depends(require_token)])
def add_reminder_endpoint(req: ReminderAddRequest):
    """Schedule a personal reminder — fires a Telegram message at due_at."""
    due_utc = _parse_due_at(req.due_at)
    a = add_scheduled_action(
        action_type="personal_reminder",
        target_phone=req.customer_phone or "NA",
        target_name=req.customer_name or "",
        due_at=due_utc,
        note=req.context,
    )
    return {
        "id": a.id,
        "due_at_il": pytz.UTC.localize(a.due_at).astimezone(
            pytz.timezone(TZ_NAME)).strftime("%Y-%m-%d %H:%M"),
        "customer_name": a.target_name,
        "customer_phone": a.target_phone,
        "context": a.note,
        "status": a.status,
    }


@app.get("/reminders", dependencies=[Depends(require_token)])
def list_reminders_endpoint(include_done: bool = False, limit: int = 50):
    """List personal reminders (pending by default)."""
    il_tz = pytz.timezone(TZ_NAME)
    with session_scope() as s:
        q = select(ScheduledAction).where(
            ScheduledAction.action_type == "personal_reminder",
        )
        if not include_done:
            q = q.where(ScheduledAction.status == "pending")
        q = q.order_by(ScheduledAction.due_at.asc()).limit(limit)
        items = list(s.execute(q).scalars().all())
        out = []
        for a in items:
            due_il = pytz.UTC.localize(a.due_at).astimezone(il_tz)
            out.append({
                "id": a.id,
                "due_at_il":     due_il.strftime("%Y-%m-%d %H:%M"),
                "customer_name": a.target_name,
                "customer_phone": a.target_phone,
                "context":       a.note,
                "status":        a.status,
                "created_at":    a.created_at.isoformat() if a.created_at else None,
            })
        return {"count": len(out), "items": out}


@app.delete("/reminders/{rid}", dependencies=[Depends(require_token)])
def cancel_reminder_endpoint(rid: int):
    with session_scope() as s:
        a = s.get(ScheduledAction, rid)
        if not a or a.action_type != "personal_reminder":
            raise HTTPException(404, "reminder not found")
        a.status = "cancelled"
        return {"ok": True, "id": rid, "status": "cancelled"}


class ScheduleSendRequest(BaseModel):
    """‫תזמון ‫שליחת ‫הודעת ‫WhatsApp ‫לזמן ‫עתידי."""
    due_at:        str = Field(..., description="ISO datetime IL time, e.g. '2026-06-06 20:30'")
    customer_phone:str = Field(..., description="‫טלפון ‫בינלאומי ‫בלי +")
    customer_name: str = Field("",  description="‫שם ‫לתיעוד")
    text:          str = Field(..., description="‫טקסט ‫ההודעה ‫(נשלח ‫כמו-שהוא)")


@app.post("/admin/schedule-send", dependencies=[Depends(require_token)])
def schedule_send_endpoint(req: ScheduleSendRequest):
    """‫מתזמן ‫שליחת ‫הודעת ‫WhatsApp ‫ללקוח ‫בזמן ‫עתידי. ‫ההודעה ‫תישלח
    ‫אוטומטית ‫כשהזמן ‫יגיע ‫(הליסנר ‫בודק ‫כל 30s)."""
    due_utc = _parse_due_at(req.due_at)
    a = add_scheduled_action(
        action_type="send_message",
        target_phone=req.customer_phone,
        target_name=req.customer_name or "",
        due_at=due_utc,
        note=f"text:{req.text}",
    )
    return {
        "id": a.id,
        "due_at_il": pytz.UTC.localize(a.due_at).astimezone(
            pytz.timezone(TZ_NAME)).strftime("%Y-%m-%d %H:%M"),
        "customer_name": a.target_name,
        "customer_phone": a.target_phone,
        "text_preview": req.text[:200],
        "status": a.status,
    }


@app.post("/admin/cancel-old-drafts", dependencies=[Depends(require_token)])
def cancel_old_drafts(hours: int = 2):
    """‫מבטל ‫כל ‫`PendingReply` ‫עם ‫סטטוס ‫`waiting` ‫שנוצר ‫לפני ‫>X ‫שעות.
    ‫עוזר ‫במקרה ‫של ‫הצטברות ‫drafts ‫ישנים ‫שעשו ‫רעש ‫למנגנון ‫הdedup."""
    from db import PendingReply
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    cancelled_ids = []
    with session_scope() as s:
        items = list(s.execute(
            select(PendingReply).where(
                PendingReply.status == "waiting",
                PendingReply.created_at < cutoff,
            )
        ).scalars().all())
        for it in items:
            it.status = "cancelled_stale"
            cancelled_ids.append(it.id)
    return {"cancelled_count": len(cancelled_ids), "ids": cancelled_ids}


@app.post("/run-check", dependencies=[Depends(require_token)])
def run_check_endpoint(dry_run: bool = False):
    summary = run_check(dry_run=dry_run)
    return summary


@app.post("/remind-tasks", dependencies=[Depends(require_token)])
def remind_tasks_endpoint(dry_run: bool = False):
    """
    ‫עובר על כל הסוכנים, ‏אוסף משימות פתוחות (לא התחיל / ‏תקוע),‬
    ‫ושולח התראת Telegram עם סיכום. ‏אם אין משימות — ‏לא שולח דבר.‬
    """
    return run_reminder(dry_run=dry_run)


# ─── Mobile mode endpoints ──────────────────────────────────────────

@app.get("/quality-report", dependencies=[Depends(require_token)])
def quality_report(days: int = 7):
    """‫דוח אירועי איכות מ-N הימים האחרונים — ‫תיקונים, תסכולים, flags ידניים."""
    from db import list_quality_events
    events = list_quality_events(days=days, limit=200)
    by_type = {}
    for e in events:
        t = e.get("event_type", "?")
        by_type[t] = by_type.get(t, 0) + 1
    return {
        "days": days,
        "total": len(events),
        "by_type": by_type,
        "events": events,
    }


@app.get("/debug/telegram-messages", dependencies=[Depends(require_token)])
def debug_telegram_messages(limit: int = 20):
    """‫דיבאג: ‫רואה את הזיכרון של הודעות הטלגרם.‬"""
    from db import session_scope, TelegramMessage
    from sqlalchemy import select
    with session_scope() as s:
        rows = s.execute(
            select(TelegramMessage).order_by(TelegramMessage.id.desc()).limit(limit)
        ).scalars().all()
        return {"count": len(rows), "messages": [
            {"id":r.id,"chat_id":r.chat_id,"role":r.role,
             "telegram_msg_id":r.telegram_msg_id,
             "text_preview":(r.text or "")[:100],
             "ts":r.ts.isoformat() if r.ts else None}
            for r in rows
        ]}


@app.get("/scheduled-actions", dependencies=[Depends(require_token)])
def list_scheduled_actions(status: str = "pending"):
    """‫רשימה של פעולות מתוזמנות (לדיבאג + ‫שקיפות לאסי).‬"""
    from db import session_scope, ScheduledAction
    from sqlalchemy import select
    with session_scope() as s:
        q = select(ScheduledAction).order_by(ScheduledAction.id.desc()).limit(50)
        if status and status != "all":
            q = q.where(ScheduledAction.status == status)
        rows = s.execute(q).scalars().all()
        return {"count": len(rows), "actions": [
            {"id":a.id,"type":a.action_type,"phone":a.target_phone,
             "name":a.target_name,"due_at":a.due_at.isoformat() if a.due_at else None,
             "status":a.status,"note":(a.note or "")[:200]}
            for a in rows
        ]}


@app.get("/mobile-mode/status", dependencies=[Depends(require_token)])
def mobile_status():
    """‫סטטוס נוכחי של המצב הנייד.‬"""
    m = get_mobile_mode()
    waiting = list_waiting_replies()
    return {
        "active":             bool(m.active),
        "activated_at":       m.activated_at.isoformat() if m.activated_at else None,
        "deactivated_at":     m.deactivated_at.isoformat() if m.deactivated_at else None,
        "last_processed_ts":  m.last_processed_ts,
        "waiting_replies":    [{"id": r.id, "customer": r.customer_name,
                                "phone": r.customer_phone,
                                "created_at": r.created_at.isoformat()}
                               for r in waiting],
    }


@app.post("/mobile-mode/activate", dependencies=[Depends(require_token)])
def mobile_activate():
    """‫הפעלת מצב נייד — ‫poller מתחיל לפעול.‬"""
    m = set_mobile_mode(True)
    return {"active": bool(m.active), "activated_at": m.activated_at.isoformat()}


@app.post("/mobile-mode/deactivate", dependencies=[Depends(require_token)])
def mobile_deactivate():
    """‫כיבוי מצב נייד — ‫poller יושן.‬"""
    m = set_mobile_mode(False)
    return {"active": bool(m.active), "deactivated_at": m.deactivated_at.isoformat()}


# ─── Telegram webhook ──────────────────────────────────────────────

class TelegramUpdate(BaseModel):
    """Subset of Telegram Update we care about."""
    update_id: int
    message:   Optional[dict] = None


@app.post("/telegram-webhook")
def telegram_webhook(update: TelegramUpdate):
    """
    Telegram → ‏webhook → ‏פירוש פקודה → ‏ביצוע.

    No Bearer auth — Telegram doesn't sign requests with our token.
    Instead we whitelist by chat_id inside `handle_command`.
    """
    msg = update.message or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not text or not chat_id:
        return {"ok": True, "skipped": "no_text_or_chat"}

    # ‫Telegram מעביר את ‫**הטקסט המלא** ‫של ההודעה שמגיבים אליה ב-webhook.‬
    # ‫אז גם אם אין לנו אותה ב-DB (לדוגמה אחרי restart) — ‫עדיין יש קונטקסט.‬
    reply_to = msg.get("reply_to_message", {}) or {}
    reply_to_msg_id   = reply_to.get("message_id")
    reply_to_text     = reply_to.get("text") or reply_to.get("caption") or ""
    incoming_msg_id   = msg.get("message_id")

    result = handle_command(text, int(chat_id),
                              reply_to_telegram_msg_id=reply_to_msg_id,
                              reply_to_text_inline=reply_to_text,
                              incoming_telegram_msg_id=incoming_msg_id)
    return result


# ── Scheduler ──
_scheduler: Optional[BackgroundScheduler] = None

# Tasks reminder cadence (separate from stock check)
REMIND_HOUR    = int(os.environ.get("REMIND_HOUR", "9"))
REMIND_MINUTE  = int(os.environ.get("REMIND_MINUTE", "30"))
REMIND_DAYS    = os.environ.get("REMIND_DAYS", "0-4")


def start_scheduler():
    global _scheduler
    tz = pytz.timezone(TZ_NAME)
    _scheduler = BackgroundScheduler(timezone=tz)

    # Job 1 — daily stock check (existing)
    _scheduler.add_job(
        func=run_check,
        trigger=CronTrigger(hour=CRON_HOUR, minute=0, day_of_week=CRON_DOW, timezone=tz),
        id="daily_stock_check",
        name="Daily stock check",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Job 2 — daily open-tasks reminder (new)
    _scheduler.add_job(
        func=run_reminder,
        trigger=CronTrigger(hour=REMIND_HOUR, minute=REMIND_MINUTE,
                             day_of_week=REMIND_DAYS, timezone=tz),
        id="daily_tasks_reminder",
        name="Daily open-tasks reminder",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()
    log.info(
        f"Scheduler started — "
        f"stock {CRON_HOUR}:00 ({CRON_DOW}) + "
        f"tasks reminder {REMIND_HOUR}:{REMIND_MINUTE:02d} ({REMIND_DAYS}) ({TZ_NAME})"
    )


@app.on_event("startup")
def on_startup():
    init_db()
    log.info("Database initialized")
    start_scheduler()
    start_listener()   # mobile_listener thread (idle until mobile_mode toggled on)
    log.info("Mobile listener thread started (idle until activated)")


@app.on_event("shutdown")
def on_shutdown():
    if _scheduler:
        _scheduler.shutdown(wait=False)
    stop_listener()


# ── Entrypoint for `python main.py` (Procfile / Docker CMD) ──
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
