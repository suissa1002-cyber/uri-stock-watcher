# Uri Stock Watcher — Deployment Info

‫**תאריך פריסה**: 04/06/2026‬

---

## ‫שירותים וכתובות‬

### Render Web Service
- **‫שם**: `uri-stock-watcher`‏‬
- **URL**: https://uri-stock-watcher.onrender.com
- **Service ID**: `srv-d8go46i8qa3s739itd70`
- **Dashboard**: https://dashboard.render.com/web/srv-d8go46i8qa3s739itd70
- **Region**: Frankfurt (EU)
- **Plan**: Free
- **Runtime**: Docker
- **Auto-Deploy**: ON (push ל-main → build אוטומטי)
- **Health Check Path**: `/health`

### Render PostgreSQL (free)
- **‫שם**: `uri-stock-watcher-db`‏‬
- **DB ID**: `dpg-d8go6eflk1mc73f44i30-a`
- **Dashboard**: https://dashboard.render.com/d/dpg-d8go6eflk1mc73f44i30-a
- **Region**: Frankfurt (EU)
- **Plan**: Free
- **Version**: PostgreSQL 16
- **⚠️ ‫פג תוקף**: 04/07/2026 — אחרי 30 ‏יום צריך לשדרג ל-Starter ($7/חודש) או למגרציה ל-Neon.‏
- **Connection**: ‫השירות משתמש ב-internal connection string (אותו VPC, ללא SSL חיצוני).‬

### GitHub Repo
- **URL**: https://github.com/suissa1002-cyber/uri-stock-watcher
- **Visibility**: Public (‫מותר — אין סודות בקוד)‬
- **Branch**: main
- **Owner**: suissa1002-cyber

### GitHub Actions (keep-alive)
- **‫קובץ**: `.github/workflows/keep-alive.yml`‏‬
- **‫תדירות**: כל 10 ‏דקות‬
- **‫מטרה**: ‏מונע מ-Render Free להירדם (sleeping אחרי 15 ‏דקות חוסר פעילות)‬
- **‫Secret**: `SERVICE_URL` ‎= `https://uri-stock-watcher.onrender.com`‏‬

---

## Environment Variables (‫ב-Render)‬

| Key | ‫מקור / ‏הערה‬ |
|---|---|
| `TZ` | `Asia/Jerusalem` |
| `CRON_HOUR` | `9` |
| `CRON_DAYS` | `0-4` (Sun-Thu) |
| `PORT` | `8000` (Render injects this) |
| `STOCK_WATCHER_TOKEN` | ‫random 32-char (ב-`.deploy_state.json`)‬ |
| `DATABASE_URL` | ‫Internal Postgres URL (mn Render API)‬ |
| `NEWORDER_API_TOKEN` | ‫כמו רון‬ |
| `NEWORDER_BASE_URL` | `https://api.neworder.co.il` |
| `CHATRACE_DASHBOARD_TOKEN` | ‫⚠️ ‏פג כל 10 ‏ימים — ‏צריך לסנכרן ידנית!‬ |
| `CHATRACE_DASHBOARD_ACCOUNT_ID` | `1428408` |

---

## ‫API endpoints (‏עם Bearer auth)‬

```bash
# ‫הוסף לקוח‬
curl -X POST https://uri-stock-watcher.onrender.com/watch \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"customer_phone":"...","customer_name":"...","neworder_id":...,"product_name":"...","product_url":"...","notes":"..."}'

# ‫רשימה‬
curl https://uri-stock-watcher.onrender.com/watches -H "Authorization: Bearer $TOKEN"

# ‫בטל מעקב‬
curl -X DELETE https://uri-stock-watcher.onrender.com/watches/{id} -H "Authorization: Bearer $TOKEN"

# ‫הרץ בדיקה ידנית‬
curl -X POST https://uri-stock-watcher.onrender.com/run-check \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}'

# ‫dry run (לא שולח באמת)‬
curl -X POST 'https://uri-stock-watcher.onrender.com/run-check?dry_run=true' \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}'

# ‫בריאות (UptimeRobot / GitHub Actions)‬
curl https://uri-stock-watcher.onrender.com/health
```

---

## ‫רכיב חי בפרודקציה‬

‫**ינון אטיה** (id=1): ממתין ל-OPPO Find X9 Ultra Umber 512GB‬
- ‫טלפון: ‏972522514332‬
- ‫NewOrder id: ‏520395‬
- ‫מחיר: ‏6,199 ₪‬
- ‫הצפי: ‏המלאי יחזור שבוע הבא (לפי הספק)‬

---

## ‫⚠️ ‫TODO לעתיד‬

1. **‫אוטומציה של סנכרון `CHATRACE_DASHBOARD_TOKEN`**: ‏הסקריפט `sync_dashboard_token.py` ‎מעדכן את ה-Worker של Cloudflare. ‏צריך להרחיב שגם ידחוף ל-Render API:‬
   ```python
   requests.put(f"https://api.render.com/v1/services/{SVC_ID}/env-vars/CHATRACE_DASHBOARD_TOKEN",
                headers={"Authorization": f"Bearer {RENDER_TOKEN}"}, json={"value": new_token})
   ```
2. **‫הגירה ל-Neon Postgres**: ‏לפני 04/07/2026 — ‏או לשלם על Render Starter, ‏או למגרצה ל-Neon free (אותו פרויקט שמשמש את איציק).‬
3. **‫template ייעודי `stock_back_in_stock`**: ‏לאשר ב-Meta. ‏כרגע משתמשים ב-`new_message` הגנרי.‬
4. **‫אינטגרציה עם אורי**: ‏פונקציה `Uri.add_to_stock_watch()` ‎שתשלח POST /watch אוטומטית כשלקוח מבקש להישמר.‬
5. **‫שנה את הרפו לפרטי**: ‏לאחר שתבדוק שהכל עובד, ‏אם תרצה — ‏אפשר לשנות חזרה לפרטי (אבל אז Render צריך להוסיף אותו ל-GitHub App permissions ב-installation page).‬
