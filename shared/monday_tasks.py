"""
Monday.com Tasks Client — Shared across all Green Mobile agents
================================================================

GraphQL client for the "משימות סוכנים" board.
Each agent reads/updates tasks from its own group.

Board ID: 5092673295
"""
from __future__ import annotations

import os
import json
import logging
import requests

logger = logging.getLogger(__name__)

# ══════════════════════════════════════
# Board & Column Configuration
# ══════════════════════════════════════

TASKS_BOARD_ID = 5092673295
MONDAY_API_URL = "https://api.monday.com/v2"

# ‫המשתמש שצריך לקבל push notification בכל פעם שסוכן יוצר משימה.‬
# ‫הסיבה: ‏ה-API token שייך לאסי, ולכן Monday לא שולח לו push על הפעולות‬
# ‫שלו עצמו. ‏אחרי ‏create_item, ‏אנחנו קוראים ל-create_notification עם‬
# ‫target_id=item_id ל-NOTIFY_USER_ID כדי לכפות push.‬
# ‫אם משאירים None — ‏לא נשלחת התראה (התנהגות מקורית).‬
# ‫מצב נוכחי: ‏Monday push ‎לא מקבל באמת באייפון (גם עם create_notification),‬
# ‫אבל המייל כן מגיע — ‏אז משאירים את זה דלוק כ-fallback.‬
NOTIFY_USER_ID = 41176349   # Asi (greenmobile.eshop@gmail.com)

# ‫Telegram notification — ‏ערוץ אמין יותר מ-Monday push.‬
# ‫אם שני המשתנים מוגדרים ב-env — ‏שולחים גם לטלגרם בכל יצירת משימה.‬
# ‫אם אחד או יותר חסר — ‏מדלגים בשקט (התנהגות אופציונלית).‬
TELEGRAM_BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_TASKS_CHAT_ID = os.environ.get("TELEGRAM_TASKS_CHAT_ID", "")

# Group IDs — one per agent
AGENT_GROUPS = {
    "gali":    "group_mm1412pn",
    "yuval":   "group_mm145yya",
    "dvir":    "group_mm14wt1b",
    "noa":     "group_mm14zpq8",
    "invoice": "group_mm141gkp",
    "amit":    "group_mm1x2brp",
    "ron":     "group_mm3zt7kj",
    "uri":     "group_mm40bj95",   # Added 04/06/2026 — WhatsApp customer service
}

# Column IDs
COL_STATUS     = "color_mm145dt4"
COL_PRIORITY   = "color_mm142kr1"
COL_TYPE       = "color_mm141v23"
COL_NOTES      = "text_mm14kfck"
COL_START_DATE = "date_mm14kqgx"
COL_END_DATE   = "date_mm14pzhs"

# Status label indices (as assigned by Monday.com)
STATUS_NOT_STARTED = 7   # לא התחיל
STATUS_IN_PROGRESS = 0   # בעבודה
STATUS_DONE        = 1   # הושלם
STATUS_STUCK       = 2   # תקוע

# Priority label indices
PRIORITY_LOW      = 15   # נמוכה
PRIORITY_MEDIUM   = 9    # בינונית
PRIORITY_HIGH     = 0    # גבוהה
PRIORITY_CRITICAL = 2    # קריטי

# Type label indices
TYPE_BUG         = 2    # באג
TYPE_FEATURE     = 7    # פיצ'ר
TYPE_RESEARCH    = 4    # מחקר
TYPE_MAINTENANCE = 19   # תחזוקה

# Human-readable label maps (for display)
STATUS_LABELS = {
    STATUS_NOT_STARTED: "לא התחיל",
    STATUS_IN_PROGRESS: "בעבודה",
    STATUS_DONE:        "הושלם",
    STATUS_STUCK:       "תקוע",
}

