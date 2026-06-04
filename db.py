"""
db.py — SQLAlchemy models + session management for the stock-watcher.

‫עובד גם על SQLite (פיתוח מקומי) וגם על Postgres (Render).‬
‫אם משתנה הסביבה DATABASE_URL מוגדר — משתמש בו (Postgres על Neon).‬
‫אחרת — SQLite ב-/data/stock_watcher.db (Render Disk) או ./stock_watcher.db מקומי.‬
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, BigInteger,
    create_engine, select,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

Base = declarative_base()


class MobileMode(Base):
    """
    ‫מצב 'אורי בחוץ' — ‏מודל singleton (id תמיד 1).‬

    ‫כשactive=True, ‏ה-poller מאזין להודעות WhatsApp חדשות, ‏טוען קונטקסט,‬
    ‫מנסח טיוטה דרך Claude, ‏ושולח לאסי בטלגרם לאישור.‬
    """
    __tablename__ = "mobile_mode"

    id                    = Column(Integer, primary_key=True, default=1)
    active                = Column(Integer, default=0, nullable=False)  # bool as int for sqlite compat
    activated_at          = Column(DateTime, nullable=True)
    deactivated_at        = Column(DateTime, nullable=True)
    # Cursor — ms_id of the latest processed conversation timestamp.
    # Anything newer than this is "new" and gets handled.
    last_processed_ts     = Column(BigInteger, default=0, nullable=False)


class PendingReply(Base):
    """
    ‫טיוטה ממתינה לאישור.‬

    ‫כל פעם שמתגלה הודעה חדשה ‏(או תגובת המשך), ‏נוצרת רשומה כאן עם‬
    ‫הטיוטה שClaude הציע. ‏אחרי שאסי אומר 'שלח' — ‏הסטטוס משתנה ל-sent.‬
    """
    __tablename__ = "pending_replies"

    id                    = Column(Integer, primary_key=True)
    customer_phone        = Column(String(20), nullable=False, index=True)
    customer_name         = Column(String(120), nullable=False)
    # The actual message text that triggered this (the customer's latest)
    customer_message      = Column(Text, nullable=False)
    # Summary that Claude built for Asi
    context_summary       = Column(Text, default="")
    # The current draft to send (gets replaced when Asi says "שנה: ...")
    claude_draft          = Column(Text, nullable=False)
    # Telegram message_id we sent to Asi (for replies / threading)
    telegram_message_id   = Column(BigInteger, nullable=True)
    # Status: "waiting" | "sent" | "cancelled" | "stale"
    status                = Column(String(20), default="waiting", nullable=False, index=True)
    created_at            = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                                   nullable=False)
    sent_at               = Column(DateTime, nullable=True)
    # Number of revisions before "שלח" — for telemetry / quality tracking
    revision_count        = Column(Integer, default=0, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id":                  self.id,
            "customer_phone":      self.customer_phone,
            "customer_name":       self.customer_name,
            "customer_message":    self.customer_message,
            "context_summary":     self.context_summary,
            "claude_draft":        self.claude_draft,
            "telegram_message_id": self.telegram_message_id,
            "status":              self.status,
            "created_at":          self.created_at.isoformat() if self.created_at else None,
            "sent_at":             self.sent_at.isoformat() if self.sent_at else None,
            "revision_count":      self.revision_count,
        }


class TelegramMessage(Base):
    """
    ‫זיכרון שיחה לחילופי דברים בטלגרם.‬
    ‫אנחנו שומרים את ההודעות של אסי + ‫תשובות הבוט, ‫כדי שClaude יוכל לקבל‬
    ‫הקשר משיחה רב-הודעתית (לדוגמה: ‫"לקוח X" → ‫"מה הכתובת שלו").‬

    ‫כל ‫message_id (אם זמין) ‫נשמר — ‫כשAsi עושה Reply ‫על הודעה ספציפית,‬
    ‫אנחנו יכולים למצוא את התוכן שלה ולתת אותו לClaude כקונטקסט מרכזי.‬
    ‫מנקים אוטומטית הודעות בנות יותר מ-2 ‏שעות.‬
    """
    __tablename__ = "telegram_messages"

    id              = Column(Integer, primary_key=True)
    chat_id         = Column(BigInteger, nullable=False, index=True)
    role            = Column(String(20), nullable=False)  # 'user' | 'assistant'
    text            = Column(Text, nullable=False)
    telegram_msg_id = Column(BigInteger, nullable=True, index=True)
    ts              = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                              nullable=False, index=True)


class ScheduledAction(Base):
    """
    ‫פעולה ‏מתוזמנת — Claude מבקש "‏ארכב את השיחה אם אין תגובה ב-30 ‫דק'".‬
    ‫listener בודק כל 30 שניות אם הdue_at הגיע ומבצע.‬
    """
    __tablename__ = "scheduled_actions"

    id              = Column(Integer, primary_key=True)
    action_type     = Column(String(40), nullable=False)   # 'archive_if_no_reply', 'archive_now'
    target_phone    = Column(String(20), nullable=False, index=True)
    target_name     = Column(String(120), default="")
    due_at          = Column(DateTime, nullable=False, index=True)
    # Status: 'pending' | 'done' | 'cancelled' | 'skipped' (e.g. customer replied)
    status          = Column(String(20), default="pending", nullable=False, index=True)
    # When created (for 'archive if no reply since' — compare against this)
    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    done_at         = Column(DateTime, nullable=True)
    note            = Column(Text, default="")


class WatchItem(Base):
    """
    ‫רשומה ברשימת המעקב.‬

    ‫כל פעם שלקוח שואל על מוצר שאזל — מוסיפים שורה.‬
    ‫בכל boot של scheduler — בודקים את כל הפעילות, ומשגרים WhatsApp במידה ויש מלאי.‬
    """
    __tablename__ = "watch_items"

    id              = Column(Integer, primary_key=True)
    customer_phone  = Column(String(20), nullable=False, index=True)
    customer_name   = Column(String(120), nullable=False)
    # NewOrder product id (integer in their system)
    neworder_id     = Column(BigInteger, nullable=False, index=True)
    # Human-readable product name (for the WhatsApp message)
    product_name    = Column(String(255), nullable=False)
    # Optional URL to send in the notification (clean / shortened URL ideally)
    product_url     = Column(String(500), default="")
    # Free-text notes (color preference, version, etc.)
    notes           = Column(Text, default="")

    added_at        = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                              nullable=False)
    last_checked_at = Column(DateTime, nullable=True)
    # Status: "watching" | "notified" | "cancelled"
    status          = Column(String(20), default="watching", nullable=False, index=True)
    # When notified — store branch where stock was found
    notified_at     = Column(DateTime, nullable=True)
    notified_branch = Column(String(60), default="")

    def to_dict(self) -> dict:
        return {
            "id":              self.id,
            "customer_phone":  self.customer_phone,
            "customer_name":   self.customer_name,
            "neworder_id":     self.neworder_id,
            "product_name":    self.product_name,
            "product_url":     self.product_url,
            "notes":           self.notes,
            "added_at":        self.added_at.isoformat() if self.added_at else None,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "status":          self.status,
            "notified_at":     self.notified_at.isoformat() if self.notified_at else None,
            "notified_branch": self.notified_branch,
        }


# ─────────────────────────────────────────────────────────────────────
# Engine + session
# ─────────────────────────────────────────────────────────────────────

def _resolve_db_url() -> str:
    """
    Order of preference:
      1. DATABASE_URL env var (Postgres/Neon on Render)
      2. SQLITE_PATH env var (custom path)
      3. /data/stock_watcher.db (Render Disk default)
      4. ./stock_watcher.db (local dev)
    """
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        # Render/Heroku give us postgres:// but SQLAlchemy wants postgresql://
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return db_url

    sqlite_path = os.environ.get("SQLITE_PATH")
    if not sqlite_path:
        # Render Disks mount at /data by convention. Fallback to cwd locally.
        if os.path.isdir("/data"):
            sqlite_path = "/data/stock_watcher.db"
        else:
            sqlite_path = os.path.join(os.path.dirname(__file__), "stock_watcher.db")
    return f"sqlite:///{sqlite_path}"


_engine = None
_SessionLocal: Optional[sessionmaker] = None


def init_db():
    """Create tables. Idempotent — safe to call on every boot."""
    global _engine, _SessionLocal
    db_url = _resolve_db_url()
    # SQLite needs special arg for multi-thread; Postgres doesn't
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    _engine = create_engine(db_url, echo=False, future=True, connect_args=connect_args)
    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


@contextmanager
def session_scope() -> Session:
    """Context manager — yields a session, commits on exit, rolls back on error."""
    if _SessionLocal is None:
        init_db()
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ─────────────────────────────────────────────────────────────────────
# Convenience helpers used by the API and the cron
# ─────────────────────────────────────────────────────────────────────

def add_watch(customer_phone: str, customer_name: str,
              neworder_id: int, product_name: str,
              product_url: str = "", notes: str = "") -> WatchItem:
    """Create a watch row. Returns the persisted item (detached, safe to use)."""
    with session_scope() as s:
        # Dedupe: same phone + same neworder_id + status=watching → return existing
        existing = s.execute(
            select(WatchItem).where(
                WatchItem.customer_phone == customer_phone,
                WatchItem.neworder_id    == neworder_id,
                WatchItem.status         == "watching",
            )
        ).scalar_one_or_none()
        if existing:
            return existing

        item = WatchItem(
            customer_phone=customer_phone,
            customer_name=customer_name,
            neworder_id=neworder_id,
            product_name=product_name,
            product_url=product_url,
            notes=notes,
            status="watching",
        )
        s.add(item)
        s.flush()
        s.refresh(item)
        return item


def list_active_watches() -> list[WatchItem]:
    """Return all rows with status='watching', oldest first."""
    with session_scope() as s:
        return list(s.execute(
            select(WatchItem).where(WatchItem.status == "watching")
                              .order_by(WatchItem.added_at.asc())
        ).scalars().all())


def list_all_watches(limit: int = 200) -> list[WatchItem]:
    """For the admin UI / debug — list everything, newest first."""
    with session_scope() as s:
        return list(s.execute(
            select(WatchItem).order_by(WatchItem.added_at.desc()).limit(limit)
        ).scalars().all())


def mark_notified(item_id: int, branch_name: str):
    """Flip an item to status='notified' after we sent the WhatsApp."""
    with session_scope() as s:
        item = s.get(WatchItem, item_id)
        if item:
            item.status          = "notified"
            item.notified_at     = datetime.now(timezone.utc)
            item.notified_branch = branch_name[:60]


def mark_cancelled(item_id: int):
    with session_scope() as s:
        item = s.get(WatchItem, item_id)
        if item:
            item.status = "cancelled"


def update_last_checked(item_id: int):
    with session_scope() as s:
        item = s.get(WatchItem, item_id)
        if item:
            item.last_checked_at = datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# Mobile mode helpers
# ─────────────────────────────────────────────────────────────────────

def get_mobile_mode() -> MobileMode:
    """Return the singleton MobileMode row, creating it if missing."""
    with session_scope() as s:
        m = s.get(MobileMode, 1)
        if m is None:
            m = MobileMode(id=1, active=0)
            s.add(m)
            s.flush()
            s.refresh(m)
        return m


def set_mobile_mode(active: bool) -> MobileMode:
    """Toggle mobile mode. Updates activated_at / deactivated_at timestamps."""
    with session_scope() as s:
        m = s.get(MobileMode, 1)
        if m is None:
            m = MobileMode(id=1)
            s.add(m)
        now = datetime.now(timezone.utc)
        if active and not m.active:
            m.active         = 1
            m.activated_at   = now
            # Reset cursor to "now" so we don't process old messages
            import time
            m.last_processed_ts = int(time.time())
        elif not active and m.active:
            m.active         = 0
            m.deactivated_at = now
        s.flush()
        s.refresh(m)
        return m


def update_cursor(ts: int):
    """Update the last_processed_ts cursor."""
    with session_scope() as s:
        m = s.get(MobileMode, 1)
        if m and ts > m.last_processed_ts:
            m.last_processed_ts = ts


def add_pending_reply(customer_phone: str, customer_name: str,
                       customer_message: str, context_summary: str,
                       claude_draft: str) -> PendingReply:
    """Persist a new draft awaiting Asi's approval."""
    with session_scope() as s:
        r = PendingReply(
            customer_phone=customer_phone,
            customer_name=customer_name,
            customer_message=customer_message,
            context_summary=context_summary,
            claude_draft=claude_draft,
            status="waiting",
        )
        s.add(r)
        s.flush()
        s.refresh(r)
        return r


