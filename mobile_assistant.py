"""
mobile_assistant.py — ‫שכבת Claude API שמנסחת טיוטות בתגובה לפנייה חדשה.‬

‫מקבל: ‏phone + ‏name + ‏customer message + ‏היסטוריה.‬
‫מחזיר: ‏(context_summary, ‏draft_for_asi) — ‏ראשון לסיכום קצר לאסי, ‏שני לטקסט לשליחה.‬

‫דורש ANTHROPIC_API_KEY בenv. ‏אם חסר — ‏מחזיר fallback פשוט.‬
"""
from __future__ import annotations

import os
import logging
import json
from typing import Optional, Tuple

log = logging.getLogger("stock_watcher.mobile_assistant")

# Lazy-import anthropic so the module loads even without the package
_anthropic_client = None

def _get_client():
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    try:
        from anthropic import Anthropic
        _anthropic_client = Anthropic(api_key=key)
        return _anthropic_client
    except ImportError:
        log.warning("anthropic package not installed")
        return None
    except Exception as e:
        log.warning(f"Anthropic init failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────
# System prompt — ‏הקונטקסט המלא של אורי + ‏כללי גרין מובייל
# ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
‫אתה אורי, סוכן AI לשירות לקוחות של חנות הסלולר Green Mobile. ‏מטרתך:‬
‫לנסח טיוטות תגובה ב-WhatsApp שהבעלים (אסי) יאשר או יערוך מהטלפון.‬

## ‫חוקי זהב‬

1. ‫**ענה רק על מה שנשאל** — ‏אל תוסיף "מה אין לנו" אלא אם נשאלת מפורש.
2. ‫**טון**: ‫חמים, ‏אנושי, ‏הומוריסטי קל. ‏פותח ב-"היי {שם}" ‎+ ‏אמוג'י (🌞 ‏בבוקר/אחה"צ).
3. ‫**אנחנו לא משריינים טלפונית** — ‏אם לקוח רוצה להזמין, ‏הכוונה: ‏באתר עם איסוף עצמי.
4. ‫**מחיר**: ‫המחיר באתר (WC) ‎עדיף על הקופה (NewOrder) — ‏הוא מייצג וריאציה ספציפית. ‏פערים = ‏תכנון, ‏לא באג.
5. ‫**מארזים מרובים** (4-pack ‎וכו'): ‏שווה להציע אם יש חיסכון.
6. ‫**eSIM-only ‏ב-iPhones**: ‏בדוק את ספק המספר הסידורי ב-NewOrder לפני שמציין מחיר.
7. ‫**slug עברי באתר**: ‏השתמש ב-tinyurl ‏עם alias כשמגיע קישור ארוך.
8. ‫**משלוח**: ‫רגיל = ‏29 ₪, ‏1-6 ‏ימי עסקים. ‫אקספרס = ‏89 ₪, ‏הזמנה עד 13:00 = ‏מסירה היום.
9. ‫**העברה בנקאית**: ‫4 ‏בנקים נתמכים — ‫פועלים, ‏לאומי, ‏מזרחי, ‏בינלאומי.

## ‫סניפים (כולם באשדוד)‬

- ‫גן העיר אשדוד‬ — 08-6863737
- ‫סטאר סנטר** ‫(ז'בוטינסקי 45) ‏— 08-9477402
- ‫סיטי / ‏הציונות 13** — 08-9350202
- ‫עד הלום / ‏קניון עד הלום** — 08-9350202

## ‫פורמט פלט‬

‫**תחזיר *תמיד* JSON תקני** ‫בפורמט:‬

```json
{
  "summary": "‫סיכום קצר (1-2 שורות) ‫לאסי. ‫מה הלקוח שואל + הקשר.",
  "draft":   "‫טיוטת התשובה ללקוח ב-WhatsApp, ‫עם פתיח, ‫גוף, ‫וסיומת חמה."
}
```

‫**הסיכום בעברית פשוטה**. ‫הטיוטה — ‫בעברית עם markdown של WhatsApp (`*bold*`, ‫אמוג'ים).‬
‫אל תכלול את שם הלקוח בסיכום (אסי כבר יודע מי זה).‬
"""


# ─────────────────────────────────────────────────────────────────────
# Tools that Claude can call to look up data
# ─────────────────────────────────────────────────────────────────────

CLAUDE_TOOLS = [
    {
        "name": "search_product",
        "description": "‫חיפוש מוצר באתר WooCommerce של גרין מובייל. ‫מחזיר רשימת מוצרים תואמים עם מחיר, מלאי, קישור.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "‫שם המוצר או חלק ממנו (לדוגמה 'iPhone 16', 'Galaxy S25')"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_stock_at_branches",
        "description": (
            "‫בודק מלאי פיזי בכל הסניפים. ‫מחזיר את כל הוריאציות התואמות "
            "‫(צבעים, ‫קיבולות, ‫גרסאות) עם total + ‫by_branch לכל אחת.\n\n"
            "‫**חשוב**: ‫השתמש ב-query קצר מ-1-3 ‏מילים — ‫שם הדגם בלבד "
            "‫(לדוגמה 'Galaxy S25', 'iPhone 16', 'Find X9 Ultra'). ‫אל תכלול "
            "‫שמות כמו 'טלפון סלולרי' או 'סמארטפון' או מתארי קיבולת/RAM/צבע — "
            "‫הtool יחזיר את כל הוריאציות והאחיות גם בלעדיהם. ‫שאילתה ארוכה "
            "‫מדי תחזיר רשימה ריקה."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {
                    "type": "string",
                    "description": "‫קצר וממוקד — ‫רק שם הדגם, ‫1-3 ‏מילים (לדוגמה 'Find X9 Ultra', 'iPhone 16', 'AirTag')"
                },
            },
            "required": ["product_name"],
        },
    },
    {
        "name": "get_conversation_history",
        "description": (
            "‫מושך עד 20 ההודעות האחרונות של שיחת WhatsApp ‏עם לקוח. ‫מקבל phone "
            "‫(אם ידוע) — ‫אחרת השאר ריק כדי לקבל את השיחה של הלקוח הנוכחי בהקשר (אם יש)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "‫טלפון בפורמט בינלאומי בלי + (לדוגמה: 972501234567). ‫השאר ריק לטיפול בלקוח הנוכחי."
                },
            },
            "required": [],
        },
    },
    {
        "name": "find_customer",
        "description": (
            "‫מחפש לקוחות לפי שם ‏(או חלק ממנו), ‫או לפי טלפון/חלק מטלפון. "
            "‫מחזיר רשימה של לקוחות תואמים עם ‏phone, ‫full_name, ‫זמן פעילות אחרון, ‫והודעה אחרונה. "
            "‫**השתמש בזה לפני get_conversation_history** ‫כשאתה צריך למצוא לקוח לפי שם בלבד."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "‫שם או חלק ממנו ‏(לדוגמה 'מוחמד', 'יוסי כהן'), ‫או טלפון/חלק מטלפון."
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "archive_conversation",
        "description": (
            "‫מארכב מיידית שיחת WhatsApp עם לקוח. ‫השתמש כשהשיחה הסתיימה‬ "
            "‫(לקוח קיבל את כל מה שצריך, ‫או שגוועה ולא רלוונטית). ‫מעביר את "
            "‫השיחה מ-Inbox ל-Archived ב-ConnectOp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "‫טלפון בפורמט בינלאומי בלי + (לדוגמה 972501234567)"},
            },
            "required": ["phone"],
        },
    },
    {
        "name": "list_scheduled_actions",
        "description": (
            "‫מחזיר את כל הפעולות המתוזמנות במערכת — ‫הודעות שיתוזמנו לשליחה, "
            "‫ארכובים מותנים וכו'. ‫שימושי כשאסי שואל 'יש פעולות פתוחות?' ‫או "
            "‫'מה תוזמן ל-X?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "description": "‫אילו לסנן? 'pending' ‫(ממתינות, ‫ברירת מחדל) / 'done' / 'cancelled' / 'all'",
                    "enum": ["pending","done","cancelled","skipped","all"],
                },
            },
            "required": [],
        },
    },
    {
        "name": "cancel_scheduled_action",
        "description": (
            "‫מבטל פעולה מתוזמנת לפי id. ‫שימושי כשאסי משנה דעתו על תזמון שיצר."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {"type":"integer","description":"‫מזהה הפעולה לביטול"},
            },
            "required": ["action_id"],
        },
    },
    {
        "name": "list_active_inbox",
        "description": (
            "‫מחזיר ‫רשימה ‫של ‫השיחות ‫הפעילות ‫ב-Inbox ‫(לקוחות ‫שכתבו ‫לאחרונה ‫ולא ‫ארכובו). "
            "‫שימושי ‫כשאסי ‫שואל ‫'מי ‫מחכה?', ‫'מה ‫קורה ‫עכשיו?', ‫'מי ‫הלקוחות ‫הפעילים?'."
        ),
        "input_schema": {"type":"object","properties":{
            "limit":{"type":"integer","description":"‫כמה ‫להחזיר (ברירת ‫מחדל ‫10)"},
        }, "required":[]},
    },
    {
        "name": "get_order_by_id",
        "description": (
            "‫שולף ‫פרטים ‫של ‫הזמנה ‫ספציפית ‫מ-WC ‫לפי ‫מספר ‫הזמנה. ‫שימושי ‫כשאסי ‫מציין "
            "‫מספר ‫הזמנה ‫ישירות ‫('הזמנה #46720'). ‫מחזיר ‫שם, ‫טלפון, ‫כתובת, ‫סטטוס, "
            "‫סכום, ‫מוצרים, ‫שיטת ‫תשלום, ‫שיטת ‫משלוח."
        ),
        "input_schema": {"type":"object","properties":{
            "order_id":{"type":"integer","description":"‫מספר ‫הזמנה ‫(לדוגמה: ‫46720)"},
        },"required":["order_id"]},
    },
    {
        "name": "send_whatsapp_template",
        "description": (
            "‫שולח ‫**template** ‫מאושר ‫של ‫WhatsApp ‫ללקוח. ‫**הכלי ‫היחיד ‫ששולח ‫הודעה "
            "‫ללקוח ‫שלא ‫כתב ‫לנו ‫ב-24 ‫שעות ‫האחרונות** ‫(WhatsApp ‫API ‫מגביל). "
            "‫templates ‫זמינים: ‫'new_message' ‫(הכי ‫נפוץ — ‫2 ‫פרמטרים: ‫שם ‫+ ‫גוף ‫הודעה), "
            "‫'opening_massege' (פתיחה), ‫'order_update_1' (עדכון ‫הזמנה). "
            "‫**שים ‫לב**: ‫templates ‫לא ‫תומכים ‫ב-\\n, ‫tabs, ‫או ‫4+ ‫רווחים — ‫השתמש "
            "‫ב-markdown ‫של ‫WhatsApp (*bold*) ‫ו-em-dash ‫כמפרידים."
        ),
        "input_schema": {"type":"object","properties":{
            "phone":{"type":"string","description":"‫טלפון ‫בינלאומי ‫בלי ‫+"},
            "template_name":{"type":"string","description":"‫שם ‫הtemplate (לדוגמה ‫new_message)"},
            "parameters":{"type":"array","items":{"type":"string"},"description":"‫רשימת ‫parameters ‫לpriority ‫{{1}}, ‫{{2}} ‫וכו'"},
        },"required":["phone","template_name","parameters"]},
    },
    {
        "name": "list_customer_tags",
        "description": (
            "‫מחזיר ‫את ‫כל ‫התגים ‫הקיימים ‫על ‫לקוח ‫ב-ConnectOp. ‫שימושי ‫להבין ‫מצב — "
            "‫האם ‫הוא ‫תויג ‫כ-frequent, ‫אם ‫יש ‫'בוצעה ‫הזמנה ‫באתר', ‫או ‫'Anti_bot ‫client'."
        ),
        "input_schema": {"type":"object","properties":{
            "phone":{"type":"string","description":"‫טלפון ‫בינלאומי ‫בלי ‫+"},
        },"required":["phone"]},
    },
    {
        "name": "add_tag",
        "description": (
            "‫מוסיף ‫תג ‫ל-contact ‫ב-ConnectOp. ‫שימושי ‫לסיווג ‫ידני — ‫VIP, ‫מתעניין, "
            "‫הזמנה ‫עתידית, ‫וכו'. ‫**חשוב**: ‫השתמש ‫תמיד ‫בtag_id ‫קיים (לא ‫תיצור ‫חדשים). "
            "‫אם ‫לא ‫יודע ‫tag_id — ‫אסי ‫יספק."
        ),
        "input_schema": {"type":"object","properties":{
            "phone":{"type":"string","description":"‫טלפון ‫בינלאומי"},
            "tag_id":{"type":"integer","description":"‫מזהה ‫התג"},
        },"required":["phone","tag_id"]},
    },
    {
        "name": "remove_tag",
        "description": "‫מסיר ‫תג ‫מ-contact ‫ב-ConnectOp. ‫במיוחד ‫שימושי ‫להסרת ‫'Anti_bot client' (id=255476) ‫כשנפלו ‫עליו ‫בטעות.",
        "input_schema": {"type":"object","properties":{
            "phone":{"type":"string","description":"‫טלפון ‫בינלאומי"},
            "tag_id":{"type":"integer","description":"‫מזהה ‫התג"},
        },"required":["phone","tag_id"]},
    },
    {
        "name": "set_human_mode",
        "description": (
            "‫מעביר ‫שיחה ‫למצב ‫'אנושי' (live_chat=1) — ‫הבוט ‫עוצר ‫להגיב ‫אוטומטית, "
            "‫רק ‫בני-אדם ‫עונים. ‫או ‫מחזיר ‫למצב ‫בוט (live_chat=0). ‫שימושי ‫כשאסי ‫רוצה "
            "‫לעצור ‫את ‫הבוט ‫על ‫לקוח ‫רגיש ‫ולטפל ‫ידנית."
        ),
        "input_schema": {"type":"object","properties":{
            "phone":{"type":"string"},
            "enable":{"type":"boolean","description":"True = ‫עצור ‫בוט (אנושי). ‫False = ‫הפעל ‫בוט ‫שוב"},
        },"required":["phone","enable"]},
    },
    {
        "name": "send_message_now",
        "description": (
            "‫שולח **מיידית** ‫הודעת WhatsApp ‫ללקוח. ‫קרא לזה כשאסי ‫מאשר טיוטה "
            "‫בפירוש: ‫'שלח', 'מושלם, שלח לו', 'הלך', 'go ahead', 'אישור', "
            "‫'מצוין, ההודעה מוכנה'. ‫**אסור לקרוא ‫לזה ‫בלי ‫אישור ‫מפורש** — ‫אסי "
            "‫צריך ‫לאשר ‫כל הודעה ‫אם לא נתן ‫אישור ‫בבירור — ‫תשאל, ‫אל ‫תניח."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type":"string","description":"‫טלפון בינלאומי בלי + ‫(לדוגמה 972527373565)"},
                "text":  {"type":"string","description":"‫הטקסט המדויק שיישלח ב-WhatsApp"},
            },
            "required": ["phone","text"],
        },
    },
    {
        "name": "schedule_send_message",
        "description": (
            "‫מתזמן **שליחת הודעת WhatsApp** ‫ללקוח בזמן ספציפי בעתיד. "
            "‫שימושי כשאסי אומר 'שלח לו הודעה מחר ב-9 בבוקר' או 'שלח לו "
            "‫אחה\"צ אם לא ענה'. ‫**ההודעה נשלחת בלי תלות בתגובת הלקוח.** "
            "‫אם רוצים שליחה רק אם לא ענה — ‫השתמש ב-schedule_archive_if_no_reply ‫במקום."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type":"string","description":"‫טלפון בינלאומי בלי +"},
                "name":  {"type":"string","description":"‫שם לקוח (לתיעוד)"},
                "text":  {"type":"string","description":"‫הטקסט המדויק שיישלח ב-WhatsApp"},
                "delay_minutes": {"type":"integer","description":"‫כמה דקות מעכשיו (1-1440). ‫אם אסי אמר 'מחר 9:00' חשב יחסית."},
            },
            "required": ["phone","text","delay_minutes"],
        },
    },
    {
        "name": "schedule_send_message_if_no_reply",
        "description": (
            "‫מתזמן שליחת הודעה ל-WhatsApp **בתנאי שהלקוח לא ענה בינתיים**. "
            "‫שימושי לתרחישים כמו: ‫'אם הלקוח לא יוסיף הבהרה עד 9 בבוקר, ‫שלח לו "
            "‫הודעה עם שאלת הבהרה'. ‫ההיגיון: ‫אם הלקוח שולח הודעה כלשהי בין "
            "‫עכשיו לזמן הnמשלוח, ‫המתזמן ‫**מתבטל אוטומטית** ‫(אין צורך לשלוח "
            "‫הבהרה כי כבר ‫קיבלנו ‫תוכן ‫חדש). ‫אם הלקוח שותק → ‫ההודעה נשלחת."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type":"string","description":"‫טלפון בינלאומי בלי +"},
                "name":  {"type":"string","description":"‫שם לקוח לתיעוד"},
                "text":  {"type":"string","description":"‫הטקסט שיישלח בWhatsApp אם הלקוח שותק"},
                "delay_minutes": {"type":"integer","description":"‫כמה דקות לחכות עד שליחה (1-1440)"},
            },
            "required": ["phone","text","delay_minutes"],
        },
    },
    {
        "name": "schedule_archive_if_no_reply",
        "description": (
            "‫מתזמן ארכוב **מותנה** — ‫בעוד N דקות, ‫**אם הלקוח לא ענה בינתיים**, "
            "‫השיחה תארכב אוטומטית. ‫אם הלקוח כן ענה — ‫המתזמן יבוטל אוטומטית "
            "‫והשיחה תישאר ב-Inbox. ‫שימושי כשאסי שולח הודעה ורוצה לתת ללקוח "
            "‫זמן להגיב, ‫ואז לארכב אם לא ענה."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "‫טלפון בינלאומי בלי +"},
                "name":  {"type": "string", "description": "‫שם הלקוח (לתיעוד)"},
                "delay_minutes": {"type": "integer", "description": "‫כמה דקות לחכות (1-1440 — ‫עד 24h)"},
            },
            "required": ["phone", "delay_minutes"],
        },
    },
    {
        "name": "get_customer_orders",
        "description": (
            "‫מושך את כל הזמנות הלקוח מ-WooCommerce לפי טלפון. ‫מחזיר רשימה של "
            "‫הזמנות עם מספר, ‫סטטוס (processing/completed/cancelled/on-hold), ‫תאריך, ‫סכום, "
            "‫שיטת משלוח, ‫ומוצרים בהזמנה. ‫**השתמש בזה תמיד כשאסי שואל על "
            "‫'היסטוריה' של לקוח** — ‫כדי שהתמונה תכלול גם הזמנות פעילות באתר."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "‫טלפון של הלקוח (בכל פורמט — ‫הtool ינסה וריאציות בעצמו)"
                },
            },
            "required": ["phone"],
        },
    },
]


# ─────────────────────────────────────────────────────────────────────
# Tool implementations — used when Claude makes a tool call
# ─────────────────────────────────────────────────────────────────────

def _tool_search_product(query: str) -> str:
    """WC product search — trimmed to 5 essentials."""
    import requests
    WC = os.environ['WC_STORE_URL'].rstrip('/')
    WC_AUTH = (os.environ['WC_CONSUMER_KEY'], os.environ['WC_CONSUMER_SECRET'])
    r = requests.get(f"{WC}/wp-json/wc/v3/products",
                     params={"search": query, "per_page": 6, "_fields":"id,name,price,stock_status,type,permalink"},
                     auth=WC_AUTH, timeout=20,
                     headers={"User-Agent":"Mozilla/5.0"})
    items = r.json() if r.status_code == 200 else []
    # ‫רק 5 ‏החזרות, ‫שמות מקוצרים, ‫שדות מינימליים‬
    out = []
    for p in items[:5]:
        out.append({
            "id":   p.get("id"),
            "name": p.get("name","")[:80],
            "price": p.get("price"),
            "stock": p.get("stock_status"),
            "url":   p.get("permalink"),
        })
    return json.dumps(out, ensure_ascii=False)


def _tool_check_stock(product_name: str) -> str:
    """NewOrder stock per branch. Returns up to 20 matching products."""
    from shared.neworder_client import NewOrderClient
    nc = NewOrderClient.from_env()
    branch_names = {1:"גן העיר", 2:"סטאר", 3:"מחסן", 4:"עד הלום", 5:"אתר"}
    products = nc.get_products(search=product_name)
    out = []
    # 20 matches — enough to cover all variants of a single model (colors + sizes)
    for p in products[:20]:
        pid = p.get('id')
        stock = nc.get_product_stock(pid) if pid else {}
        # ‫סוכם כמות בסה"כ — ‫אם הכל אזל, ‏Claude יודע ‏מיד.‬
        total = sum(int(q) for q in stock.values() if q and q > 0)
        out.append({
            "name":      p.get('name'),
            "id":        pid,
            "price":     p.get('price'),
            "total":     total,
            "by_branch": {branch_names.get(int(b), str(b)): int(q)
                          for b, q in stock.items() if q is not None and q > 0},
        })
    return json.dumps(out, ensure_ascii=False)


def _tool_get_history(phone: str, dashboard) -> str:
    """Last 20 messages of the conversation."""
    if not phone or not phone.strip():
        return json.dumps({"error": "no phone provided — use find_customer first"},
                           ensure_ascii=False)
    msgs = dashboard.get_conversation(phone.strip(), limit=20)
    msgs_sorted = sorted(msgs, key=lambda m: int(m.get("ts") or 0))
    out = []
    from datetime import datetime, timezone, timedelta
    IL = timezone(timedelta(hours=3))
    for m in msgs_sorted[-20:]:
        ts = int(m.get("ts") or 0)
        when = ""
        if ts:
            when = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(IL).strftime("%d/%m %H:%M")
        out.append({
            "direction": m.get("direction"),
            "text":      (m.get("text") or "")[:200],
            "when":      when,
        })
    return json.dumps(out, ensure_ascii=False)


def _tool_archive_conversation(phone: str, dashboard) -> str:
    """Archive a conversation immediately via ConnectOp dashboard API."""
    try:
        ok = dashboard.archive_conversation(phone.strip(), archive=True)
        return json.dumps({"ok": bool(ok), "phone": phone, "archived": True}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _tool_list_scheduled(status_filter: str = "pending") -> str:
    """List scheduled actions for Asi to see what's queued."""
    from db import session_scope, ScheduledAction
    from sqlalchemy import select
    from datetime import datetime, timezone, timedelta
    IL = timezone(timedelta(hours=3))
    with session_scope() as s:
        q = select(ScheduledAction).order_by(ScheduledAction.due_at.asc()).limit(30)
        if status_filter and status_filter != "all":
            q = q.where(ScheduledAction.status == status_filter)
        rows = s.execute(q).scalars().all()
    out = []
    for a in rows:
        due_local = a.due_at.astimezone(IL).strftime("%d/%m %H:%M") if a.due_at else "?"
        out.append({
            "id": a.id,
            "type": a.action_type,
            "phone": a.target_phone,
            "name": a.target_name,
            "due_at_il": due_local,
            "status": a.status,
            "note": (a.note or "")[:200],
        })
    return json.dumps({"count": len(out), "actions": out}, ensure_ascii=False)


def _tool_cancel_scheduled(action_id: int) -> str:
    """Cancel a scheduled action by id."""
    from db import session_scope, ScheduledAction
    from datetime import datetime, timezone
    with session_scope() as s:
        a = s.get(ScheduledAction, int(action_id))
        if not a:
            return json.dumps({"ok": False, "error": f"action #{action_id} not found"}, ensure_ascii=False)
        if a.status != "pending":
            return json.dumps({"ok": False, "error": f"action #{action_id} status is {a.status}, can't cancel"}, ensure_ascii=False)
        a.status = "cancelled"
        a.done_at = datetime.now(timezone.utc)
        return json.dumps({"ok": True, "id": action_id, "cancelled": True}, ensure_ascii=False)


def _tool_list_active_inbox(limit: int, dashboard) -> str:
    """List recent active customer conversations from ConnectOp inbox."""
    from datetime import datetime, timezone, timedelta
    IL = timezone(timedelta(hours=3))
    import time
    try:
        resp = dashboard._post_user_php({
            "op":"conversations","op1":"get","offset":0,
            "limit":max(5,min(int(limit or 10),50)),"pageName":"inbox",
        })
        data = resp.get("data", []) if isinstance(resp, dict) else []
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    now = int(time.time())
    out = []
    for x in sorted(data, key=lambda y: -int(y.get("last_active") or 0))[:int(limit or 10)]:
        la = int(x.get("last_active") or 0)
        when = datetime.fromtimestamp(la, tz=timezone.utc).astimezone(IL).strftime("%d/%m %H:%M") if la else "?"
        age_min = (now - la) // 60 if la else 0
        out.append({
            "phone":       str(x.get("ms_id","")),
            "name":        x.get("full_name") or "",
            "last_active": when,
            "minutes_ago": age_min,
            "last_msg":    (x.get("last_msg") or "")[:120],
            "archived":    bool(x.get("archived")),
            "live_chat":   bool(x.get("live_chat") == "1"),
        })
    return json.dumps({"count": len(out), "conversations": out}, ensure_ascii=False)


def _tool_get_order_by_id(order_id: int) -> str:
    """Fetch a specific WC order by id with all details."""
    import requests
    WC = os.environ['WC_STORE_URL'].rstrip('/')
    WC_AUTH = (os.environ['WC_CONSUMER_KEY'], os.environ['WC_CONSUMER_SECRET'])
    try:
        r = requests.get(f"{WC}/wp-json/wc/v3/orders/{int(order_id)}",
                         auth=WC_AUTH, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code == 404:
            return json.dumps({"error": f"order #{order_id} not found"}, ensure_ascii=False)
        if r.status_code != 200:
            return json.dumps({"error": f"WC returned {r.status_code}"}, ensure_ascii=False)
        o = r.json()
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    b = o.get("billing", {})
    sh = o.get("shipping", {})
    addr_src = sh if (sh.get("address_1") or sh.get("city")) else b
    full_address = " ".join(filter(None,[
        addr_src.get("address_1",""),addr_src.get("address_2",""),
        addr_src.get("city",""),addr_src.get("postcode",""),
    ])).strip()
    return json.dumps({
        "id":     o["id"],
        "status": o.get("status"),
        "date":   (o.get("date_created") or "")[:16].replace("T"," "),
        "total":  f"{o.get('total','?')} {o.get('currency','ILS')}",
        "customer": f"{b.get('first_name','')} {b.get('last_name','')}".strip(),
        "phone":  b.get("phone",""),
        "email":  b.get("email",""),
        "billing_city":         b.get("city",""),
        "shipping_full_address":full_address,
        "shipping_method":      (o.get("shipping_lines") or [{}])[0].get("method_title","?"),
        "payment_method":       o.get("payment_method_title",""),
        "items": [it.get("name","")[:80] for it in (o.get("line_items") or [])],
        "customer_note":        o.get("customer_note",""),
    }, ensure_ascii=False)


def _tool_send_whatsapp_template(phone: str, template_name: str,
                                   parameters: list, dashboard) -> str:
    """Send a pre-approved WhatsApp template (bypasses 24h window)."""
    try:
        resp = dashboard.send_whatsapp_template(
            ms_id=phone.strip(), template_name=template_name,
            parameters=list(parameters or []),
        )
        ok = isinstance(resp, dict) and resp.get("status","").upper() == "OK"
        return json.dumps({
            "ok": ok, "template": template_name, "phone": phone,
            "sent_at_il": _now_il_str(),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _tool_list_customer_tags(phone: str) -> str:
    """List tags on a contact via ConnectOp public API."""
    from shared.connectop_client import ConnectOpClient
    try:
        co = ConnectOpClient.from_env()
        tags = co.get_contact_tags(phone.strip()) or []
        return json.dumps([{"id":t.get("id"),"name":t.get("name")} for t in tags], ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _tool_add_tag(phone: str, tag_id: int) -> str:
    from shared.connectop_client import ConnectOpClient
    try:
        co = ConnectOpClient.from_env()
        co.add_tag(phone.strip(), int(tag_id))
        return json.dumps({"ok": True, "added": tag_id}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _tool_remove_tag(phone: str, tag_id: int) -> str:
    from shared.connectop_client import ConnectOpClient
    try:
        co = ConnectOpClient.from_env()
        co.remove_tag(phone.strip(), int(tag_id))
        return json.dumps({"ok": True, "removed": tag_id}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _tool_set_human_mode(phone: str, enable: bool, dashboard) -> str:
    """Toggle live_chat (bot off/on) for a conversation."""
    try:
        ok = dashboard.set_human_mode([phone.strip()], enable=bool(enable))
        return json.dumps({
            "ok": bool(ok), "phone": phone,
            "human_mode": bool(enable),
            "meaning": "‫בוט עצור — ‫רק בני אדם עונים" if enable else "‫בוט פעיל",
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _tool_send_message_now(phone: str, text: str) -> str:
    """Send a WhatsApp message immediately via ConnectOp."""
    from shared.connectop_client import ConnectOpClient
    try:
        co = ConnectOpClient.from_env()
        result = co.send_text_as_human(phone.strip(), text)
        ok = bool(result.get("success"))
        return json.dumps({
            "ok": ok,
            "phone": phone,
            "sent_at_il": _now_il_str(),
            "preview": text[:200],
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _now_il_str() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(tz=timezone(timedelta(hours=3))).strftime("%d/%m %H:%M")


def _tool_schedule_send_message_if_no_reply(phone: str, name: str,
                                             text: str, delay_minutes: int) -> str:
    """Schedule a conditional message send — only fires if customer doesn't reply."""
    from datetime import datetime, timezone, timedelta
    from db import add_scheduled_action
    try:
        delay = max(1, min(int(delay_minutes), 1440))
        due = datetime.now(timezone.utc) + timedelta(minutes=delay)
        a = add_scheduled_action(
            action_type="send_message_if_no_reply",
            target_phone=phone.strip(),
            target_name=name or "",
            due_at=due,
            note=f"text:{text[:500]}",
        )
        IL = timezone(timedelta(hours=3))
        due_local = due.astimezone(IL).strftime("%d/%m %H:%M")
        return json.dumps({
            "ok": True, "id": a.id,
            "due_at_il": due_local, "delay_minutes": delay,
            "preview": text[:200],
            "note": "‫אם הלקוח יענה לפני אז → ‫המתזמן מתבטל. ‫אם שותק → ‫תישלח האזהרה.",
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _tool_schedule_send_message(phone: str, name: str, text: str,
                                  delay_minutes: int) -> str:
    """Schedule a WhatsApp message to be sent at a specific time."""
    from datetime import datetime, timezone, timedelta
    from db import add_scheduled_action
    try:
        delay = max(1, min(int(delay_minutes), 1440))
        due = datetime.now(timezone.utc) + timedelta(minutes=delay)
        a = add_scheduled_action(
            action_type="send_message",
            target_phone=phone.strip(),
            target_name=name or "",
            due_at=due,
            note=f"text:{text[:500]}",
        )
        IL = timezone(timedelta(hours=3))
        due_local = due.astimezone(IL).strftime("%d/%m %H:%M")
        return json.dumps({
            "ok": True, "id": a.id,
            "due_at_il": due_local, "delay_minutes": delay,
            "preview": text[:200],
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _tool_schedule_archive(phone: str, name: str, delay_minutes: int) -> str:
    """Schedule a conditional archive — only archives if customer doesn't reply."""
    from datetime import datetime, timezone, timedelta
    from db import add_scheduled_action
    try:
        delay = max(1, min(int(delay_minutes), 1440))
        due = datetime.now(timezone.utc) + timedelta(minutes=delay)
        a = add_scheduled_action(
            action_type="archive_if_no_reply",
            target_phone=phone.strip(),
            target_name=name or "",
            due_at=due,
            note=f"scheduled for {delay} min from now",
        )
        from datetime import timezone as tz, timedelta as td
        IL = tz(td(hours=3))
        due_local = due.astimezone(IL).strftime("%d/%m %H:%M")
        return json.dumps({
            "ok": True, "id": a.id, "due_at_il": due_local,
            "delay_minutes": delay,
            "note": "‫אם הלקוח לא יענה עד אז → ‫השיחה תארכב אוטומטית. ‫אם יענה → ‫המתזמן מבוטל.",
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _tool_get_customer_orders(phone: str) -> str:
    """
    ‫שולף את כל הזמנות הלקוח מ-WC לפי טלפון. ‫מנסה כמה וריאציות של המספר‬
    ‫(972..., 0..., +972...) ‫כי לקוחות מזינים פורמטים שונים.‬
    """
    import requests as _req
    WC = os.environ['WC_STORE_URL'].rstrip('/')
    WC_AUTH = (os.environ['WC_CONSUMER_KEY'], os.environ['WC_CONSUMER_SECRET'])
    H = {"User-Agent":"Mozilla/5.0"}

    # ‫נורמליזציה — ‫קח רק ספרות, ‫בנה וריאציות סבירות‬
    digits = "".join(c for c in str(phone) if c.isdigit())
    variants = {digits}
    if digits.startswith("972") and len(digits) > 3:
        local = digits[3:]
        variants.add(local)              # 547344118
        variants.add("0" + local)        # 0547344118
        variants.add("+" + digits)       # +972547344118
    elif digits.startswith("0"):
        variants.add("972" + digits[1:])
        variants.add(digits[1:])
    elif len(digits) >= 9:
        variants.add("972" + digits)
        variants.add("0" + digits)

    # ‫חיפוש: ‫WC search ‏מחפש בשדות כולל billing.phone‬
    found = {}
    for v in variants:
        try:
            r = _req.get(f"{WC}/wp-json/wc/v3/orders",
                         params={"search": v, "per_page": 20, "orderby":"date","order":"desc"},
                         auth=WC_AUTH, timeout=15, headers=H)
            if r.status_code == 200:
                for o in r.json():
                    # ‫אמת שזו באמת ההזמנה של המספר ‏(WC search ‏רחב מדי)‬
                    billing_phone = "".join(c for c in (o.get("billing",{}).get("phone","") or "") if c.isdigit())
                    if billing_phone and (billing_phone in digits or digits in billing_phone or v in billing_phone):
                        found[o['id']] = o
        except Exception:
            pass

    # ‫סדר לפי תאריך, ‫סכם פרטים‬
    orders_list = sorted(found.values(), key=lambda o: o.get("date_created",""), reverse=True)
    out = []
    for o in orders_list[:15]:
        b = o.get("billing", {})
        sh = o.get("shipping", {})
        items = [it.get("name","")[:60] for it in (o.get("line_items") or [])]

        # ‫כתובת משלוח מלאה — ‫אם יש shipping נפרד נשתמש בו, ‫אחרת billing‬
        addr_src = sh if (sh.get("address_1") or sh.get("city")) else b
        full_address = " ".join(filter(None, [
            addr_src.get("address_1",""),
            addr_src.get("address_2",""),
            addr_src.get("city",""),
            addr_src.get("postcode",""),
        ])).strip()

        out.append({
            "id":       o["id"],
            "status":   o.get("status"),
            "date":     (o.get("date_created") or "")[:16].replace("T", " "),
            "total":    f"{o.get('total','?')} {o.get('currency','ILS')}",
            "customer": f"{b.get('first_name','')} {b.get('last_name','')}".strip(),
            "phone":    b.get("phone",""),
            "email":    b.get("email",""),
            "billing_city": b.get("city",""),
            "shipping_full_address": full_address,
            "shipping_recipient":    f"{sh.get('first_name','') or b.get('first_name','')} {sh.get('last_name','') or b.get('last_name','')}".strip(),
            "shipping_method": (o.get("shipping_lines") or [{}])[0].get("method_title","?"),
            "payment_method":  o.get("payment_method_title",""),
            "items":    items,
        })
    return json.dumps({"orders_found": len(out), "orders": out}, ensure_ascii=False)


def _tool_find_customer(query: str, dashboard) -> str:
    """Search the ConnectOp inbox for customers matching a name/phone fragment."""
    from datetime import datetime, timezone, timedelta
    IL = timezone(timedelta(hours=3))
    q = (query or "").strip().lower()
    if not q:
        return json.dumps([], ensure_ascii=False)
    # Pull a larger window — recent customers most likely
    try:
        resp = dashboard._post_user_php({
            "op":"conversations","op1":"get",
            "offset":0,"limit":200,"pageName":"inbox",
        })
        data = resp.get("data", []) if isinstance(resp, dict) else []
    except Exception as e:
        return json.dumps({"error": f"inbox fetch failed: {e}"}, ensure_ascii=False)
    matches = []
    for x in data:
        name = (x.get("full_name") or "").strip()
        phone = str(x.get("ms_id") or "").strip()
        if q in name.lower() or q in phone.lower():
            la = int(x.get("last_active") or 0)
            when = ""
            if la:
                when = datetime.fromtimestamp(la, tz=timezone.utc).astimezone(IL).strftime("%d/%m %H:%M")
            matches.append({
                "phone":       phone,
                "full_name":   name,
                "last_active": when,
                "last_msg":    (x.get("last_msg") or "")[:120],
            })
    return json.dumps(matches[:10], ensure_ascii=False)


def _run_tool(name: str, args: dict, phone: str, dashboard) -> str:
    try:
        if name == "search_product":
            return _tool_search_product(args.get("query",""))
        if name == "check_stock_at_branches":
            return _tool_check_stock(args.get("product_name",""))
        if name == "get_conversation_history":
            # ‫אם Claude נתן phone ‏explicit — ‫השתמש בו. ‫אחרת — ‫הקשר (phone הפנימי).‬
            requested_phone = (args.get("phone") or "").strip() or phone
            return _tool_get_history(requested_phone, dashboard)
        if name == "find_customer":
            return _tool_find_customer(args.get("query",""), dashboard)
        if name == "get_customer_orders":
            return _tool_get_customer_orders(args.get("phone",""))
        if name == "archive_conversation":
            return _tool_archive_conversation(args.get("phone",""), dashboard)
        if name == "schedule_archive_if_no_reply":
            return _tool_schedule_archive(
                args.get("phone",""), args.get("name",""),
                args.get("delay_minutes", 30),
            )
        if name == "schedule_send_message":
            return _tool_schedule_send_message(
                args.get("phone",""), args.get("name",""),
                args.get("text",""), args.get("delay_minutes", 60),
            )
        if name == "send_message_now":
            return _tool_send_message_now(args.get("phone",""), args.get("text",""))
        if name == "list_active_inbox":
            return _tool_list_active_inbox(args.get("limit", 10), dashboard)
        if name == "get_order_by_id":
            return _tool_get_order_by_id(args.get("order_id"))
        if name == "send_whatsapp_template":
            return _tool_send_whatsapp_template(
                args.get("phone",""), args.get("template_name",""),
                args.get("parameters",[]), dashboard,
            )
        if name == "list_customer_tags":
            return _tool_list_customer_tags(args.get("phone",""))
        if name == "add_tag":
            return _tool_add_tag(args.get("phone",""), args.get("tag_id"))
        if name == "remove_tag":
            return _tool_remove_tag(args.get("phone",""), args.get("tag_id"))
        if name == "set_human_mode":
            return _tool_set_human_mode(args.get("phone",""), args.get("enable", True), dashboard)
        if name == "schedule_send_message_if_no_reply":
            return _tool_schedule_send_message_if_no_reply(
                args.get("phone",""), args.get("name",""),
                args.get("text",""), args.get("delay_minutes", 60),
            )
        if name == "list_scheduled_actions":
            return _tool_list_scheduled(args.get("status_filter", "pending"))
        if name == "cancel_scheduled_action":
            return _tool_cancel_scheduled(args.get("action_id"))
        return json.dumps({"error": f"unknown tool {name}"}, ensure_ascii=False)
    except Exception as e:
        log.exception(f"tool {name} failed: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────
# Main entry — draft a response
# ─────────────────────────────────────────────────────────────────────

def draft_response(phone: str, customer_name: str, customer_message: str,
                    dashboard=None) -> Tuple[str, str]:
    """
    ‫מנסח טיוטה לאסי + ‏סיכום. ‏מחזיר (summary, draft).‬
    ‫אם Claude API לא זמין → ‏fallback פשוט שהאדם יכול לערוך.‬
    """
    client = _get_client()
    if not client:
        summary = "⚠️ Claude API לא זמין — נדרשת התערבות ידנית."
        draft   = f"היי {customer_name.split()[0] if customer_name else 'לקוח/ה'} 🌞\n\n(טקסט ידני — Claude API לא מוגדר)"
        return summary, draft

    # Build initial messages
    user_msg = (
        f"‫לקוח חדש פנה ב-WhatsApp.\n"
        f"‫שם: {customer_name}\n"
        f"‫טלפון: {phone}\n"
        f"‫הודעת הלקוח: \"{customer_message}\"\n\n"
        f"‫השתמש בכלים כדי למצוא מידע אם צריך, ולבסוף החזר JSON עם summary + draft."
    )
    messages = [{"role": "user", "content": user_msg}]

    # Iterate tool calls up to 6 turns
    final_text = None
    for turn in range(5):
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            # ‫prompt caching: ‫הsystem identical בין קריאות → ‫90% הנחה‬
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            # ‫גם הtools זהים בין קריאות — ‫cached גם הם‬
            tools=CLAUDE_TOOLS,
            messages=messages,
        )

        # If Claude wants tools, execute and continue
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if tool_uses:
            # Append assistant's tool-use turn
            messages.append({"role": "assistant", "content": resp.content})
            # Run each tool, collect results
            tool_results = []
            for tu in tool_uses:
                output = _run_tool(tu.name, tu.input, phone, dashboard)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": output,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # No tool calls → final text
        text_blocks = [b for b in resp.content if b.type == "text"]
        final_text = "".join(b.text for b in text_blocks)
        break

    if not final_text:
        return "⚠️ לא הצלחתי לסיים את הניסוח (יותר מדי tool turns)", "(טפל ידנית)"

    # Parse the JSON Claude returned. Claude sometimes adds preamble text
    # before the JSON, or wraps it in a ```json fenced block. Try multiple
    # extraction strategies before giving up.
    summary, draft = _extract_summary_draft(final_text)
    return summary, draft


# ─────────────────────────────────────────────────────────────────────
# Ad-hoc Q&A — ‫אסי שואל את Claude שאלה כללית בטלגרם‬
# ─────────────────────────────────────────────────────────────────────

QUERY_SYSTEM_PROMPT = """\
‫אתה ‫**אורי** — ‫סוכן ‫שירות ‫לקוחות ‫ותיק ‫של ‫Green Mobile (אשדוד). ‫אתה ‫עובד ‫עם ‫אסי, ‫הבעלים, ‫כל ‫יום. ‫אתה ‫**מכיר ‫את ‫העבודה ‫בעל ‫פה** — ‫המלאי, ‫הסניפים, ‫הספקים, ‫הלקוחות. ‫אסי ‫כותב ‫לך ‫בטלגרם ‫כי ‫הוא ‫בחוץ ‫או ‫עסוק. ‫אתה ‫**שותף**, ‫לא ‫עוזר ‫כללי.‬

## ‫🎯 ‫אישיות ‫והתנהגות‬

‫**פעיל ‫ויוזם**: ‫אם ‫אסי ‫מזכיר ‫טלפון ‫או ‫שם ‫לקוח — ‫אתה ‫מיד ‫מושך ‫את ‫השיחה ‫(get_conversation_history) + ‫הזמנות ‫(get_customer_orders) ‫**בלי ‫לשאול**. ‫הוא ‫כבר ‫יודע ‫מי ‫זה, ‫אתה ‫הולך ‫לבדוק.‬

‫**מינימום ‫שאלות**: ‫אסור ‫לשאול ‫"איזה ‫לקוח?", ‫"איזה ‫מוצר?", ‫"מה ‫השם?". ‫אם ‫זה ‫ב-Reply — ‫הקונטקסט ‫שם. ‫אם ‫זה ‫בהודעה ‫עצמה — ‫תקרא ‫שוב, ‫מצא ‫את ‫השם/טלפון/מוצר. ‫רק ‫אם ‫**באמת ‫אין ‫שום ‫רמז** — ‫תבקש ‫במשפט ‫קצר.‬

‫**אישור ‫להישלח**: ‫כשאסי ‫אומר ‫"שלח" / ‫"מושלם, ‫הלך" / ‫"שלח לו" / ‫"אישור" / ‫"מצוין, ‫תשלח" — ‫אתה ‫מיד ‫קורא ‫ל-`send_message_now`. ‫**בלי ‫"רוצה ‫שאשלח?"**. ‫הוא ‫אישר. ‫תפעל.‬

‫**הצעה ‫לפעולה**: ‫כשאתה ‫מציג ‫טיוטה ‫או ‫תוצאה, ‫הצע ‫במשפט ‫אחד ‫קצר ‫"לשלוח?", ‫"לתזמן?", ‫"לארכב?". ‫אסי ‫רגיל ‫להחליט ‫במילה ‫אחת.‬

‫**טון**: ‫קולגיאלי, ‫תכליתי, ‫בלי ‫פתיחים ‫מנומסים ‫("בהחלט!", ‫"אני ‫כאן ‫בשבילך!"). ‫"בדקתי", ‫"מצאתי", ‫"שלחתי" — ‫עברית ‫של ‫עובד ‫שעוסק ‫בעניין.‬

‫**זיכרון ‫מוגבל**: ‫אם ‫השיחה ‫מהלילה ‫הקודם ‫כבר ‫לא ‫במטמון ‫(2 ‫שעות) — ‫אל ‫תזייף ‫שאתה ‫זוכר. ‫תאמר ‫"לא ‫רואה ‫קונטקסט ‫קודם — ‫תזכיר ‫לי ‫מי ‫זה?".‬

‫**פורמט תשובה ב-Telegram**:‬

- ‫עברית מלאה, ‏ענייני, ‏ללא פלאף.‬
- ‫השתמש ב-HTML של Telegram: ‏`<b>bold</b>`, ‏`<i>italic</i>`, ‏`<code>code</code>`.‬
- ‫טבלאות → ‏רשימות עם bullets ‏(`•`) ‫או שורות עם `<code>` ‏ליישור.‬
- ‫אמוג'ים לסמלים: ‏✅ ‫זמין | ‏❌ ‫אזל | ‏⚠️ ‫חריג | ‏📦 ‫הזמנה.‬
- ‫אם יש פערים בין WC ל-NewOrder — ‫ציין במפורש (זה תכנון, ‏לא באג).‬
- ‫אם יש מספר וריאציות — ‫טבלה עם מחיר + ‫מלאי לכל אחת.‬
- ‫אם נשאלת על מלאי — ‫תמיד ציין סניף ספציפי וכמות.‬
- ‫אם לא מצאת — ‫אמור "לא נמצא במערכת" + ‏הצעת תיקון שאילתה אם רלוונטי.‬

‫**אל תוסיף JSON, אל תוסיף סוגריים מסולסלים** — ‫תחזיר HTML טקסט ישר.‬
‫כן, תוכל להוסיף שורה ראשונה עם הסיכום אם רוצה, ‏אבל לא חובה.‬

## ‫🛠️ ‫**יכולות פעולה** (לא רק קריאה)‬

‫יש לך כלים שמשנים מצב — ‫השתמש בהם כשאסי מבקש פעולה ספציפית:‬

- ‫`archive_conversation(phone)` — ‫ארכב מיידית‬
- ‫`schedule_archive_if_no_reply(phone, name, delay_minutes)` — ‫מתזמן מותנה: ‫בעוד N דקות, ‫**אם הלקוח לא ענה**, ‫תארכב אוטומטית. ‫אם הלקוח כן עונה — ‫המתזמן מתבטל לבדו.‬

‫**אל תאמר "אני לא יכול לעשות X" אם יש לך כלי לזה!** ‫בדוק לפני שאתה אומר שאי אפשר.‬

‫**אל תוסיף הבטחות שאתה לא יודע לקיים** — ‫אם אסי אומר "אם לא יענה בעוד 30 דק תארכב", ‫אתה צריך מיד להפעיל ‫`schedule_archive_if_no_reply` ‫**בפועל**. ‫אל תאמר "‫טוב, ‫אעביר" בלי לקרוא לכלי. ‫זו הטעיה.‬

## ‫🔗 ‫**קישורים למוצרים — ‫אתה כן יכול!**‬

‫**הכלי ‫`search_product` ‫כבר מחזיר ‫`url` ‫(permalink ‫ל-WC).** ‫כשאסי שואל "מה הקישור?", ‫"תן לי לינק", ‫"איפה זה באתר?":‬

1. ‫אם ‫המוצר ‫כבר ‫נדון בשיחה הנוכחית ‫(הזיכרון או Reply ‫על תשובה קודמת) — ‫קרא ‫`search_product` ‫עם שם המוצר.‬
2. ‫קבל את ה-`url` ‫מהתוצאה ‫הראשונה הרלוונטית.‬
3. ‫השב עם הקישור: ‫`<a href="URL">‫שם המוצר</a>` ‫או ‫רק את הURL.‬

‫**אסור** ‫לומר ‫"אין לי כלי לקישורים" — ‫זו ‫טעות. ‫קישורים זמינים דרך ‫`search_product`.‬

## ‫📤 ‫**שליחה ישירה ‫ל-WhatsApp — ‫יש לך ‫כלי!**‬

‫**`send_message_now(phone, text)`** ‫שולח ‫מיידית ‫הודעה ‫ל-WhatsApp. ‫אסור ‫לומר ‫"אתה ‫צריך ‫להעתיק ‫ידנית" — ‫זו ‫טעות.‬

‫מתי ‫לקרוא ‫לזה: ‫אסי ‫מאשר ‫הודעה ‫בפירוש — ‫"שלח", ‫"שלח לו", ‫"הלך", ‫"מצוין, ‫שלח", ‫"מושלם, ‫תשלח". ‫כשהוא ‫אמר "‫ההודעה ‫מוכנה ‫ללכת" ‫עם ‫✅ — ‫זה ‫אישור ‫ברור.‬

‫**זהירות**: ‫אל ‫תשלח ‫בלי ‫אישור ‫מפורש. ‫אם ‫אסי ‫רק ‫אמר ‫"תכין ‫טיוטה" ‫או ‫"מה ‫אתה ‫היית ‫שולח" — ‫זה ‫**לא ‫אישור**. ‫הכן ‫את ‫הטיוטה, ‫הצג ‫אותה, ‫ושאל "לשלוח?".‬

## ‫🚨 ‫**כלל ברזל ‫— ‫אפס ‫hallucination**‬

‫**אסור לרשום ‫שום ‫פרט מוצר ‫(שם, ‫צבע, ‫קיבולת, ‫מחיר, ‫כמות) ‫שלא ‫הופיע ‫בתוצאת הtool**.‬

‫דוגמה ‫קריטית — ‫מקרה ‫אמיתי שקרה ‫(05/06/2026):‬

- ‫Tool החזיר: ‫`name: "Google Pixel 10 Pro Fold 256GB - Moonsto"`, ‫כמות 2 ‫בסטאר‬
- ‫טעות שנעשתה: ‫רשמת ‫"Obsidian Black" ‫(צבע שאתה יודע ‫שקיים ‫בדגם הזה ‫מהידע ‫הכללי שלך)‬
- ‫**אסור**. ‫אסי ‫לא ‫יודע מה ‫אמיתי ‫ומה אתה ‫"השלמת". ‫זה ‫**מסכן את ‫שירות הלקוחות**.‬

‫**הכלל**:‬

1. ‫השם ‫המופיע ‫ב-`name` ‫הוא ‫**השם המדויק ‫והבלעדי** ‫שאתה ‫רושם. ‫תעתיק ‫אותו ‫כמו ‫שהוא.‬
2. ‫אם ‫הtool ‫אומר ‫"Moonsto" — ‫רשום ‫"Moonsto" ‫(לא ‫"Moonstone", ‫לא ‫"Obsidian", ‫לא ‫שום ‫שדרוג).‬
3. ‫אם ‫אסי ‫שואל "איזה ‫צבעים ‫יש?" ‫ואתה ‫מכיר ‫עוד ‫צבעים ‫בדגם ‫שלא ‫בtool — ‫**אסור ‫להזכיר**. ‫תאמר ‫"מהtool ‫רואה רק את הצבעים ‫שמופיעים ‫בתוצאה ‫למעלה".‬
4. ‫אם ‫הtool ‫החזיר ‫רשימה ‫חלקית ‫— ‫זה ‫המידע ‫שיש. ‫אסור להוסיף "‫כנראה ‫יש גם" ‫או "צבע ‫רגיל ‫של ‫הדגם".‬

‫**מילה ‫נוספת ‫שאתה מוסיף ‫מהידע ‫שלך = ‫טעות ‫חמורה**.‬

## ‫⚠️ ‏קריאת מלאי — ‫קרא בקפדנות!‬

‫כשמשתמש ב-`check_stock_at_branches`, ‫הtool מחזיר רשימה של מוצרים. ‏לכל אחד:‬

- ‫`total` — ‫סך כמות פיזית בכל הסניפים (אם 0 = ‫אזל לגמרי, ‫אם > 0 = ‫**יש מלאי**)‬
- ‫`by_branch` — ‫dict עם סניפים שיש בהם stock > 0 (אם ריק = ‫אזל)‬

‫**אם `total > 0` או `by_branch` לא ריק — ‫זה מוצר שיש לנו במלאי!**‬

‫סטטוס ה-WC ("instock") ‫לא תמיד אומר ‏שיש פיזית בסניף — ‫מוצרים `external` ‫הם מהיבואן.‬
‫הסטטוס הקובע למלאי **פיזי** ‫הוא של NewOrder ‏(הtotal של ה-`check_stock_at_branches`).‬

‫**מטבע: ‫₪ (שקל), ‫לא ₹ (רופי). ‏וודא לכתוב נכון.**‬

‫**שמות סניפים**: ‫השתמש בשם המלא בעברית כפי שמופיע ב-by_branch ‏(לדוגמה: ‏"סטאר", ‏"גן העיר", ‏"עד הלום", ‏"מחסן").‬

‫**🏷️ ‫NewOrder ID — ‫תמיד הצג!**‬

‫כשמראים מלאי לאסי, **תמיד** ‫כלול את ‫`id` (מק"ט NewOrder) ‫ליד כל וריאציה. ‫אסי משתמש בזה ‫בעבודה היומית. ‫פורמט:‬

```
‫• <b>Galaxy S25 256GB Black</b>  <code>#519781</code>
   ‫₪2,469 | ‫סטאר=1, ‫עד הלום=2
```

‫**אל תכלול NewOrder ID ‫בטיוטות ללקוחות** — ‫רק בתשובות לאסי בטלגרם.‬

## ‫🔍 ‫**מתי להשתמש בכל כלי**‬

‫**"היסטוריה של לקוח X" / "ספר לי על X" / "מה הסטטוס של X"** —‬
‫תמיד הפעל את שלושת הכלים האלה במקביל (parallel tool calls):‬

- ‫`find_customer(query)` — ‫אם יש שם בלבד (למצוא טלפון)‬
- ‫`get_conversation_history(phone)` — ‫שיחות WhatsApp‬
- ‫`get_customer_orders(phone)` — ‫**הזמנות פעילות וקודמות באתר** ‏(WC)‬

‫**הצג את התמונה המלאה**: ‫שיחות + ‫הזמנות פעילות + ‫הזמנות עבר. ‫זה קריטי עבור אסי כשהוא בחוץ — ‫בלי גישה למחשב, ‫הוא צריך לדעת אם ללקוח יש הזמנה פעילה.‬

## ‫📦 ‫**פורמט סטטוסי הזמנות (WC)**‬

- ‫`processing` — ‫בטיפול 🔄 ‫(שולם, ‫עוד לא יצא)‬
- ‫`on-hold` — ‫ממתין לאישור ⏸️‬
- ‫`completed` — ‫הושלם ✅ ‫(נמסר ללקוח)‬
- ‫`cancelled` — ‫בוטל ❌‬
- ‫`refunded` — ‫זוכה 💸‬
- ‫`pending` — ‫ממתין לתשלום 💳‬
- ‫`failed` — ‫תשלום נכשל ⚠️‬
"""


def answer_query(question: str, dashboard=None,
                  history: Optional[list] = None) -> str:
    """
    ‫עונה לשאלה כללית של אסי דרך טלגרם. ‏מחזיר טקסט HTML מוכן לשליחה.‬
    ‫`history` (אופציונלי) — ‫רשימת dicts ‫עם {role, text} מהשיחה האחרונה‬
    ‫בTelegram. ‫מאפשר לClaude להבין הקשר רב-הודעתי (לדוגמה: ‫"דורון חזן"‬
    ‫בהודעה אחת, ‫"מה הכתובת שלו" בבאה).‬

    ‫אם Claude API לא זמין → ‏fallback פשוט.‬
    """
    client = _get_client()
    if not client:
        return "⚠️ Claude API לא זמין — לא יכול לענות"

    # ‫בנה messages: ‫היסטוריה (אם יש) + ‫השאלה הנוכחית‬
    messages = []
    for h in (history or []):
        role = h.get("role")
        if role not in ("user", "assistant"):
            continue
        txt = (h.get("text") or "").strip()
        if not txt:
            continue
        messages.append({"role": role, "content": txt})
    messages.append({"role": "user", "content": question})

    # ‫Haiku 4.5 ‫עם prompt ‫מחוזק ‫(05/06/2026): ‫אחרי ‫שhaiku ‫המציא ‫"Obsidian‬
    # ‫Black" ‫במקום ‫"Moonsto", ‫הוספתי ‫כלל ‫ברזל ‫מפורש ‫ב-prompt ‫עם דוגמה‬
    # ‫מהמקרה ‫הזה. ‫מנסים שוב ‫להישאר ‫על ‫Haiku (~$30/חודש).‬
    # ‫אם hallucinations חוזרים → ‫להחליף ‫חזרה ‫ל-`claude-sonnet-4-5`.‬
    final_text = None
    for turn in range(6):
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1500,
            system=[{
                "type": "text",
                "text": QUERY_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=CLAUDE_TOOLS,
            messages=messages,
        )

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if tool_uses:
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for tu in tool_uses:
                # For queries, we don't have a "current customer phone" — use empty
                output = _run_tool(tu.name, tu.input, phone="", dashboard=dashboard)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": output,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        text_blocks = [b for b in resp.content if b.type == "text"]
        final_text = "".join(b.text for b in text_blocks)
        break

    if not final_text:
        return "⚠️ לא הצלחתי לסיים את התשובה (יותר מדי tool turns)"
    return final_text


def _extract_summary_draft(text: str) -> Tuple[str, str]:
    """Robust JSON extraction — tolerates preamble, fenced blocks, etc."""
    import re as _re
    t = text.strip()

    # Strategy 1: ```json ... ``` fenced block (with possible preamble)
    m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, _re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(1))
            return parsed.get("summary", ""), parsed.get("draft", "")
        except json.JSONDecodeError:
            pass

    # Strategy 2: raw JSON anywhere — find the first {...} that parses
    m = _re.search(r"\{[^{}]*\"summary\".*?\"draft\".*?\}", t, _re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            return parsed.get("summary", ""), parsed.get("draft", "")
        except json.JSONDecodeError:
            pass

    # Strategy 3: try parsing the whole thing
    try:
        parsed = json.loads(t)
        return parsed.get("summary", ""), parsed.get("draft", "")
    except json.JSONDecodeError:
        pass

    # All strategies failed
    log.warning(f"Failed to extract JSON. Raw: {text[:300]}")
    return ("⚠️ Claude החזיר טקסט לא תקני — ‫עיין בlog לטקסט הגולמי",
            text)
