"""
db.py — SQLAlchemy models + session management for the stock-watcher.

‫עובד גם על SQLite (פיתוח מקומי) וגם על Postgres (Render).‬
‫אם משתנה הסביבה DATABASE_URL מוגדר — משתמש בו (Postgres על Neon).‬
‫אחרת — SQLite ב-/data/stock_watcher.db (Render Disk) או ./stock_watcher.db מקומי.‬
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, BigInteger,
    create_engine, select,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

Base = declarative_base()


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
