"""
checker.py — ‫הליבה של ה-stock-watcher.‬

‫רץ פעם ביום (cron) או on-demand דרך POST /run-check.‬
‫עבור כל רשומה ברשימה (status='watching'):‬
  ‫1. בדיקת stock ב-NewOrder לפי neworder_id‬
  ‫2. אם יש stock>0 בסניף כלשהו (חוץ מ"אתר" שמתעדכן עצלות) → שולח WhatsApp ומסמן notified‬
  ‫3. אחרת — מעדכן last_checked_at ומשאיר לבדיקה נוספת מחר‬
"""
from __future__ import annotations

import os
import logging
from typing import Optional

from shared.neworder_client import NewOrderClient
from shared.chatrace_dashboard_client import ChatRaceDashboardClient

from db import list_active_watches, mark_notified, update_last_checked
from notifier import notify_back_in_stock

log = logging.getLogger("stock_watcher.checker")

# ‫מפת שמות סניפים (מ-NewOrder API) — כדי שההודעה תכלול שם בעברית קריא.‬
# ‫מקור: nc.get_branches() — נדלק ב-init.‬
BRANCH_NAMES_CACHE: dict[int, str] = {}

# ‫סניפים שלא משלמים לנו לעדכן עליהם — בדרך כלל 'אתר' נספר כסניף נפרד אבל המלאי שלו עצלן.‬
EXCLUDE_BRANCH_IDS_FROM_NOTIFY = {5}  # 5 = "אתר" (online inventory)


def _load_branch_names(nc: NewOrderClient):
    """Cache the branch_id → branch_name map for nicer notifications."""
    global BRANCH_NAMES_CACHE
    if BRANCH_NAMES_CACHE:
        return
    try:
        branches = nc.get_branches() or []
        for b in branches:
            bid = int(b.get("branchId") or b.get("id") or 0)
            name = b.get("branchName") or b.get("name") or f"סניף {bid}"
            if bid:
                BRANCH_NAMES_CACHE[bid] = name
        log.info(f"Loaded {len(BRANCH_NAMES_CACHE)} branches")
    except Exception as e:
        log.warning(f"Failed to load branches: {e}")


def _find_first_branch_with_stock(stock_map: dict[int, float]) -> Optional[tuple[int, float]]:
    """
    Given {branch_id: qty}, return the (branch_id, qty) of the first physical
    branch that has stock > 0. Excludes EXCLUDE_BRANCH_IDS_FROM_NOTIFY.
    """
    for bid, qty in stock_map.items():
        try:
            bid_int = int(bid)
        except Exception:
            continue
        if bid_int in EXCLUDE_BRANCH_IDS_FROM_NOTIFY:
            continue
        if qty and qty > 0:
            return bid_int, qty
    return None


def run_check(dry_run: bool = False) -> dict:
    """
    Main entry — runs through all active watches, notifies on any that have
    stock available now. Returns a dict summary suitable for the API/log.
    """
    nc = NewOrderClient.from_env()
    dc = ChatRaceDashboardClient.from_env()
    _load_branch_names(nc)

    items = list_active_watches()
    log.info(f"Found {len(items)} active watches")

    summary = {
        "total":      len(items),
        "notified":   0,
        "still_out":  0,
        "errors":     0,
        "details":    [],
    }

    for item in items:
        try:
            stock_map = nc.get_product_stock(item.neworder_id) or {}
            found = _find_first_branch_with_stock(stock_map)
            if found:
                bid, qty = found
                branch_name = BRANCH_NAMES_CACHE.get(bid, f"סניף {bid}")
                log.info(
                    f"  ✅ stock found for {item.product_name!r} at {branch_name} "
                    f"(qty={qty}) → notifying {item.customer_phone}"
                )
                if dry_run:
                    summary["notified"] += 1
                    summary["details"].append({
                        "id":            item.id,
                        "phone":         item.customer_phone,
                        "name":          item.customer_name,
                        "product":       item.product_name,
                        "branch":        branch_name,
                        "qty":           qty,
                        "would_notify":  True,
                    })
                    continue
                # Real send
                ok = notify_back_in_stock(
                    customer_phone=item.customer_phone,
                    customer_name=item.customer_name,
                    product_name=item.product_name,
                    branch_name=branch_name,
                    product_url=item.product_url,
                    dashboard=dc,
                )
                if ok:
                    mark_notified(item.id, branch_name)
                    summary["notified"] += 1
                    summary["details"].append({
                        "id":      item.id,
                        "phone":   item.customer_phone,
                        "product": item.product_name,
                        "branch":  branch_name,
                        "qty":     qty,
                        "sent":    True,
                    })
                else:
                    log.error(f"  ❌ notification failed for item #{item.id}")
                    summary["errors"] += 1
                    summary["details"].append({
                        "id":      item.id,
                        "phone":   item.customer_phone,
                        "product": item.product_name,
                        "sent":    False,
                    })
            else:
                update_last_checked(item.id)
                summary["still_out"] += 1
        except Exception as e:
            log.exception(f"Check failed for item #{item.id}: {e}")
            summary["errors"] += 1

    log.info(
        f"Run done — notified={summary['notified']} / "
        f"still_out={summary['still_out']} / errors={summary['errors']}"
    )
    return summary