PRIORITY_LABELS = {
    PRIORITY_LOW:      "נמוכה",
    PRIORITY_MEDIUM:   "בינונית",
    PRIORITY_HIGH:     "גבוהה",
    PRIORITY_CRITICAL: "קריטי",
}

TYPE_LABELS = {
    TYPE_BUG:         "באג",
    TYPE_FEATURE:     "פיצ'ר",
    TYPE_RESEARCH:    "מחקר",
    TYPE_MAINTENANCE: "תחזוקה",
}


class MondayTasksClient:
    """
    monday.com GraphQL client for the shared tasks board.

    Usage:
        client = MondayTasksClient(agent_name="gali", api_token="eyJ...")
        tasks = client.get_pending_tasks()
        client.start_task(task_id)
        client.complete_task(task_id)
    """

    def __init__(self, agent_name: str, api_token: str):
        """
        Args:
            agent_name: One of: gali, yuval, dvir, noa, invoice
            api_token: Monday.com personal API token
        """
        if agent_name not in AGENT_GROUPS:
            raise ValueError(
                f"Unknown agent '{agent_name}'. "
                f"Valid agents: {', '.join(AGENT_GROUPS.keys())}"
            )
        if not api_token:
            raise ValueError("Monday API token is required")

        self.agent_name = agent_name
        self.group_id = AGENT_GROUPS[agent_name]
        self.board_id = TASKS_BOARD_ID
        self.headers = {
            "Authorization": api_token,
            "Content-Type": "application/json",
            "API-Version": "2024-10",
        }

    # ══════════════════════════════════════
    # Core GraphQL
    # ══════════════════════════════════════

    def _query(self, query: str, variables: dict = None) -> dict:
        """Execute a GraphQL query/mutation."""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        response = requests.post(
            MONDAY_API_URL, json=payload,
            headers=self.headers, timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            raise RuntimeError(f"monday.com API error: {data['errors']}")

        return data.get("data", {})

    # ══════════════════════════════════════
    # Read Tasks
    # ══════════════════════════════════════

    def get_all_tasks(self) -> list[dict]:
        """Get all tasks in this agent's group."""
        return self._fetch_tasks()

    def get_pending_tasks(self) -> list[dict]:
        """Get tasks with status 'לא התחיל'."""
        return [t for t in self._fetch_tasks()
                if t["status"] == "לא התחיל"]

    def get_active_tasks(self) -> list[dict]:
        """Get tasks with status 'בעבודה'."""
        return [t for t in self._fetch_tasks()
                if t["status"] == "בעבודה"]

    def get_task(self, item_id: int | str) -> dict:
        """Get a single task by ID."""
        query = """
        query ($itemId: [ID!]!) {
            items(ids: $itemId) {
                id
                name
                column_values {
                    id
                    text
                    value
                }
                updates(limit: 5) {
                    id
                    body
                    created_at
                    creator { name }
                }
            }
        }
        """
        data = self._query(query, {"itemId": [str(item_id)]})
        items = data.get("items", [])
        if not items:
            raise ValueError(f"Task {item_id} not found")
        return self._parse_task(items[0])

    def _fetch_tasks(self) -> list[dict]:
        """Fetch all tasks from this agent's group."""
        query = """
        query ($boardId: [ID!]!, $groupId: [String!]) {
            boards(ids: $boardId) {
                groups(ids: $groupId) {
                    items_page(limit: 100) {
                        items {
                            id
                            name
                            column_values {
                                id
                                text
                                value
                            }
                        }
                    }
                }
            }
        }
        """
        variables = {
            "boardId": [str(self.board_id)],
            "groupId": [self.group_id],
        }
        data = self._query(query, variables)

        tasks = []
        for board in data.get("boards", []):
            for group in board.get("groups", []):
                for item in group.get("items_page", {}).get("items", []):
                    tasks.append(self._parse_task(item))
        return tasks

    # ══════════════════════════════════════
    # Update Tasks
    # ══════════════════════════════════════

    def start_task(self, item_id: int | str) -> dict:
        """Mark task as 'בעבודה'."""
        logger.info(f"[{self.agent_name}] Starting task {item_id}")
        return self._set_status(item_id, STATUS_IN_PROGRESS)

    def complete_task(self, item_id: int | str) -> dict:
        """Mark task as 'הושלם'."""
        logger.info(f"[{self.agent_name}] Completing task {item_id}")
        return self._set_status(item_id, STATUS_DONE)

    def mark_stuck(self, item_id: int | str, reason: str = "") -> dict:
        """Mark task as 'תקוע' and optionally add reason to notes."""
        logger.info(f"[{self.agent_name}] Task {item_id} stuck: {reason}")
        self._set_status(item_id, STATUS_STUCK)
        if reason:
            return self.update_notes(item_id, reason)
        return {}

    def reset_task(self, item_id: int | str) -> dict:
        """Reset task to 'לא התחיל'."""
        return self._set_status(item_id, STATUS_NOT_STARTED)

    def update_notes(self, item_id: int | str, text: str) -> dict:
        """Update the notes column."""
        return self._change_column(item_id, COL_NOTES, json.dumps(text))

    def set_priority(self, item_id: int | str, priority_index: int) -> dict:
        """Set task priority (use PRIORITY_* constants)."""
        label_text = PRIORITY_LABELS.get(priority_index)
        if not label_text:
            raise ValueError(f"Unknown priority index: {priority_index}")
        value = json.dumps({"label": label_text})
        return self._change_column(item_id, COL_PRIORITY, value)

    def set_type(self, item_id: int | str, type_index: int) -> dict:
        """Set task type (use TYPE_* constants)."""
        label_text = TYPE_LABELS.get(type_index)
        if not label_text:
            raise ValueError(f"Unknown type index: {type_index}")
        value = json.dumps({"label": label_text})
        return self._change_column(item_id, COL_TYPE, value)

    def add_comment(self, item_id: int | str, body: str) -> dict:
        """Add an update/comment to a task."""
        query = """
        mutation ($itemId: ID!, $body: String!) {
            create_update(item_id: $itemId, body: $body) {
                id
            }
        }
        """
        return self._query(query, {
            "itemId": str(item_id),
            "body": body,
        })

    # ══════════════════════════════════════
    # Create Tasks
    # ══════════════════════════════════════

    def create_task(
        self,
        name: str,
        priority: int = None,
        task_type: int = None,
        notes: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> dict:
        """
        Create a new task in this agent's group.

        Args:
            name: Task title
            priority: PRIORITY_* constant (optional)
            task_type: TYPE_* constant (optional)
            notes: Free-text notes (optional)
            start_date: "YYYY-MM-DD" (optional)
            end_date: "YYYY-MM-DD" (optional)

        Returns:
            {"id": "...", "name": "..."}
        """
        col_values = {
            COL_STATUS: {"label": STATUS_LABELS[STATUS_NOT_STARTED]},
        }
        if priority is not None:
            col_values[COL_PRIORITY] = {"label": PRIORITY_LABELS[priority]}
        if task_type is not None:
            col_values[COL_TYPE] = {"label": TYPE_LABELS[task_type]}
        if notes:
            col_values[COL_NOTES] = notes
        if start_date:
            col_values[COL_START_DATE] = {"date": start_date}
        if end_date:
            col_values[COL_END_DATE] = {"date": end_date}

        query = """
        mutation ($boardId: ID!, $groupId: String!, $itemName: String!, $colValues: JSON!) {
            create_item(
                board_id: $boardId,
                group_id: $groupId,
                item_name: $itemName,
                column_values: $colValues
            ) {
                id
                name
            }
        }
        """
        variables = {
            "boardId": str(self.board_id),
            "groupId": self.group_id,
            "itemName": name,
            "colValues": json.dumps(col_values),
        }

        data = self._query(query, variables)
        result = data.get("create_item", {})
        logger.info(
            f"[{self.agent_name}] Created task '{name}' (ID: {result.get('id')})"
        )

        # ‫כפיית push: ‏ה-API פועל מטעם אסי, ולכן Monday לא שולח לו push על‬
        # ‫הפעולות שלו עצמו. ‏create_notification עוקף את זה — ‏הוא נחשב‬
        # ‫"פעולה" שמייצרת התראה ספציפית, ‏גם אם היוצר וה-target הם אותו user.‬
        # ‫בפועל: ‏גורם למייל להישלח (push לא מגיע במובייל בלי קשר).‬
        if NOTIFY_USER_ID and result.get("id"):
            try:
                self._send_notification(
                    user_id=NOTIFY_USER_ID,
                    target_id=int(result["id"]),
                    text=f"📌 משימה חדשה ל-{self.agent_name}: {name[:80]}",
                )
            except Exception as e:
                logger.warning(f"create_notification failed (non-fatal): {e}")

        # ‫Telegram — ‏ערוץ ה-push האמין שלנו. ‏שולח גם אם Monday push נכשל.‬
        if TELEGRAM_BOT_TOKEN and TELEGRAM_TASKS_CHAT_ID and result.get("id"):
            try:
                self._send_telegram_task_alert(
                    item_id=result["id"],
                    task_name=name,
                    priority_idx=priority,
                    type_idx=task_type,
                    notes=notes,
                    end_date=end_date,
                )
            except Exception as e:
                logger.warning(f"telegram notification failed (non-fatal): {e}")

        return result

    def _send_telegram_task_alert(
        self,
        item_id: str | int,
        task_name: str,
        priority_idx: int = None,
        type_idx: int = None,
        notes: str = "",
        end_date: str = "",
    ) -> None:
        """
        ‫שולח התראת Telegram לאסי ב-private chat ‎(לא בקבוצת איציק).‬
        ‫זה מנגנון ה-push האמין שלנו — Monday native push לא עובד באייפון.‬

        ‫טוקן וchat_id ‎נטענים מ-env (TELEGRAM_BOT_TOKEN, TELEGRAM_TASKS_CHAT_ID).‬
        ‫אם חסרים — ‏הקריאה ל-create_task פשוט מדלגת על הצעד הזה בשקט.‬
        """
        # Agent display name mapping (Hebrew labels)
        agent_labels = {
            "gali": "גלי", "yuval": "יובל", "dvir": "דביר", "noa": "נועה",
            "invoice": "איציק", "amit": "עמית", "ron": "רון", "uri": "אורי",
        }
        agent_label = agent_labels.get(self.agent_name, self.agent_name)

        # Priority + type labels
        pri_label = PRIORITY_LABELS.get(priority_idx, "—") if priority_idx is not None else "—"
        typ_label = TYPE_LABELS.get(type_idx, "—") if type_idx is not None else "—"

        # Build message (HTML mode — Telegram supports <b>, <code>, <a>)
        url = f"https://greenmobile5.monday.com/boards/{self.board_id}/pulses/{item_id}"
        lines = [
            f"📌 <b>משימה חדשה ל-{agent_label}</b>",
            "",
            f"🏷️ <b>{task_name}</b>",
            f"⭐ עדיפות: {pri_label}",
            f"🗂️ סוג: {typ_label}",
        ]
        if end_date:
            lines.append(f"📅 דדליין: {end_date}")
        if notes:
            # Trim notes to keep msg compact
            preview = notes.strip().replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:200] + "…"
            lines.append("")
            lines.append(f"📝 {preview}")
        lines.append("")
        lines.append(f'🔗 <a href="{url}">פתח במאנדיי</a>')
        body = "\n".join(lines)

        # Send
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(api_url, json={
            "chat_id":    int(TELEGRAM_TASKS_CHAT_ID),
            "text":       body,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        if r.status_code != 200 or not r.json().get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {r.status_code} {r.text[:200]}")
        logger.info(f"[{self.agent_name}] Telegram task alert sent → chat {TELEGRAM_TASKS_CHAT_ID}")

    def _send_notification(self, user_id: int, target_id: int, text: str) -> dict:
        """
        ‫שולח Monday notification לuser ספציפי, ‏מקושר ל-item ספציפי.‬
        ‫זה גורם ל-push notification ב-Monday mobile app אם המשתמש מאופשר.‬

        Args:
            user_id:   Monday user id (e.g. 41176349 for Asi)
            target_id: The item id this notification refers to
            text:      Plain text notification body
        """
        query = """
        mutation ($userId: ID!, $targetId: ID!, $text: String!) {
          create_notification (
            user_id:      $userId,
            target_id:    $targetId,
            text:         $text,
            target_type:  Project
          ) {
            text
          }
        }
        """
        return self._query(query, {
            "userId":   str(user_id),
            "targetId": str(target_id),
            "text":     text,
        })

    # ══════════════════════════════════════
    # Internal Helpers
    # ══════════════════════════════════════

    def _set_status(self, item_id, status_index):
        """Set the status column by label text."""
        label_text = STATUS_LABELS.get(status_index)
        if not label_text:
            raise ValueError(f"Unknown status index: {status_index}")
        value = json.dumps({"label": label_text})
        return self._change_column(item_id, COL_STATUS, value)

    def _change_column(self, item_id, column_id, value):
        """Generic column value change."""
        query = """
        mutation ($boardId: ID!, $itemId: ID!, $columnId: String!, $value: JSON!) {
            change_column_value(
                board_id: $boardId,
                item_id: $itemId,
                column_id: $columnId,
                value: $value
            ) {
                id
            }
        }
        """
        return self._query(query, {
            "boardId": str(self.board_id),
            "itemId": str(item_id),
            "columnId": column_id,
            "value": value,
        })

    def _parse_task(self, raw_item: dict) -> dict:
        """Transform raw GraphQL item into clean task dict."""
        columns = {}
        for col in raw_item.get("column_values", []):
            columns[col["id"]] = {
                "text": col.get("text", ""),
                "value": col.get("value", ""),
            }

        # Parse updates if present
        updates = []
        for upd in raw_item.get("updates", []):
            updates.append({
                "id": upd["id"],
                "body": upd.get("body", ""),
                "created_at": upd.get("created_at", ""),
                "creator": upd.get("creator", {}).get("name", ""),
            })

        return {
            "id": raw_item["id"],
            "name": raw_item["name"],
            "status": columns.get(COL_STATUS, {}).get("text", ""),
            "priority": columns.get(COL_PRIORITY, {}).get("text", ""),
            "type": columns.get(COL_TYPE, {}).get("text", ""),
            "notes": columns.get(COL_NOTES, {}).get("text", ""),
            "start_date": columns.get(COL_START_DATE, {}).get("text", ""),
            "end_date": columns.get(COL_END_DATE, {}).get("text", ""),
            "updates": updates,
        }

    def __repr__(self):
        return (
            f"MondayTasksClient(agent='{self.agent_name}', "
            f"group='{self.group_id}')"
        )


# ══════════════════════════════════════
# CLI Test
# ══════════════════════════════════════

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    # Try loading from root .env
    env_path = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
    load_dotenv(env_path)

    token = os.getenv("MONDAY_API_TOKEN")
    if not token or token.startswith("YOUR_"):
        print("Error: Set MONDAY_API_TOKEN in .env first")
        exit(1)

    # Test with each agent
    for agent_name in AGENT_GROUPS:
        client = MondayTasksClient(agent_name, token)
        tasks = client.get_all_tasks()
        print(f"\n[{agent_name}] {len(tasks)} tasks")
        for t in tasks:
            print(f"  - {t['name']} | {t['status']} | {t['priority']}")
