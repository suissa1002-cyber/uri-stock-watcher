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
        "description": "‫מושך עד 20 ההודעות האחרונות של השיחה הנוכחית עם הלקוח. ‫שימושי להבין הקשר ושיחות עבר.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ─────────────────────────────────────────────────────────────────────
# Tool implementations — used when Claude makes a tool call
# ─────────────────────────────────────────────────────────────────────

def _tool_search_product(query: str) -> str:
    """WC product search."""
    import requests
    WC = os.environ['WC_STORE_URL'].rstrip('/')
    WC_AUTH = (os.environ['WC_CONSUMER_KEY'], os.environ['WC_CONSUMER_SECRET'])
    r = requests.get(f"{WC}/wp-json/wc/v3/products",
                     params={"search": query, "per_page": 8},
                     auth=WC_AUTH, timeout=20,
                     headers={"User-Agent":"Mozilla/5.0"})
    items = r.json() if r.status_code == 200 else []
    out = []
    for p in items[:5]:
        out.append({
            "id":         p.get("id"),
            "name":       p.get("name","")[:100],
            "price":      p.get("price"),
            "stock":      p.get("stock_status"),
            "type":       p.get("type"),
            "permalink":  p.get("permalink"),
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
    msgs = dashboard.get_conversation(phone, limit=20)
    msgs_sorted = sorted(msgs, key=lambda m: int(m.get("ts") or 0))
    out = []
    for m in msgs_sorted[-20:]:
        out.append({
            "direction": m.get("direction"),
            "text":      (m.get("text") or "")[:200],
            "ts":        m.get("ts"),
        })
    return json.dumps(out, ensure_ascii=False)


def _run_tool(name: str, args: dict, phone: str, dashboard) -> str:
    try:
        if name == "search_product":
            return _tool_search_product(args.get("query",""))
        if name == "check_stock_at_branches":
            return _tool_check_stock(args.get("product_name",""))
        if name == "get_conversation_history":
            return _tool_get_history(phone, dashboard)
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
    for turn in range(6):
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
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
‫אתה אורי, ‏עוזר AI של אסי, ‏הבעלים של Green Mobile. ‏הוא שאל אותך שאלה בטלגרם.‬

‫אסי הוא בעל החנות — ‫עונה לו ישירות, בלי פתיחים שיווקיים. ‏השתמש בכלים‬
‫(חיפוש מוצר, בדיקת מלאי, היסטוריית שיחה) כדי לאסוף נתונים אמיתיים.‬

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

## ‫⚠️ ‏קריאת מלאי — ‫קרא בקפדנות!‬

‫כשמשתמש ב-`check_stock_at_branches`, ‫הtool מחזיר רשימה של מוצרים. ‏לכל אחד:‬

- ‫`total` — ‫סך כמות פיזית בכל הסניפים (אם 0 = ‫אזל לגמרי, ‫אם > 0 = ‫**יש מלאי**)‬
- ‫`by_branch` — ‫dict עם סניפים שיש בהם stock > 0 (אם ריק = ‫אזל)‬

‫**אם `total > 0` או `by_branch` לא ריק — ‫זה מוצר שיש לנו במלאי!**‬

‫סטטוס ה-WC ("instock") ‫לא תמיד אומר ‏שיש פיזית בסניף — ‫מוצרים `external` ‫הם מהיבואן.‬
‫הסטטוס הקובע למלאי **פיזי** ‫הוא של NewOrder ‏(הtotal של ה-`check_stock_at_branches`).‬

‫**מטבע: ‫₪ (שקל), ‫לא ₹ (רופי). ‏וודא לכתוב נכון.**‬

‫**שמות סניפים**: ‫השתמש בשם המלא בעברית כפי שמופיע ב-by_branch ‏(לדוגמה: ‏"סטאר", ‏"גן העיר", ‏"עד הלום", ‏"מחסן").‬
"""


def answer_query(question: str, dashboard=None) -> str:
    """
    ‫עונה לשאלה כללית של אסי דרך טלגרם. ‏מחזיר טקסט HTML מוכן לשליחה.‬
    ‫אם Claude API לא זמין → ‏fallback פשוט.‬
    """
    client = _get_client()
    if not client:
        return "⚠️ Claude API לא זמין — לא יכול לענות"

    messages = [{"role": "user", "content": question}]

    final_text = None
    for turn in range(8):
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=QUERY_SYSTEM_PROMPT,
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
