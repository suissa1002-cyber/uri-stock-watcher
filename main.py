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
)
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


# ── Endpoints ──
@app.get("/health")
def health():
    """Pingable by UptimeRobot — no auth, just status."""
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


@app.get("/", response_class=HTMLResponse)
def index():
    """Simple landing page."""
    watches = list_all_watches(limit=50)
    rows_html = ""
    for w in watches:
        rows_html += (
            f"<tr><td>{w.id}</td><td>{w.customer_name}</td>"
            f"<td>{w.product_name}</td><td>{w.status}</td>"
            f"<td>{w.added_at.strftime('%d/%m %H:%M') if w.added_at else ''}</td></tr>"
        )
    return f"""
    <html dir="rtl"><head><meta charset="utf-8"><title>Uri Stock Watcher</title>
    <style>body{{font-family:system-ui;max-width:900px;margin:40px auto;padding:0 20px}}
    table{{border-collapse:collapse;width:100%}} th,td{{padding:8px;border-bottom:1px solid #eee;text-align:right}}
    th{{background:#f6f6f6}}</style></head><body>
    <h1>Uri Stock Watcher</h1>
    <p>Cron: <strong>{CRON_HOUR}:00 — ‫ימי {CRON_DOW}</strong> (timezone: {TZ_NAME})</p>
    <p>‫סך הכל ‏{len(watches)} ‏רשומות אחרונות:</p>
    <table><thead><tr><th>id</th><th>‫שם</th><th>‫מוצר</th><th>‫סטטוס</th><th>‫נוסף</th></tr></thead>
    <tbody>{rows_html}</tbody></table>
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

    # ‫אם זו תגובה (Reply) ‫להודעה ספציפית שלנו — ‫שלוף את ה-PendingReply‬
    # ‫המתאים, ‫או ‫את ההודעה הקודמת מהזיכרון, ‫כדי שClaude ידע על מה.‬
    reply_to = msg.get("reply_to_message", {}) or {}
    reply_to_msg_id = reply_to.get("message_id")
    incoming_msg_id = msg.get("message_id")

    result = handle_command(text, int(chat_id),
                              reply_to_telegram_msg_id=reply_to_msg_id,
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