def list_waiting_replies() -> list[PendingReply]:
    """All pending replies currently waiting on Asi's approval."""
    with session_scope() as s:
        return list(s.execute(
            select(PendingReply).where(PendingReply.status == "waiting")
                                 .order_by(PendingReply.created_at.asc())
        ).scalars().all())


def get_pending_reply(reply_id: int) -> Optional[PendingReply]:
    with session_scope() as s:
        return s.get(PendingReply, reply_id)


def mark_reply_sent(reply_id: int):
    with session_scope() as s:
        r = s.get(PendingReply, reply_id)
        if r:
            r.status  = "sent"
            r.sent_at = datetime.now(timezone.utc)


def mark_reply_cancelled(reply_id: int):
    with session_scope() as s:
        r = s.get(PendingReply, reply_id)
        if r:
            r.status = "cancelled"


def update_reply_draft(reply_id: int, new_draft: str):
    """Replace the draft text (used when Asi says 'שנה: ...')."""
    with session_scope() as s:
        r = s.get(PendingReply, reply_id)
        if r:
            r.claude_draft   = new_draft
            r.revision_count = (r.revision_count or 0) + 1


def update_reply_telegram_id(reply_id: int, telegram_message_id: int):
    with session_scope() as s:
        r = s.get(PendingReply, reply_id)
        if r:
            r.telegram_message_id = telegram_message_id


