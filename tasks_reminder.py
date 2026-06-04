"""
tasks_reminder.py — ‫תזכורת יומית למשימות סוכנים פתוחות.‬

‫עובר על כל הסוכנים בבורד "משימות סוכנים" (5092673295), ‏אוסף משימות במצב‬
‫"לא התחיל" + "תקוע", ‏ושולח התראת Telegram לאסי עם סיכום ‎(אם יש מה לשלוח).‬

‫רץ אוטומטית פעם ביום (cron ב-main.py), ‏וגם זמין דרך POST /remind-tasks‬
‫להפעלה ידנית.‬
"""
from __future__ import annotations

import os
import logging
import requests
from typing import Optional

from shared.monday_tasks import (
    MondayTasksClient,
    AGENT_GROUPS,
    STATUS_LABELS,
    PRIORITY_LABELS,
    TYPE_LABELS,
)

log = logging.getLogger("stock_watcher.tasks_reminder")

# ‫תוויות עבריות נחמדות לסוכנים — ‏יוצגו בהתראת Telegram‬
AGENT_LABELS = {
    "gali":    "🦊 גלי",
    "yuval":   "🦉 יובל",
    "dvir":    "🐢 דביר",
    "noa":     "🦄 נועה",
    "invoice": "🦅 איציק",
    "amit":    "🐯 עמית",
    "ron":     "🐺 רון",
    "uri":     "🦁 אורי",
}

# ‫אמוג'י לעדיפות (נשתמש בכותרות של משימות פתוחות)‬
PRIORITY_EMOJIS = {
    "קריטי":   "🔴",
    "גבוהה":   "🟠",
    "בינונית": "🟡",
    "נמוכה":   "⚪",
}


def collect_open_tasks(monday_token: str) -> dict:
    """
    ‫עובר על כל הסוכנים, ‏אוסף משימות פתוחות (לא התחיל / ‏תקוע).‬

    Returns:
        {
            "agents": {
                "uri": [{id, name, priority, type, ...}, ...],
                ...
            },
            "total": int,
        }
    """
    out: dict = {"agents": {}, "total": 0}
    for agent_name in AGENT_GROUPS:
        try:
            client = MondayTasksClient(agent_name, monday_token)
            pending = client.get_pending_tasks()
            # ‫`get_pending_tasks` ‎לפי הקוד מחזיר רק "לא התחיל". ‏גם אוסף "תקוע".‬
            all_tasks = client.get_all_tasks()
            stuck = [t for t in all_tasks if t.get("status") == "תקוע"]
            open_tasks = pending + stuck
            if open_tasks:
                out["agents"][agent_name] = open_tasks
                out["total"] += len(open_tasks)
        except Exception as e:
            log.warning(f"Failed to fetch tasks for {agent_name}: {e}")
    return out


def format_telegram_message(open_data: dict) -> str:
    """
    ‫בונה הודעת Telegram בפורמט HTML.‬
    ‫אם אין משימות — ‏מחזיר ‎'' כדי שלא נשלח כלום.‬
    """
    if open_data["total"] == 0:
        return ""

    lines = [
        f"🔔 <b>תזכורת — ‫משימות סוכנים פתוחות</b>",
        f"<i>סך הכל: ‫{open_data['total']} ‏משימות</i>",
        "",
    ]

    for agent_name, tasks in open_data["agents"].items():
        label = AGENT_LABELS.get(agent_name, agent_name)
        lines.append(f"<b>{label}</b> ({len(tasks)} ‫משימות)")
        for t in tasks[:6]:    # cap per agent — keep message reasonable
            pri = t.get("priority", "—")
            pri_em = PRIORITY_EMOJIS.get(pri, "·")
            status = t.get("status", "?")
            status_em = "🚧" if status == "תקוע" else ""
            name = (t.get("name") or "").strip()
            if len(name) > 60:
                name = name[:60] + "…"
            url = f"https://greenmobile5.monday.com/boards/5092673295/pulses/{t['id']}"
            lines.append(f"  {pri_em}{status_em} <a href=\"{url}\">{name}</a>")
        if len(tasks) > 6:
            lines.append(f"  <i>+ ‫עוד {len(tasks) - 6} ‎לא הוצגו</i>")
        lines.append("")

    lines.append("<i>פתח את Claude Code ואמור לסוכן הרצוי "
                 "\"תבדוק את המשימות שלך\".</i>")
    return "\n".join(lines)


def send_via_telegram(message: str, bot_token: str, chat_id: str) -> bool:
    """‫שולח את ההודעה לטלגרם. ‏אם אין message ריק — ‏לא שולח דבר.‬"""
    if not message:
        log.info("No open tasks — skipping Telegram send")
        return True
    if not bot_token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_TASKS_CHAT_ID missing — skip")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    r = requests.post(url, json={
        "chat_id":    int(chat_id),
        "text":       message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=15)
    if r.status_code != 200 or not r.json().get("ok"):
        log.error(f"Telegram send failed: {r.status_code} {r.text[:200]}")
        return False
    log.info(f"Reminder sent → chat {chat_id}")
    return True


def run_reminder(dry_run: bool = False) -> dict:
    """
    Main entry — run the reminder.

    Returns summary dict suitable for the API response.
    """
    monday_token = os.environ.get("MONDAY_API_TOKEN", "")
    bot_token    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id      = os.environ.get("TELEGRAM_TASKS_CHAT_ID", "")

    if not monday_token:
        return {"error": "MONDAY_API_TOKEN missing", "total": 0}

    log.info("Collecting open tasks across all agents...")
    data = collect_open_tasks(monday_token)
    log.info(f"Found {data['total']} open tasks across {len(data['agents'])} agents")

    msg = format_telegram_message(data)

    if dry_run:
        return {
            "total":   data["total"],
            "agents":  {a: len(t) for a, t in data["agents"].items()},
            "preview": msg[:400] if msg else "",
            "sent":    False,
            "dry_run": True,
        }

    sent = send_via_telegram(msg, bot_token, chat_id)
    return {
        "total":   data["total"],
        "agents":  {a: len(t) for a, t in data["agents"].items()},
        "sent":    sent,
        "skipped": (data["total"] == 0),
    }
