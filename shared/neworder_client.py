"""
NewOrder POS API Client
=======================
חיבור ל-NewOrder — מערכת הקופה (POS) של Green Mobile בסניפים ובאתר.
Client לקריאה בלבד (כל ה-API הוא GET נכון ל-06/2026).

API base: https://neworderapi.azurewebsites.net
Docs:     https://neworderapidocs.azurewebsites.net/
Auth:     Authorization: Bearer <token>

⚠️ הערות חשובות:
  • הטוקן בתוקף ל-3 חודשים בלבד (פג 03/09/2026) — לבקש חדש לפני כן.
  • Rate limit: 100 קריאות לדקה. ה-client מאט אוטומטית כדי לא לחרוג.
  • זו גרסת BETA — ייתכנו באגים/שינויים. תמיד לאמת תקינות מידע.
  • המידע רגיש — לא לחשוף את הטוקן.

Usage:
    from neworder_client import NewOrderClient
    no = NewOrderClient.from_env()
    branches = no.get_branches()
    products = no.get_products(branch_id=5, search="JBL")     # מלאי לפי סניף
    stock    = no.get_product_stock("516308")                  # מלאי חי למוצר בכל הסניפים
    moves    = no.get_stock_operations(from_date="2026-06-01") # תנועות מלאי / העברות בין סניפים
"""
from __future__ import annotations

import os
import time
import logging
import threading
from collections import deque
from typing import Any, Optional, Union

import requests

log = logging.getLogger("neworder")

DEFAULT_BASE_URL = "https://neworderapi.azurewebsites.net"
RATE_LIMIT_PER_MIN = 100


class NewOrderError(Exception):
    """Raised on non-2xx responses or transport errors from the NewOrder API."""
    pass


def _fmt_date(value: Any) -> Optional[str]:
    """
    Normalize a date to NewOrder's expected DD/MM/YYYY format.
    Accepts: None (->None), date/datetime, 'YYYY-MM-DD', 'DD/MM/YYYY',
    or ISO 'YYYY-MM-DDTHH:MM:SS'. Anything unrecognized is returned as-is.
    NewOrder's stock-operations/documents date filters require DD/MM/YYYY;
    mixing formats (e.g. a YYYY-MM-DD toDate) triggers HTTP 500.
    """
    if value is None:
        return None
    from datetime import date, datetime
    if isinstance(value, (date, datetime)):
        return value.strftime("%d/%m/%Y")
    s = str(value).strip()
    if not s:
        return None
    if "/" in s:  # already DD/MM/YYYY (or close enough) — leave it
        return s
    iso = s.split("T", 1)[0]  # drop any time component
    try:
        y, m, d = iso.split("-")
        return f"{int(d):02d}/{int(m):02d}/{int(y):04d}"
    except (ValueError, AttributeError):
        return s


