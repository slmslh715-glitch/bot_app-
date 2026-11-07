"""طبقة قاعدة البيانات.

- إذا ضُبط DATABASE_URL (Postgres) تُستعمل قاعدة دائمة عبر psycopg2.
- وإلا يُستعمل SQLite محلي (يُمحى مع كل نشر على Render المجانية، للتجربة فقط).
- التواريخ تُخزَّن نصوصاً ISO بتوقيت UTC، والقيم المنطقية أعداداً 0/1، ليعمل الكود نفسه على القاعدتين.
- أسرار المشتركين (توكن واتساب ومفتاح Gemini) تُخزَّن مشفّرة.
"""
import os
import re
import json
import time
import base64
import hashlib
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("bot.db")

backend = "sqlite"
custom_encryption_key = False
_fernet = None
_sqlite = None
_sqlite_lock = threading.RLock()
_pool = None
_last_used = {}
psycopg2 = None

# ---------------------------------------------------------------------------
# الميزات والباقات الافتراضية
# ---------------------------------------------------------------------------
# enforced=True: يطبّقها الكود فعلاً. enforced=False: تُخزَّن كعلَم فقط حتى تُبنى الميزة نفسها.
FEATURE_CATALOG = [
    {"key": "memory", "label": "ذاكرة المحادثة",
     "hint": "يتذكر البوت ما سأله الزبون سابقاً في المحادثة", "enforced": True},
    {"key": "save_chat", "label": "حفظ المحادثات",
     "hint": "تُحفظ محادثات الزبائن ويمكن الاطلاع عليها من اللوحة", "enforced": True},
    {"key": "pdf_rag", "label": "قراءة كتالوج PDF",
     "hint": "يجيب البوت من ملف الكتالوج (يتطلب تفعيل ENABLE_PDF_RAG)", "enforced": True},
    {"key": "human_handoff", "label": "تحويل الزبون إلى إنسان",
     "hint": "ينبّه صاحب المتجر عندما يطلب الزبون التحدث إلى شخص", "enforced": False},
    {"key": "order_log", "label": "تسجيل الطلبات",
     "hint": "تُرتَّب الطلبات في ملف يمكن تصديره", "enforced": False},
    {"key": "owner_notify", "label": "إشعار صاحب المتجر بالطلب",
     "hint": "يصل الطلب المكتمل إلى صاحب المتجر مباشرة", "enforced": False},
    {"key": "weekly_report", "label": "تقرير أسبوعي",
     "hint": "عدد الزبائن والطلبات خلال الأسبوع", "enforced": False},
    {"key": "merchant_dashboard", "label": "لوحة خاصة بالتاجر",
     "hint": "لوحة يتابع منها التاجر بوته بنفسه", "enforced": False},
]
FEATURE_KEYS = [f["key"] for f in FEATURE_CATALOG]

# الأسعار بالدينار الجزائري. monthly_limit: عدد المحادثات (زبائن مختلفون) شهرياً، 0 = بلا حد.
DEFAULT_PLANS = [
    {"key": "trial", "name": "تجريبية", "setup_fee": 15000, "price": 3500, "monthly_limit": 300,
     "features": [], "active": True},
    {"key": "basic", "name": "عادية", "setup_fee": 25000, "price": 6000, "monthly_limit": 1000,
     "features": ["memory", "save_chat"], "active": True},
    {"key": "pro", "name": "برو", "setup_fee": 35000, "price": 9000, "monthly_limit": 3000,
     "features": ["memory", "save_chat", "pdf_rag"], "active": True},
    {"key": "business", "name": "بزنس (قريباً)", "setup_fee": 50000, "price": 13000, "monthly_limit": 0,
     "features": ["memory", "save_chat", "pdf_rag", "human_handoff", "order_log",
                  "owner_notify", "weekly_report"], "active": False},
    {"key": "vip", "name": "VIP (قريباً)", "setup_fee": 70000, "price": 20000, "monthly_limit": 0,
     "features": ["memory", "save_chat", "pdf_rag", "human_handoff", "order_log",
                  "owner_notify", "weekly_report", "merchant_dashboard"], "active": False},
]