def get_pending_by_telegram_id(telegram_message_id: int) -> Optional[PendingReply]:
    """Look up a PendingReply by the telegram message id we sent for it."""
    with session_scope() as s:
        return s.execute(
            select(PendingReply).where(PendingReply.telegram_message_id == telegram_message_id)
        ).scalar_one_or_none()


def record_telegram_message(chat_id: int, role: str, text: str,
                              telegram_msg_id: int = None) -> None:
    """‫שומר הודעת טלגרם להקשר. ‫מנקה הודעות ישנות מ-2 ‏שעות.‬"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    with session_scope() as s:
        # ‫נקה ישן‬
        from sqlalchemy import delete
        s.execute(delete(TelegramMessage).where(TelegramMessage.ts < cutoff))
        # ‫הוסף חדש‬
        s.add(TelegramMessage(
            chat_id=chat_id, role=role, text=text[:4000],
            telegram_msg_id=telegram_msg_id,
        ))


def find_telegram_message_by_id(chat_id: int, telegram_msg_id: int):
    """‫מחזיר את הודעת הטלגרם הספציפית (אם נמצאה) לקונטקסט Reply.‬"""
    with session_scope() as s:
        return s.execute(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_msg_id == telegram_msg_id,
            )
        ).scalar_one_or_none()


def get_recent_telegram_messages(chat_id: int, limit: int = 10,
                                    minutes_back: int = 30) -> list[dict]:
    """
    ‫מחזיר את N ‏ההודעות האחרונות בchat בתוך חלון זמן.‬
    ‫מסונן לפי chat_id ‫כדי לא לזלוג בין משתמשים.‬
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes_back)
    with session_scope() as s:
        rows = s.execute(
            select(TelegramMessage)
              .where(TelegramMessage.chat_id == chat_id,
                     TelegramMessage.ts >= cutoff)
              .order_by(TelegramMessage.ts.desc())
              .limit(limit)
        ).scalars().all()
    # ‫הופך לסדר כרונולוגי + ‫מחזיר רשימת dicts פשוטה‬
    out = []
    for r in reversed(rows):
        out.append({"role": r.role, "text": r.text, "ts": r.ts.isoformat()})
    return out