class _RateLimiter:
    """Sliding-window limiter: at most `max_calls` within `period` seconds.

    NewOrder caps us at 100 req/min. We stay one under (99) for safety margin
    against clock skew between our window and theirs.
    """

    def __init__(self, max_calls: int = RATE_LIMIT_PER_MIN - 1, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self.period:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                sleep_for = self.period - (now - self._calls[0])
                if sleep_for > 0:
                    log.warning("NewOrder rate limit reached — sleeping %.1fs", sleep_for)
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


class NewOrderClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL,
                 store_guid: str = "", timeout: int = 30):
        if not token:
            raise ValueError("NewOrder token is required (NEWORDER_API_TOKEN)")
        self.token = token
        self.base = base_url.rstrip("/")
        self.store_guid = store_guid
        self.timeout = timeout
        self._limiter = _RateLimiter()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })

    @classmethod
    def from_env(cls) -> "NewOrderClient":
        """Build from NEWORDER_API_TOKEN / NEWORDER_BASE_URL / NEWORDER_STORE_GUID."""
        token = os.environ.get("NEWORDER_API_TOKEN", "")
        base = os.environ.get("NEWORDER_BASE_URL", DEFAULT_BASE_URL)
        guid = os.environ.get("NEWORDER_STORE_GUID", "")
        return cls(token=token, base_url=base, store_guid=guid)

    # ── Low-level HTTP ────────────────────────────────────────────────
    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        self._limiter.acquire()
        url = f"{self.base}{path}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            r = self.session.get(url, params=clean, timeout=self.timeout)
        except requests.RequestException as e:
            raise NewOrderError(f"transport error on GET {path}: {e}") from e

        if r.status_code == 401:
            raise NewOrderError(
                "401 Unauthorized — NewOrder token invalid or expired "
                "(tokens last 3 months; request a new one)."
            )
        if r.status_code == 429:
            raise NewOrderError("429 Too Many Requests — exceeded 100 req/min.")
        if r.status_code >= 400:
            raise NewOrderError(f"HTTP {r.status_code} on GET {path}: {r.text[:300]}")

        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as e:
            raise NewOrderError(f"non-JSON response on GET {path}: {r.text[:300]}") from e

    def _get_all_pages(self, path: str, params: Optional[dict] = None,
                       page_size: int = 200, max_pages: int = 100) -> list[dict]:
        """Iterate page_num until a short/empty page. Beta API exposes no total count."""
        params = dict(params or {})
        out: list[dict] = []
        for page_num in range(1, max_pages + 1):
            params.update(page_size=page_size, page_num=page_num)
            batch = self._get(path, params)
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < page_size:
                break
        return out

    # ── Business / branches ───────────────────────────────────────────
    def get_branches(self) -> list[dict]:
        """רשימת סניפים: [{companyName, branchId, branchName, taxId, address, phoneNumber}]"""
        return self._get("/api/Bussiness/branches")

    # ── Products ──────────────────────────────────────────────────────
    def get_products(self, branch_id: Optional[int] = None, search: Optional[str] = None,
                     category: Optional[Union[int, str]] = None, stock_mode: Optional[str] = None,
                     serials_only: Optional[bool] = None, show_not_used: Optional[bool] = None,
                     page_size: int = 200, page_num: int = 1) -> list[dict]:
        """
        שליפת מוצרים. `branch_id` קובע ש-`currentStock` יחזיר את המלאי של אותו סניף.
        כל מוצר: {id, name, barcode, cost, price, isSerial, category{id,name},
                  isStock, currentStock, isActive, supplier{...}, additionalBarcodes[]}
        """
        return self._get("/api/Products", {
            "branchId": branch_id, "searchBy": search, "category": category,
            "stockMode": stock_mode, "serialsOnly": serials_only,
            "showNotUsed": show_not_used, "page_size": page_size, "page_num": page_num,
        })

    def get_all_products(self, branch_id: Optional[int] = None,
                         category: Optional[Union[int, str]] = None) -> list[dict]:
        """כל המוצרים (כל הדפים). שימושי לסנכרון קטלוג/מלאי מלא לאתר."""
        return self._get_all_pages("/api/Products",
                                   {"branchId": branch_id, "category": category})

    def get_product(self, product_id: Union[str, int], branch_id: Optional[int] = None) -> dict:
        """
        פרטי מוצר בודד לפי id.
        ⚠️ באג בטא: ה-endpoint הזה מתעלם מ-branchId ו-`currentStock` שלו הוא תמיד
        הסך הכולל בכל הסניפים. למלאי לפי סניף יש להשתמש ב-get_products(branch_id=...)
        או ב-get_product_stock().
        """
        return self._get(f"/api/Products/{product_id}", {"branchId": branch_id})

    def get_categories(self) -> list[dict]:
        """רשימת קטגוריות: [{id, name}]"""
        return self._get("/api/Products/categories")

    def get_suppliers(self) -> list[dict]:
        """רשימת ספקים."""
        return self._get("/api/Products/suppliers", {"showNotUsed": False})

    def get_product_serials(self, product_id: Union[str, int],
                            branch_id: Optional[int] = None) -> list[dict]:
        """מספרים סריאליים עבור מוצר ספציפי."""
        return self._get(f"/api/Products/{product_id}/serials", {"branchId": branch_id})

    def get_stock_operations(self, branch_id: Optional[int] = None,
                             from_date: Optional[str] = None, to_date: Optional[str] = None,
                             page_size: int = 200, page_num: int = 1) -> list[dict]:
        """
        היסטוריית תנועות מלאי. כולל העברות בין סניפים (opTypeName="העברה בין סניפים").
        כל תנועה: {id, createDate, operationType, opTypeName, documentNumber,
        employee, totalQuantity, stockItems[]}.
        ✅ סינון תאריכים עובד (תוקן ע"י NewOrder 06/2026). הפורמט הנדרש הוא DD/MM/YYYY;
        אפשר להעביר גם YYYY-MM-DD / datetime ו-`_fmt_date` ימיר אוטומטית.
        """
        return self._get("/api/Products/stock-operations", {
            "branchId": branch_id,
            "fromDate": _fmt_date(from_date), "toDate": _fmt_date(to_date),
            "page_size": page_size, "page_num": page_num,
        })

    # ── Live stock helpers (used by Uri / customer service) ───────────
    def get_product_stock(self, product_id: Union[str, int]) -> dict[int, float]:
        """
        מלאי חי למוצר בכל הסניפים: {branchId: quantity}.
        ✅ עובד דרך endpoint ייעודי `/api/Products/{id}/stock` (נוסף ע"י NewOrder 06/2026)
        שמחזיר `[{branchId, quantity}]` בקריאה אחת — מחליף את הלולאה הישנה על כל סניף
        (ואת העקיפה לבאג branchId ב-/api/Products/{id}).
        ⚠️ ערכים יכולים להיות שליליים (oversold) — זה מצב אמיתי בקופה, לא שגיאה.
        """
        pid = str(product_id)
        rows = self._get(f"/api/Products/{pid}/stock") or []
        return {int(r["branchId"]): r.get("quantity") for r in rows}

    def find_product_by_barcode(self, barcode: str,
                                branch_id: Optional[int] = None) -> Optional[dict]:
        """חיפוש מוצר לפי ברקוד (כולל additionalBarcodes). מחזיר את המוצר או None."""
        for p in self.get_products(search=barcode, branch_id=branch_id, page_size=50):
            if p.get("barcode") == barcode or barcode in (p.get("additionalBarcodes") or []):
                return p
        return None

    # ── Customers ─────────────────────────────────────────────────────
    def get_customers(self, search: Optional[str] = None, balance_mode: Optional[str] = None,
                      page_size: int = 200, page_num: int = 1) -> list[dict]:
        """שליפת לקוחות עם יתרות וסינונים."""
        return self._get("/api/Customers", {
            "searchBy": search, "balanceMode": balance_mode,
            "page_size": page_size, "page_num": page_num,
        })

    def get_customer(self, customer_id: Union[str, int]) -> dict:
        """פרטי לקוח מלאים לפי id."""
        return self._get(f"/api/Customers/{customer_id}")

    # ── Documents / invoices ──────────────────────────────────────────
    def get_documents(self, branch_id: Optional[int] = None, from_date: Optional[str] = None,
                      to_date: Optional[str] = None, doc_type: Optional[str] = None,
                      search: Optional[str] = None, site_only: Optional[bool] = None,
                      page_size: int = 200, page_num: int = 1) -> list[dict]:
        """שליפת מסמכים/חשבוניות. `site_only=True` למסמכי האתר בלבד."""
        return self._get("/api/Documents", {
            "branchId": branch_id, "fromDate": from_date, "toDate": to_date,
            "docType": doc_type, "searchBy": search, "siteOnly": site_only,
            "page_size": page_size, "page_num": page_num,
        })

    def get_document(self, invoice_id: Union[str, int]) -> dict:
        """פרטי מסמך בודד לפי מזהה חשבונית."""
        return self._get(f"/api/Documents/{invoice_id}")

    # ── Meta ──────────────────────────────────────────────────────────
    def get_schema(self) -> Any:
        """סכמת מטא-דאטה של ה-API."""
        return self._get("/api/meta/schema")