# ---------------------------------------------------------------------------
# أدوات عامة
# ---------------------------------------------------------------------------
def now():
    """توقيت UTC بدون tzinfo (يُخزَّن ويُقارن بهذه الصيغة دائماً)."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _ts(dt):
    return dt.replace(microsecond=0).isoformat() if dt else None


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


def _iso(dt):
    return dt.isoformat() + "Z" if dt else None


def _loads(s):
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def _int(value, lo, hi, label="القيمة"):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} يجب أن تكون رقماً صحيحاً")
    if n < lo or n > hi:
        raise ValueError(f"{label} يجب أن تكون بين {lo} و{hi}")
    return n


def _mask(secret):
    return ("••••" + secret[-4:]) if secret and len(secret) > 4 else ("••••" if secret else "")


def _enc(value):
    return _fernet.encrypt(value.encode()).decode() if value else None


def _dec(value):
    if not value:
        return ""
    try:
        return _fernet.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        log.error("تعذّر فك تشفير قيمة محفوظة (هل تغيّر ENCRYPTION_KEY؟)")
        return ""


def _integrity_errors():
    errs = (sqlite3.IntegrityError,)
    if psycopg2 is not None:
        errs += (psycopg2.IntegrityError,)
    return errs


# ---------------------------------------------------------------------------
# الاتصال والمعاملات
# ---------------------------------------------------------------------------
class _Tx:
    """غلاف رفيع لتنفيذ SQL بنفس الصيغة (علامات ؟) على SQLite وPostgres."""

    def __init__(self, conn, pg):
        self.conn, self.pg = conn, pg

    def _cursor(self):
        if self.pg:
            return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return self.conn.cursor()

    def _sql(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def q(self, sql, params=()):
        cur = self._cursor()
        cur.execute(self._sql(sql), params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows

    def one(self, sql, params=()):
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql, params=()):
        cur = self._cursor()
        cur.execute(self._sql(sql), params)
        n = cur.rowcount
        cur.close()
        return n

    def insert(self, sql, params=()):
        cur = self._cursor()
        if self.pg:
            cur.execute(self._sql(sql) + " RETURNING id", params)
            new_id = cur.fetchone()["id"]
        else:
            cur.execute(sql, params)
            new_id = cur.lastrowid
        cur.close()
        return new_id


def _pg_get():
    conn = _pool.getconn()
    stale = time.time() - _last_used.get(id(conn), 0) > 30
    if conn.closed or stale:
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            conn.rollback()
        except Exception:
            _pool.putconn(conn, close=True)
            conn = _pool.getconn()
    return conn


@contextmanager
def _tx():
    if backend == "postgres":
        conn = _pg_get()
        try:
            yield _Tx(conn, True)
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            _last_used[id(conn)] = time.time()
            _pool.putconn(conn, close=bool(conn.closed))
    else:
        with _sqlite_lock:
            try:
                yield _Tx(_sqlite, False)
                _sqlite.commit()
            except BaseException:
                _sqlite.rollback()
                raise


def _schema():
    pk = "SERIAL PRIMARY KEY" if backend == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS plans (
            id {pk}, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            price INTEGER NOT NULL DEFAULT 0, duration_days INTEGER NOT NULL DEFAULT 30,
            setup_fee INTEGER NOT NULL DEFAULT 0, monthly_limit INTEGER NOT NULL DEFAULT 0,
            features TEXT NOT NULL DEFAULT '{{}}', sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1)""",
        f"""CREATE TABLE IF NOT EXISTS clients (
            id {pk}, phone_id TEXT UNIQUE NOT NULL, store_name TEXT NOT NULL DEFAULT '',
            whatsapp_token TEXT, gemini_key TEXT, plan_key TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, pending INTEGER NOT NULL DEFAULT 0,
            expiry TEXT, overrides TEXT NOT NULL DEFAULT '{{}}', notes TEXT NOT NULL DEFAULT '',
            messages_total INTEGER NOT NULL DEFAULT 0, last_message_at TEXT,
            last_error TEXT, last_error_at TEXT, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS usage_daily (
            client_id INTEGER NOT NULL, day TEXT NOT NULL, msgs INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (client_id, day))""",
        f"""CREATE TABLE IF NOT EXISTS chats (
            id {pk}, client_id INTEGER NOT NULL, customer TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_chats_client ON chats (client_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_chats_created ON chats (created_at)",
        f"""CREATE TABLE IF NOT EXISTS audit_log (
            id {pk}, ts TEXT NOT NULL, action TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')""",
        "CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS conversations (
            client_id INTEGER NOT NULL, month TEXT NOT NULL, customer TEXT NOT NULL,
            PRIMARY KEY (client_id, month, customer))""",
    ]
    with _tx() as tx:
        for s in stmts:
            tx.x(s)
        _migrate(tx)


def _migrate(tx):
    """يضيف الأعمدة الجديدة إلى قواعد أُنشئت بإصدار أقدم."""
    if backend == "postgres":
        have = {r["column_name"] for r in tx.q(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'plans'")}
    else:
        have = {r["name"] for r in tx.q("PRAGMA table_info(plans)")}
    for col in ("setup_fee", "monthly_limit"):
        if col not in have:
            tx.x(f"ALTER TABLE plans ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")


# ---------------------------------------------------------------------------
# التهيئة
# ---------------------------------------------------------------------------
def init(data_dir):
    global backend, _fernet, custom_encryption_key, _sqlite, _pool, psycopg2
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        import psycopg2 as _pg  # noqa: F401
        import psycopg2.extras  # noqa: F401
        import psycopg2.pool  # noqa: F401
        psycopg2 = _pg
        backend = "postgres"
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        for attempt in range(4):
            try:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 4, url)
                break
            except psycopg2.OperationalError:
                if attempt == 3:
                    raise
                log.warning("القاعدة غير جاهزة بعد، إعادة المحاولة...")
                time.sleep(2 * (attempt + 1))
    else:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "bot.db")
        _sqlite = sqlite3.connect(path, check_same_thread=False)
        _sqlite.row_factory = sqlite3.Row
        backend = "sqlite"
        log.warning("DATABASE_URL غير مضبوط: سيُستعمل SQLite مؤقت وتُمحى البيانات عند كل نشر.")

    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if key:
        custom_encryption_key = True
    else:
        seed = os.environ.get("ADMIN_KEY") or "insecure-default"
        key = base64.urlsafe_b64encode(
            hashlib.sha256(("enc-v1:" + seed).encode()).digest()).decode()
        log.warning("ENCRYPTION_KEY غير مضبوط: اشتُق مفتاح التشفير من ADMIN_KEY (غير مستحسن).")
    _fernet = Fernet(key.encode())

    _schema()
    _seed()


def _insert_default_plans(tx, start=0):
    for i, p in enumerate(DEFAULT_PLANS):
        feats = {k: (k in p["features"]) for k in FEATURE_KEYS}
        if tx.one("SELECT id FROM plans WHERE slug = ?", (p["key"],)):
            continue
        tx.x("INSERT INTO plans (slug, name, price, duration_days, setup_fee, monthly_limit, "
             "features, sort_order, is_active) VALUES (?, ?, ?, 30, ?, ?, ?, ?, ?)",
             (p["key"], p["name"], p["price"], p["setup_fee"], p["monthly_limit"],
              json.dumps(feats), start + i, 1 if p["active"] else 0))


def _seed():
    with _tx() as tx:
        v2_done = tx.one("SELECT k FROM settings WHERE k = 'plans_v2'")
        if not v2_done:
            # قاعدة جديدة أو قديمة بلا مشتركين: تُستبدل الباقات الافتراضية القديمة
            if tx.one("SELECT COUNT(*) AS n FROM clients")["n"] == 0:
                tx.x("DELETE FROM plans")
            base = tx.one("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM plans")["n"]
            _insert_default_plans(tx, base)
        has_flag = tx.one("SELECT k FROM settings WHERE k = 'bot_enabled'")
    if not v2_done:
        set_setting("plans_v2", "1")
    if not has_flag:
        set_setting("bot_enabled", "1")
        set_setting("currency", "DZD")


# ---------------------------------------------------------------------------
# الإعدادات (مع ذاكرة مؤقتة قصيرة تُبطَل عند أي تعديل)
# ---------------------------------------------------------------------------
_settings_cache = {}


def get_setting(key, default=None):
    cached = _settings_cache.get(key)
    if cached and time.time() - cached[1] < 5:
        return cached[0]
    with _tx() as tx:
        row = tx.one("SELECT v FROM settings WHERE k = ?", (key,))
    val = row["v"] if row else default
    _settings_cache[key] = (val, time.time())
    return val


def set_setting(key, value):
    value = str(value)
    with _tx() as tx:
        tx.x("INSERT INTO settings (k, v) VALUES (?, ?) "
             "ON CONFLICT (k) DO UPDATE SET v = excluded.v", (key, value))
    _settings_cache[key] = (value, time.time())


def global_expiry():
    """نهاية التجربة العامة (للأرقام غير المسجّلة كمشتركين)."""
    exp = _dt(get_setting("global_expiry"))
    if exp:
        return exp
    exp = now() + timedelta(days=12)
    set_setting("global_expiry", _ts(exp))
    return exp


def extend_global_trial(days):
    days = _int(days, 1, 3650, "عدد الأيام")
    exp = max(global_expiry(), now()) + timedelta(days=days)
    set_setting("global_expiry", _ts(exp))
    return exp


# ---------------------------------------------------------------------------
# سجل العمليات
# ---------------------------------------------------------------------------
def audit(action, target="", detail=""):
    try:
        with _tx() as tx:
            tx.x("INSERT INTO audit_log (ts, action, target, detail) VALUES (?, ?, ?, ?)",
                 (_ts(now()), action[:60], str(target)[:160], str(detail)[:2000]))
    except Exception:
        log.exception("تعذّر كتابة سجل العمليات")


def list_audit(limit=100):
    with _tx() as tx:
        rows = tx.q("SELECT ts, action, target, detail FROM audit_log ORDER BY id DESC LIMIT ?",
                    (int(limit),))
    return [{"ts": _iso(_dt(r["ts"])), "action": r["action"], "target": r["target"],
             "detail": r["detail"]} for r in rows]


# ---------------------------------------------------------------------------
# الباقات
# ---------------------------------------------------------------------------
def _plan_dict(r):
    feats = _loads(r["features"])
    return {
        "key": r["slug"], "name": r["name"], "price": r["price"],
        "duration_days": r["duration_days"], "setup_fee": r["setup_fee"],
        "monthly_limit": r["monthly_limit"],
        "features": {k: bool(feats.get(k, False)) for k in FEATURE_KEYS},
        "sort_order": r["sort_order"], "is_active": bool(r["is_active"]),
    }


def list_plans():
    with _tx() as tx:
        rows = tx.q("SELECT * FROM plans ORDER BY sort_order, id")
        counts = {r["plan_key"]: r["n"] for r in tx.q(
            "SELECT plan_key, COUNT(*) AS n FROM clients GROUP BY plan_key")}
    out = []
    for r in rows:
        d = _plan_dict(r)
        d["client_count"] = counts.get(r["slug"], 0)
        out.append(d)
    return out


def get_plan(key):
    with _tx() as tx:
        r = tx.one("SELECT * FROM plans WHERE slug = ?", (key,))
    return _plan_dict(r) if r else None


def save_plan(data):
    key = str(data.get("key", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{2,40}", key):
        raise ValueError("معرّف الباقة: حروف إنجليزية صغيرة وأرقام وشرطة فقط (2 إلى 40 خانة)")
    name = str(data.get("name", "")).strip()[:80]
    if not name:
        raise ValueError("اسم الباقة مطلوب")
    price = _int(data.get("price", 0), 0, 100_000_000, "السعر")
    duration = _int(data.get("duration_days", 30), 1, 3650, "مدة الباقة")
    setup_fee = _int(data.get("setup_fee", 0), 0, 100_000_000, "رسم التركيب")
    limit = _int(data.get("monthly_limit", 0), 0, 10_000_000, "حد المحادثات")
    raw = data.get("features") or {}
    feats = json.dumps({k: bool(raw.get(k, False)) for k in FEATURE_KEYS})
    is_active = 1 if data.get("is_active", True) else 0

    with _tx() as tx:
        exists = tx.one("SELECT id FROM plans WHERE slug = ?", (key,))
        if exists:
            tx.x("UPDATE plans SET name = ?, price = ?, duration_days = ?, setup_fee = ?, "
                 "monthly_limit = ?, features = ?, is_active = ? WHERE slug = ?",
                 (name, price, duration, setup_fee, limit, feats, is_active, key))
            action = "تعديل باقة"
        else:
            nxt = tx.one("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM plans")["n"]
            tx.x("INSERT INTO plans (slug, name, price, duration_days, setup_fee, monthly_limit, "
                 "features, sort_order, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (key, name, price, duration, setup_fee, limit, feats, nxt, is_active))
            action = "إنشاء باقة"
    audit(action, key, f"{name} | {price} | {duration} يوم")
    return get_plan(key)


def delete_plan(key):
    with _tx() as tx:
        used = tx.one("SELECT COUNT(*) AS n FROM clients WHERE plan_key = ?", (key,))["n"]
        if used:
            raise ValueError(f"لا يمكن حذف باقة يشترك بها {used} مشترك، انقلهم إلى باقة أخرى أولاً")
        if tx.one("SELECT COUNT(*) AS n FROM plans")["n"] <= 1:
            raise ValueError("لا يمكن حذف آخر باقة")
        if tx.x("DELETE FROM plans WHERE slug = ?", (key,)) == 0:
            raise LookupError("الباقة غير موجودة")
    audit("حذف باقة", key)


# ---------------------------------------------------------------------------
# المشتركون
# ---------------------------------------------------------------------------
def _effective(plan, overrides):
    feats = {k: False for k in FEATURE_KEYS}
    if plan:
        feats.update(plan["features"])
    for k, v in (overrides or {}).items():
        if k in feats and isinstance(v, bool):
            feats[k] = v
    return feats


def _status(enabled, pending, expiry, now_):
    if pending:
        return "pending"
    if not enabled:
        return "paused"
    if expiry and expiry < now_:
        return "expired"
    return "active"


def _month():
    return now().strftime("%Y-%m")


def _convs_month():
    with _tx() as tx:
        rows = tx.q("SELECT client_id, COUNT(*) AS n FROM conversations WHERE month = ? "
                    "GROUP BY client_id", (_month(),))
    return {r["client_id"]: int(r["n"]) for r in rows}


def allow_conversation(client_id, customer, limit):
    """يسجّل المحادثة (زبون مختلف في الشهر) ويُرجع False إن تجاوز المشترك حد باقته.
    الزبون المسجّل سابقاً هذا الشهر يُسمح له دائماً."""
    customer = str(customer)[:40]
    with _tx() as tx:
        known = tx.one("SELECT 1 AS x FROM conversations WHERE client_id = ? AND month = ? "
                       "AND customer = ?", (client_id, _month(), customer))
        if known:
            return True
        if limit and limit > 0:
            n = tx.one("SELECT COUNT(*) AS n FROM conversations WHERE client_id = ? AND month = ?",
                       (client_id, _month()))["n"]
            if n >= limit:
                return False
        tx.x("INSERT INTO conversations (client_id, month, customer) VALUES (?, ?, ?)",
             (client_id, _month(), customer))
    return True


def _client_dict(r, plans_by_key, msgs7=0, secrets=False, convs=0):
    now_ = now()
    expiry = _dt(r["expiry"])
    overrides = _loads(r["overrides"])
    plan = plans_by_key.get(r["plan_key"])
    token = _dec(r["whatsapp_token"])
    gkey = _dec(r["gemini_key"])
    d = {
        "id": r["id"], "phone_id": r["phone_id"], "store_name": r["store_name"],
        "plan_key": r["plan_key"], "plan_name": plan["name"] if plan else r["plan_key"],
        "enabled": bool(r["enabled"]), "pending": bool(r["pending"]),
        "status": _status(r["enabled"], r["pending"], expiry, now_),
        "expiry": _iso(expiry),
        "days_left": (expiry.date() - now_.date()).days if expiry else None,
        "has_token": bool(token), "token_hint": _mask(token),
        "has_gemini_key": bool(gkey), "gemini_hint": _mask(gkey),
        "overrides": {k: v for k, v in overrides.items() if k in FEATURE_KEYS and isinstance(v, bool)},
        "features": _effective(plan, overrides),
        "notes": r["notes"], "messages_total": r["messages_total"], "messages_7d": msgs7,
        "conversations_month": convs, "monthly_limit": plan["monthly_limit"] if plan else 0,
        "last_message_at": _iso(_dt(r["last_message_at"])),
        "last_error": r["last_error"], "last_error_at": _iso(_dt(r["last_error_at"])),
        "created_at": _iso(_dt(r["created_at"])),
    }
    if secrets:
        d["whatsapp_token"] = token
        d["gemini_key"] = gkey
    return d


def _plans_by_key():
    return {p["key"]: p for p in list_plans()}


def _usage_7d():
    start = (now().date() - timedelta(days=6)).isoformat()
    with _tx() as tx:
        rows = tx.q("SELECT client_id, SUM(msgs) AS total FROM usage_daily "
                    "WHERE day >= ? GROUP BY client_id", (start,))
    return {r["client_id"]: int(r["total"] or 0) for r in rows}


def list_clients(q=None, status=None):
    pbk = _plans_by_key()
    usage = _usage_7d()
    convs = _convs_month()
    sql, params = "SELECT * FROM clients", ()
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        sql += " WHERE lower(store_name) LIKE ? OR lower(phone_id) LIKE ? OR lower(notes) LIKE ?"
        params = (like, like, like)
    sql += " ORDER BY id DESC"
    with _tx() as tx:
        rows = tx.q(sql, params)
    out = [_client_dict(r, pbk, usage.get(r["id"], 0), convs=convs.get(r["id"], 0)) for r in rows]
    if status and status != "all":
        out = [c for c in out if c["status"] == status]
    return out


def get_client(cid):
    with _tx() as tx:
        r = tx.one("SELECT * FROM clients WHERE id = ?", (cid,))
    if not r:
        return None
    return _client_dict(r, _plans_by_key(), _usage_7d().get(cid, 0),
                        convs=_convs_month().get(cid, 0))


def get_client_by_phone(phone_id):
    """للاستعمال الداخلي من البوت: يتضمن التوكن ومفتاح Gemini بعد فك التشفير."""
    with _tx() as tx:
        r = tx.one("SELECT * FROM clients WHERE phone_id = ?", (str(phone_id),))
    if not r:
        return None
    return _client_dict(r, _plans_by_key(), 0, secrets=True)


def create_client(data):
    phone_id = str(data.get("phone_id", "")).strip()
    if not re.fullmatch(r"\d{5,32}", phone_id):
        raise ValueError("معرّف رقم الهاتف (Phone Number ID) يجب أن يكون أرقاماً فقط")
    token = str(data.get("whatsapp_token", "")).strip()
    if not token or len(token) > 1000:
        raise ValueError("توكن واتساب مطلوب")
    gkey = str(data.get("gemini_key", "") or "").strip()
    if len(gkey) > 300:
        raise ValueError("مفتاح Gemini غير صالح")
    pbk = _plans_by_key()
    plan_key = data.get("plan_key") or next((k for k, p in pbk.items() if p["is_active"]), "")
    if plan_key not in pbk:
        raise ValueError("الباقة غير موجودة")
    pending = bool(data.get("pending", False))
    if data.get("expiry"):
        expiry = _dt(data["expiry"])
        if expiry is None:
            raise ValueError("تاريخ الانتهاء غير صالح")
    elif pending:
        expiry = None
    else:
        days = _int(data.get("days") or pbk[plan_key]["duration_days"], 1, 3650, "مدة الاشتراك")
        expiry = now() + timedelta(days=days)
    name = (str(data.get("store_name", "")).strip() or "متجر جديد")[:120]
    try:
        with _tx() as tx:
            cid = tx.insert(
                "INSERT INTO clients (phone_id, store_name, whatsapp_token, gemini_key, plan_key, "
                "enabled, pending, expiry, overrides, notes, messages_total, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, 0, ?)",
                (phone_id, name, _enc(token), _enc(gkey), plan_key,
                 1 if data.get("enabled", True) else 0, 1 if pending else 0, _ts(expiry),
                 str(data.get("notes", ""))[:2000], _ts(now())))
    except _integrity_errors():
        raise ValueError("هذا المشترك مسجّل مسبقاً")
    audit("طلب تسجيل جديد" if pending else "إضافة مشترك", name, f"{plan_key} | {phone_id}")
    return get_client(cid)


def update_client(cid, data):
    with _tx() as tx:
        cur = tx.one("SELECT * FROM clients WHERE id = ?", (cid,))
    if not cur:
        raise LookupError("المشترك غير موجود")
    cur_expiry = _dt(cur["expiry"])
    pbk = _plans_by_key()
    vals, log_items = {}, []

    if "store_name" in data:
        name = str(data["store_name"]).strip()[:120]
        if not name:
            raise ValueError("اسم المتجر لا يمكن أن يكون فارغاً")
        vals["store_name"] = name
        log_items.append(("تعديل بيانات", "الاسم"))
    if "notes" in data:
        vals["notes"] = str(data["notes"])[:2000]
    if "plan_key" in data and data["plan_key"] != cur["plan_key"]:
        if data["plan_key"] not in pbk:
            raise ValueError("الباقة غير موجودة")
        vals["plan_key"] = data["plan_key"]
        log_items.append(("تغيير الباقة", f"{cur['plan_key']} ← {data['plan_key']}"))
    if "enabled" in data:
        enabled = 1 if data["enabled"] else 0
        if enabled != int(cur["enabled"]):
            vals["enabled"] = enabled
            log_items.append(("تشغيل البوت" if enabled else "إيقاف البوت", ""))
    if data.get("approve"):
        vals["pending"] = 0
        vals["enabled"] = 1
        if not cur_expiry or cur_expiry < now():
            plan = pbk.get(vals.get("plan_key", cur["plan_key"]))
            vals["expiry"] = _ts(now() + timedelta(days=plan["duration_days"] if plan else 30))
        log_items.append(("الموافقة على مشترك", ""))
    if data.get("whatsapp_token"):
        t = str(data["whatsapp_token"]).strip()
        if len(t) > 1000:
            raise ValueError("التوكن طويل جداً")
        vals["whatsapp_token"] = _enc(t)
        log_items.append(("تعديل بيانات", "توكن واتساب"))
    if data.get("clear_gemini_key"):
        vals["gemini_key"] = None
        log_items.append(("تعديل بيانات", "حذف مفتاح Gemini"))
    elif data.get("gemini_key"):
        g = str(data["gemini_key"]).strip()
        if len(g) > 300:
            raise ValueError("مفتاح Gemini غير صالح")
        vals["gemini_key"] = _enc(g)
        vals["last_error"] = None
        vals["last_error_at"] = None
        log_items.append(("تعديل بيانات", "مفتاح Gemini"))
    if "overrides" in data:
        raw = data["overrides"] or {}
        vals["overrides"] = json.dumps(
            {k: v for k, v in raw.items() if k in FEATURE_KEYS and isinstance(v, bool)})
        log_items.append(("تعديل الميزات", "استثناءات المشترك"))
    if "set_days" in data:
        days = _int(data["set_days"], 1, 3650, "مدة الاشتراك")
        vals["expiry"] = _ts(now() + timedelta(days=days))
        log_items.append(("تجديد الاشتراك", f"{days} يوم من اليوم"))
    elif "extend_days" in data:
        days = _int(data["extend_days"], 1, 3650, "عدد الأيام")
        base = max(cur_expiry, now()) if cur_expiry else now()
        vals["expiry"] = _ts(base + timedelta(days=days))
        log_items.append(("تمديد الاشتراك", f"{days} يوم"))

    if vals:
        cols = ", ".join(f"{k} = ?" for k in vals)  # الأسماء من الكود نفسه وليست من المستخدم
        with _tx() as tx:
            tx.x(f"UPDATE clients SET {cols} WHERE id = ?", (*vals.values(), cid))
    for action, detail in log_items:
        audit(action, vals.get("store_name", cur["store_name"]), detail)
    return get_client(cid)


def delete_client(cid):
    with _tx() as tx:
        cur = tx.one("SELECT store_name FROM clients WHERE id = ?", (cid,))
        if not cur:
            raise LookupError("المشترك غير موجود")
        tx.x("DELETE FROM usage_daily WHERE client_id = ?", (cid,))
        tx.x("DELETE FROM chats WHERE client_id = ?", (cid,))
        tx.x("DELETE FROM conversations WHERE client_id = ?", (cid,))
        tx.x("DELETE FROM clients WHERE id = ?", (cid,))
    audit("حذف مشترك", cur["store_name"])


# ---------------------------------------------------------------------------
# الاستعمال والمحادثات
# ---------------------------------------------------------------------------
def record_message(client_id):
    ts = now()
    with _tx() as tx:
        tx.x("UPDATE clients SET messages_total = messages_total + 1, last_message_at = ?, "
             "last_error = NULL, last_error_at = NULL WHERE id = ?", (_ts(ts), client_id))
        tx.x("INSERT INTO usage_daily (client_id, day, msgs) VALUES (?, ?, 1) "
             "ON CONFLICT (client_id, day) DO UPDATE SET msgs = usage_daily.msgs + 1",
             (client_id, ts.date().isoformat()))


def record_error(client_id, message):
    with _tx() as tx:
        tx.x("UPDATE clients SET last_error = ?, last_error_at = ? WHERE id = ?",
             (str(message)[:200], _ts(now()), client_id))


_last_prune = 0.0


def _maybe_prune():
    global _last_prune
    if time.time() - _last_prune < 3600:
        return
    _last_prune = time.time()
    try:
        days = int(os.environ.get("CHAT_RETENTION_DAYS", "60") or 60)
        with _tx() as tx:
            tx.x("DELETE FROM chats WHERE created_at < ?", (_ts(now() - timedelta(days=days)),))
    except Exception:
        log.exception("تعذّر تنظيف المحادثات القديمة")


def save_chat_turn(client_id, customer, user_text, bot_text):
    ts = _ts(now())
    cust = str(customer)[:40]
    with _tx() as tx:
        for role, body in (("user", user_text), ("bot", bot_text)):
            tx.x("INSERT INTO chats (client_id, customer, role, body, created_at) VALUES (?, ?, ?, ?, ?)",
                 (client_id, cust, role, str(body)[:4000], ts))
    _maybe_prune()


def list_chats(client_id, limit=60):
    with _tx() as tx:
        rows = tx.q("SELECT customer, role, body, created_at FROM chats WHERE client_id = ? "
                    "ORDER BY id DESC LIMIT ?", (client_id, int(limit)))
    rows.reverse()
    return [{"customer": ("…" + r["customer"][-4:]) if len(r["customer"]) > 4 else r["customer"],
             "role": r["role"], "text": r["body"], "at": _iso(_dt(r["created_at"]))} for r in rows]


# ---------------------------------------------------------------------------
# نظرة عامة
# ---------------------------------------------------------------------------
def overview():
    cl = list_clients()
    plans_list = list_plans()
    pbk = {p["key"]: p for p in plans_list}
    counts = {"total": len(cl), "active": 0, "paused": 0, "expired": 0, "pending": 0}
    expiring, lamps, revenue = [], [], 0.0
    for c in cl:
        counts[c["status"]] += 1
        state = c["status"]
        if state == "active" and c["days_left"] is not None and c["days_left"] <= 7:
            state = "expiring"
            expiring.append({"id": c["id"], "store_name": c["store_name"],
                             "days_left": c["days_left"], "plan_name": c["plan_name"]})
        if c["status"] == "active":
            p = pbk.get(c["plan_key"])
            if p and p["duration_days"]:
                revenue += p["price"] * 30.0 / p["duration_days"]
        lamps.append({"id": c["id"], "name": c["store_name"], "state": state})
    expiring.sort(key=lambda x: x["days_left"])

    today = now().date()
    start = today - timedelta(days=13)
    with _tx() as tx:
        rows = tx.q("SELECT day, SUM(msgs) AS total FROM usage_daily WHERE day >= ? GROUP BY day",
                    (start.isoformat(),))
    by_day = {r["day"]: int(r["total"] or 0) for r in rows}
    series = []
    for i in range(14):
        d = (start + timedelta(days=i)).isoformat()
        series.append({"day": d, "count": by_day.get(d, 0)})

    dist = [{"key": p["key"], "name": p["name"], "count": p["client_count"]} for p in plans_list]
    top = sorted([c for c in cl if c["messages_7d"] > 0],
                 key=lambda c: c["messages_7d"], reverse=True)[:5]
    return {
        "counts": counts,
        "messages_today": series[-1]["count"],
        "messages_7d": sum(s["count"] for s in series[-7:]),
        "revenue_monthly": round(revenue),
        "currency": get_setting("currency", "DZD"),
        "series": series, "plans": dist, "expiring": expiring[:8], "lamps": lamps[:300],
        "top": [{"id": c["id"], "store_name": c["store_name"], "messages_7d": c["messages_7d"]}
                for c in top],
        "bot_enabled": get_setting("bot_enabled", "1") == "1",
        "storage": backend,
    }


# ---------------------------------------------------------------------------
# استيراد قديم من clients.json (مرة واحدة إذا كانت الجداول فارغة)
# ---------------------------------------------------------------------------
def import_legacy_clients(path):
    if not os.path.exists(path):
        return 0
    with _tx() as tx:
        if tx.one("SELECT COUNT(*) AS n FROM clients")["n"]:
            return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f)
    except (ValueError, OSError):
        return 0
    first_plan = next(iter(_plans_by_key()), "trial")
    n = 0
    for c in items if isinstance(items, list) else []:
        try:
            create_client({
                "phone_id": c.get("phone_id"), "whatsapp_token": c.get("whatsapp_token"),
                "store_name": c.get("store_name", ""), "plan_key": first_plan,
                "expiry": c.get("expiry"),
            })
            n += 1
        except (ValueError, TypeError, AttributeError):
            continue
    if n:
        audit("استيراد مشتركين", "clients.json", f"{n} مشترك")
    return n