def add_scheduled_action(action_type: str, target_phone: str, target_name: str,
                          due_at: datetime, note: str = "") -> ScheduledAction:
    """Schedule a future action (e.g. archive after 30 min)."""
    with session_scope() as s:
        a = ScheduledAction(
            action_type=action_type,
            target_phone=target_phone.strip(),
            target_name=(target_name or "").strip(),
            due_at=due_at,
            status="pending",
            note=note,
        )
        s.add(a)
        s.flush()
        s.refresh(a)
        return a


def list_due_actions() -> list[ScheduledAction]:
    """All pending actions whose due_at has passed."""
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        return list(s.execute(
            select(ScheduledAction).where(
                ScheduledAction.status == "pending",
                ScheduledAction.due_at <= now,
            ).order_by(ScheduledAction.due_at.asc())
        ).scalars().all())


def mark_action_done(action_id: int, status: str = "done", note: str = ""):
    with session_scope() as s:
        a = s.get(ScheduledAction, action_id)
        if a:
            a.status   = status
            a.done_at  = datetime.now(timezone.utc)
            if note:
                a.note = (a.note + " | " + note) if a.note else note


def cancel_scheduled_for_phone(phone: str, action_type: str = None) -> int:
    """
    ‫מבטל פעולות מתוזמנות עתידיות לטלפון מסוים.‬
    ‫שימושי: ‫אם הלקוח השיב — ‫לא לארכב יותר.‬
    """
    cancelled = 0
    with session_scope() as s:
        from sqlalchemy import update
        q = select(ScheduledAction).where(
            ScheduledAction.status == "pending",
            ScheduledAction.target_phone == phone.strip(),
        )
        if action_type:
            q = q.where(ScheduledAction.action_type == action_type)
        for a in s.execute(q).scalars():
            a.status = "cancelled"
            a.done_at = datetime.now(timezone.utc)
            cancelled += 1
    return cancelled


def get_latest_waiting() -> Optional[PendingReply]:
    """Most recent waiting reply — used when Asi sends a command without thread."""
    with session_scope() as s:
        return s.execute(
            select(PendingReply).where(PendingReply.status == "waiting")
                                 .order_by(PendingReply.created_at.desc())
                                 .limit(1)
        ).scalar_one_or_none()
