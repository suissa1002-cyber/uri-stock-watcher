"""
notifier.py — ‫שליחת הודעת "המוצר חזר למלאי" ב-WhatsApp.‬

‫מכיוון שאיננו יכולים להבטיח שהלקוח שלח לנו הודעה ב-24 ‏שעות האחרונות —‬
‫תמיד שולחים דרך ה-template ‎`new_message` (‎`{{1}}` = ‏שם, ‎`{{2}}` = ‏גוף).‬
‫אם נסיים להוסיף ‎template ייעודי ‎`stock_back_in_stock` ‎בעתיד, נחליף כאן.‬
"""
from __future__ import annotations

import os
import re
import logging
from typing import Optional

from shared.chatrace_dashboard_client import ChatRaceDashboardClient

log = logging.getLogger("stock_watcher.notifier")

# ‫template-עוטף הקיים — מוגבל ל-1024 ‏תווים בגוף, ללא \n / tabs / 4+ ‏רווחים.‬
TEMPLATE_NAME = os.environ.get("STOCK_TEMPLATE_NAME", "new_message")


def _flatten_for_template(text: str) -> str:
    """
    WhatsApp templates reject \n, \t, and 4+ consecutive spaces.
    Replace newlines with a space, normalize spaces, trim.
    Use markdown (* _ ~) and em-dash for visual breaks instead.
    """
    text = text.replace("\n", " ").replace("\t", " ")
    text = re.sub(r" {2,}", " ", text)   # collapse multiple spaces
    return text.strip()


def build_notification_body(customer_first_name: str, product_name: str,
                              branch_name: str, product_url: str = "") -> str:
    """
    ‫ניסוח הודעה בעברית, עם markdown ל-WhatsApp.‬
    ‫מבנה בשורה אחת (template restriction):‬
        חדשות טובות! ה-X חזר למלאי בסניף Y. קישור: ...
    """
    url_part = f" קישור: {product_url}" if product_url else ""
    body = (
        f"חדשות טובות 🎉 הבטחנו שנעדכן ברגע שהמוצר יחזור — *{product_name}* "
        f"חזר למלאי בסניף *{branch_name}*.{url_part} "
        f"ניתן להזמין באתר עם איסוף עצמי, או להגיע ישירות לסניף. "
        f"אם יש שאלה — אנחנו כאן 🙂 — Green Mobile."
    )
    return _flatten_for_template(body)


def notify_back_in_stock(customer_phone: str, customer_name: str,
                          product_name: str, branch_name: str,
                          product_url: str = "",
                          dashboard: Optional[ChatRaceDashboardClient] = None) -> bool:
    """
    ‫שולח את הודעת חזרה למלאי דרך טמפלייט ConnectOp dashboard.‬
    ‫מחזיר True אם הטמפלייט נשלח בהצלחה (ConnectOp returned status=OK).‬
    """
    dc = dashboard or ChatRaceDashboardClient.from_env()
    # First name only — for the {{1}} param.
    first_name = (customer_name or "לקוח/ה").strip().split()[0] if customer_name else "לקוח/ה"
    body = build_notification_body(first_name, product_name, branch_name, product_url)

    log.info(f"Sending stock notification to {customer_phone} for {product_name!r}")
    try:
        resp = dc.send_whatsapp_template(
            ms_id=customer_phone,
            template_name=TEMPLATE_NAME,
            parameters=[first_name, body],
        )
        ok = isinstance(resp, dict) and resp.get("status", "").upper() == "OK"
        if not ok:
            log.warning(f"Template send returned non-OK: {resp}")
        return ok
    except Exception as e:
        log.exception(f"Failed to send template: {e}")
        return False
