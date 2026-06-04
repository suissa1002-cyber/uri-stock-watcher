# Uri Stock Watcher

‫שירות שמחזיק רשימת לקוחות שמחכים שמוצר יחזור למלאי, ובכל יום בודק את NewOrder ושולח WhatsApp ברגע שהמוצר חזר.‬

---

## ‫למה זה קיים‬

‫כשלקוח שואל "תעדכן אותי כש-X יחזור למלאי" — בעבר רשמנו לעצמנו תזכורת ב-Claude Code, אבל היא לא רצה אם Claude סגור. השירות הזה רץ 24/7 על Render ולא תלוי בשום סוכן אחר.‬

---

## ‫ארכיטקטורה‬

```
┌─────────────────────────────────────────────┐
│                Render (web)                  │
│  ┌─────────────────────────────────────┐    │
│  │  FastAPI:                            │    │
│  │   POST /watch       — add entry      │    │
│  │   GET  /watches     — list           │    │
│  │   DELETE /watches/:id                │    │
│  │   POST /run-check   — manual run     │    │
│  │   GET  /health      — UptimeRobot    │    │
│  └─────────────────────────────────────┘    │
│  ┌─────────────────────────────────────┐    │
│  │  APScheduler:                         │    │
│  │   • Daily 09:00 Sun-Thu (Asia/Jerusalem) │
│  │   • Iterates watch list, checks NewOrder │
│  │   • Sends WhatsApp via ChatRace template │
│  └─────────────────────────────────────┘    │
└─────────────────────────────────────────────┘
              ↓                ↓
        NewOrder POS    ConnectOp dashboard
        (read stock)    (send WhatsApp template)
              ↑
        UptimeRobot pings /health every 5 min
        (Render Free sleeps after 15 min idle)
```

---

## ‫מבנה הקבצים‬

```
stock_watcher/
├── main.py                # FastAPI + APScheduler + startup
├── db.py                  # SQLAlchemy models + helpers
├── checker.py             # core loop: check NewOrder per item, notify if found
├── notifier.py            # WhatsApp send via ChatRace template
├── shared/                # local copies of agents/shared/* (Docker-friendly)
│   ├── neworder_client.py
│   ├── connectop_client.py
│   └── chatrace_dashboard_client.py
├── requirements.txt
├── Dockerfile
├── Procfile               # `web: python3 main.py`
├── render.yaml            # Render blueprint
├── .env.example
└── README.md
```

---

## ‫API‬

‫כל הendpoints חוץ מ-`/health` ו-`/` דורשים header:‬

```
Authorization: Bearer <STOCK_WATCHER_TOKEN>
```

### `POST /watch`

‫מוסיף לקוח לרשימה. ‏בקשה לדוגמה:‬

```json
{
  "customer_phone":  "972522514332",
  "customer_name":   "ינון אטיה",
  "neworder_id":     520395,
  "product_name":    "OPPO Find X9 Ultra 512GB Tundra Umber",
  "product_url":     "https://tinyurl.com/gm-oppo-x9ultra-umber",
  "notes":           "‫שחור 512GB, חזר שבוע הבא לפי דבריך"
}
```

‫`neworder_id` הוא ה-`id` של המוצר ב-NewOrder (לא ה-SKU/ברקוד). ‏השג אותו על ידי:‬

```python
from shared.neworder_client import NewOrderClient
nc = NewOrderClient.from_env()
products = nc.get_products(search="Find X9 Ultra")
for p in products: print(p['id'], p['name'])
```

### `GET /watches?limit=200`

‫מחזיר את כל הרשומות (כולל notified ו-cancelled), חדשים ראשונים.‬

### `DELETE /watches/{id}`

‫מסמן רשומה כ-cancelled (לא מוחק — היסטוריה נשמרת).‬

### `POST /run-check?dry_run=true`

‫מריץ בדיקה עכשיו, בלי לחכות ל-09:00. ‏`dry_run=true` יראה מה היה נשלח, בלי לשלוח באמת.‬

### `GET /health`

‫מחזיר `{"status":"ok"}`. ‏UptimeRobot יודע לפי זה שהשירות חי.‬

---

## ‫פריסה ל-Render (צעד-צעד)‬

### 1. ‫יצירת repo נפרד ב-GitHub‬

```bash
cd "/Users/asisuissa/Desktop/workspace /green-woo/agents/uri/stock_watcher"
git init
git add .
git commit -m "Initial commit"
gh repo create uri-stock-watcher --private --source=. --remote=origin --push
```

### 2. ‫הקמת Neon Postgres‬

- ‫כנס ל-https://console.neon.tech‬
- ‫Create project: ‏`uri-stock-watcher`‏ (region: AWS eu-central-1 Frankfurt)‬
- ‫בלשונית Connection details — העתק את connection string (postgres://...)‬

### 3. ‫יצירת Web Service ב-Render‬

- ‫https://dashboard.render.com/select-repo‬
- ‫בחר את ‎`uri-stock-watcher`‏‬
- ‫Settings:‬
  - **Name**: `uri-stock-watcher`
  - **Region**: Frankfurt (EU)
  - **Branch**: main
  - **Runtime**: Docker
  - **Plan**: Free
  - **Auto-Deploy**: On
  - **Health Check Path**: `/health`

### 4. ‫הגדרת Environment Variables‬

‫ב-Render Dashboard → ‏השירות → ‏Environment → ‏הוסף:‬

| Key | Value |
|---|---|
| `STOCK_WATCHER_TOKEN` | ‫מחרוזת אקראית של 32 ‏תווים (לאוטנטיקציה ב-/watch)‬ |
| `DATABASE_URL` | ‫connection string מ-Neon (postgres://...)‬ |
| `NEWORDER_API_TOKEN` | ‫אותו טוקן שמשמש את רון‬ |
| `NEWORDER_BASE_URL` | ‫`https://api.neworder.co.il`‏‬ |
| `CHATRACE_DASHBOARD_TOKEN` | ‫אותו טוקן שמשמש את אורי (יפוג כל ~10 ‏ימים — הסקריפט `sync_dashboard_token.py` מטפל בעדכון .env המקומי. ‏יש לסנכרן ידנית עם Render כשיתעדכן, או להוסיף webhook)‬ |
| `CHATRACE_DASHBOARD_ACCOUNT_ID` | ‫`1428408`‏‬ |
| `TZ` | ‫`Asia/Jerusalem`‏‬ |

### 5. ‫הגדרת UptimeRobot‬

- ‫https://uptimerobot.com → ‏Add monitor‬
- ‫סוג: ‏HTTP(s)‬
- ‫URL: ‏`https://uri-stock-watcher-XXXX.onrender.com/health`‏ (השם מתקבל מ-Render)‬
- ‫Interval: ‏5 ‏דקות‬

### 6. ‫בדיקה: ‏הוסף את ינון ידנית‬

```bash
curl -X POST https://uri-stock-watcher-XXXX.onrender.com/watch \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_phone": "972522514332",
    "customer_name":  "ינון אטיה",
    "neworder_id":    520395,
    "product_name":   "OPPO Find X9 Ultra 512GB Tundra Umber",
    "product_url":    "https://greenmobile.co.il/product/oppo-find-x9-ultra/",
    "notes":          "‫שחור 512GB, חזרה שבוע הבא לפי הספק"
  }'
```

‫ואז:‬

```bash
curl https://uri-stock-watcher-XXXX.onrender.com/watches \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## ‫פיתוח מקומי‬

```bash
cd agents/uri/stock_watcher
cp .env.example .env   # מלא את הערכים
pip install -r requirements.txt
python3 main.py
```

‫השירות יעלה על ‏`http://localhost:8000`.‏ ‫אפשר להריץ בדיקה ידנית:‬

```bash
curl -X POST 'http://localhost:8000/run-check?dry_run=true' \
  -H "Authorization: Bearer your-local-token"
```

---

## ‫אינטגרציה עם אורי (העתיד)‬

‫כשאסי מטפל בשיחת WhatsApp עם לקוח שמבקש להתעדכן כשמוצר יחזור — אורי יוסיף POST /watch אוטומטית. ‏בהמשך:‬

```python
# In agents/uri/uri.py
def add_to_stock_watch(self, phone, name, neworder_id, product_name, url=""):
    requests.post(
        f"{os.environ['STOCK_WATCHER_URL']}/watch",
        headers={"Authorization": f"Bearer {os.environ['STOCK_WATCHER_TOKEN']}"},
        json={
            "customer_phone": phone,
            "customer_name":  name,
            "neworder_id":    neworder_id,
            "product_name":   product_name,
            "product_url":    url,
        },
        timeout=10,
    )
```

---

## ‫מגבלות ידועות‬

1. ‫**WhatsApp 24h window**: ‏השירות שולח דרך template ‎`new_message` ‎— ‏זה עוקף את חלון 24 ‏השעות כי הtemplate מאושר ע"י Meta. ‏בעתיד אם נצור template ייעודי `stock_back_in_stock` — נחליף ב-`notifier.py` ‎דרך משתנה `STOCK_TEMPLATE_NAME`.‬
2. ‫**CHATRACE_DASHBOARD_TOKEN פג כל 10 ‏ימים**: ‏על Render אין אוטומציה לסנכרון. ‏כשמתעדכן ב-.env המקומי, צריך גם להעדכן ידנית ב-Render UI. ‏פתרון עתידי: ‏webhook מ-`sync_dashboard_token.py` ‎שגם דוחף ל-Render API.‬
3. ‫**Render Free שנייה ע"י inactivity**: ‏UptimeRobot כל 5 ‏דקות מונע זאת. ‏עדיין יכול להירדם בלילה — לא משנה כי הcron הראשון של היום הוא 09:00.‬
4. ‫**SQLite mode**: ‏אם אין `DATABASE_URL`, ‏מבסיס נתונים מקומי על `/data/`. ‏ב-Render Free אין persistent disk חינמי — אז במצב הזה נתונים יאבדו בכל פריסה. ‏לכן Postgres מומלץ.‬
