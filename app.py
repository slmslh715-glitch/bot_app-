import os
import re
import json
import time
import hmac
import hashlib
import logging
import threading
from datetime import timedelta
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template, session
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

import db  # noqa: E402  (يقرأ متغيرات البيئة عند التهيئة)

# ---------------------------------------------------------------------------
# الإعدادات (كلها من متغيرات البيئة، لا أسرار داخل الكود)
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("GEMINI_API_KEY")  # مفتاح احتياطي للمشتركين الذين بلا مفتاح خاص
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
ADMIN_KEY = os.environ.get("ADMIN_KEY")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
DEFAULT_WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
META_APP_SECRET = os.environ.get("META_APP_SECRET")  # اختياري: للتحقق من توقيع ميتا
ENABLE_PDF_RAG = os.environ.get("ENABLE_PDF_RAG", "false").lower() == "true"

# مجلد ملفات المتاجر وكتالوجات PDF (وقاعدة SQLite المؤقتة عند غياب DATABASE_URL)
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
LEGACY_CLIENTS_FILE = os.path.join(DATA_DIR, "clients.json")

if not API_KEY:
    raise ValueError("يجب ضبط المتغير GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

if ENABLE_PDF_RAG:
    from rag_strong import build_or_load_db  # يُستورد فقط عند التفعيل

db.init(DATA_DIR)
try:
    db.import_legacy_clients(LEGACY_CLIENTS_FILE)
except Exception:
    log.exception("تعذّر استيراد clients.json القديم")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("session-v1:" + (ADMIN_KEY or "none")).encode()).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=1024 * 1024,
)

history_lock = threading.Lock()
chat_histories = {}
chat_langs = {}
rag_dbs = {}

MAX_HISTORY = 20
MAX_CONVERSATIONS = 500  # سقف عدد المحادثات في الذاكرة (لحماية السيرفر الضعيف)
HISTORY_IN_PROMPT = 6

# ---------------------------------------------------------------------------
# تحديد معدّل الطلبات (في الذاكرة)
# ---------------------------------------------------------------------------
_rl = {}
_rl_lock = threading.Lock()


def rate_count(bucket, window):
    now = time.time()
    with _rl_lock:
        hits = [t for t in _rl.get(bucket, []) if now - t < window]
        _rl[bucket] = hits
        return len(hits)


def rate_hit(bucket):
    with _rl_lock:
        _rl.setdefault(bucket, []).append(time.time())
        if len(_rl) > 3000:
            for k in list(_rl)[:1500]:
                _rl.pop(k, None)


# ---------------------------------------------------------------------------
# كشف لغة الزبون (حتى لا يخلط البوت بين اللغات)
# ---------------------------------------------------------------------------
ARABIC_RE = re.compile(r"[؀-ۿ]")
TOKEN_RE = re.compile(r"[a-zà-ÿ0-9']+")
# رقم داخل الكلمة بين حروف (nb3at, kb3t) أو في أولها (3lach, 7abib)
ARABIZI_DIGIT_RE = re.compile(r"^(?:[a-z]+[2356789][a-z]+|[2356789][a-z]{2,})")
FRENCH_ACCENTS = set("éèêëàâçùûôîï")

DARIJA_WORDS = {
    "salam", "slm", "saha", "sahit", "wesh", "wach", "wech", "kifach", "kifah", "kayen",
    "kayna", "kayn", "makach", "bezaf", "bzaf", "bezzaf", "chhal", "dok", "dork", "daba",
    "rani", "rak", "raki", "nahb", "nheb", "hab", "habit", "hbit", "bghit", "bghina",
    "afak", "mlih", "mliha", "khoya", "khouya", "yakhi", "ta3", "ghir", "barka", "tfadal",
    "wella", "wla", "bach", "bech", "mazal", "wakha", "walo", "sah", "ey", "eyh",
}
FRENCH_WORDS = {
    "bonjour", "bonsoir", "merci", "combien", "prix", "livraison", "je", "tu", "vous",
    "nous", "est", "avec", "pour", "quel", "quelle", "quels", "disponible", "commande",
    "commander", "svp", "voulez", "veux", "voudrais", "produit", "produits", "le", "la",
    "les", "un", "une", "des", "du", "comment", "oui", "non", "quand", "payer", "paiement",
    "salut", "bien", "aussi", "mais", "ou", "et", "dans", "sur", "ce", "cette",
}
ENGLISH_WORDS = {
    "hello", "hi", "hey", "price", "how", "much", "what", "do", "you", "have", "is", "are",
    "the", "delivery", "available", "order", "want", "buy", "please", "thanks", "thank",
    "can", "my", "where", "which", "with", "for", "and", "this", "that", "cost", "need",
    "wow", "amazing", "great", "nice", "good", "awesome", "cool", "yes", "perfect", "ok",
}

LANGUAGE_RULES = {
    "ar": (
        "Reply ONLY in Arabic script. If the customer uses Algerian Darija words, answer in "
        "simple Algerian Darija written in Arabic script; otherwise answer in clear, simple "
        "Arabic. Do not use Latin letters except for product names, brands and numbers."
    ),
    "darija_latin": (
        "Reply ONLY in Algerian Darija written in Latin letters with numbers (Arabizi), the "
        "same way the customer writes. Do NOT use Arabic script. Do NOT write full French "
        "sentences; common loanwords such as prix, livraison, commande are fine."
    ),
    "fr": "Reply ONLY in French. Do not use Arabic script or Darija.",
    "en": "Reply ONLY in English. Do not use Arabic script, Darija or French.",
}


def detect_language(text):
    """يعيد ar أو darija_latin أو fr أو en، أو None إذا لم تكن الإشارة واضحة."""
    if ARABIC_RE.search(text):
        return "ar"
    lowered = text.lower()
    tokens = TOKEN_RE.findall(lowered)
    if not tokens:
        return None
    if any(t in DARIJA_WORDS or ARABIZI_DIGIT_RE.match(t) for t in tokens):
        return "darija_latin"
    if any(ch in FRENCH_ACCENTS for ch in lowered) or any(t in FRENCH_WORDS for t in tokens):
        return "fr"
    if any(t in ENGLISH_WORDS for t in tokens):
        return "en"
    return None


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def safe_generate_content(prompt, system_instruction="", api_key=None, attempts=3, timeout=20):
    key = api_key or API_KEY
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3},
    }
    if system_instruction:
        data["system_instruction"] = {"parts": [{"text": system_instruction}]}

    last_error = None
    for attempt in range(attempts):
        try:
            r = requests.post(url, headers=headers, json=data, timeout=timeout)
        except requests.RequestException as e:
            last_error = f"network error: {type(e).__name__}"
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            break

        if r.status_code == 200:
            try:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError, ValueError):
                raise RuntimeError("Gemini returned an empty or blocked response")

        last_error = f"status {r.status_code}"
        if r.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
            time.sleep(2 ** attempt)
            continue
        break

    raise RuntimeError(f"Gemini error: {last_error}")


# ---------------------------------------------------------------------------
# ملفات المتاجر و RAG (اختياري)
# ---------------------------------------------------------------------------
def load_store_data(phone_id=None):
    default = {"products": [], "delivery": "58 wilaya", "payment": "COD"}
    candidates = []
    if phone_id:
        candidates.append(os.path.join(DATA_DIR, f"store_data_{phone_id}.json"))
    candidates.append(os.path.join(DATA_DIR, "store_data.json"))
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (ValueError, OSError):
                log.exception("تعذّرت قراءة %s", path)
    return default


def get_client_rag(phone_id):
    if phone_id in rag_dbs:
        return rag_dbs[phone_id]
    pdf_filename = os.path.join(DATA_DIR, f"catalogue_{phone_id}.pdf")
    if not os.path.exists(pdf_filename):
        pdf_filename = os.path.join(DATA_DIR, "catalogue.pdf")
    rag_dbs[phone_id] = build_or_load_db(pdf_filename)
    return rag_dbs[phone_id]


def get_catalogue_context(phone_id, msg, allowed):
    if not (ENABLE_PDF_RAG and allowed):
        return ""
    try:
        return get_client_rag(phone_id).search(msg, top_k=3)
    except Exception:
        log.exception("RAG lookup failed")
        return ""


# ---------------------------------------------------------------------------
# منطق الرد المشترك
# ---------------------------------------------------------------------------
def build_reply(history_key, msg, phone_id=None, api_key=None, use_rag=False):
    store = load_store_data(phone_id)

    with history_lock:
        history = list(chat_histories.get(history_key, []))
        lang = detect_language(msg) or chat_langs.get(history_key) or "ar"

    system_instruction = (
        "You are a sales assistant for this store. Answer only from the store data "
        "and the catalogue context. If the information is not available, say you will "
        "check with the store. Keep replies short, friendly and natural. "
        f"LANGUAGE RULE (strict): {LANGUAGE_RULES[lang]} "
        "Never mix languages or scripts in one reply. Follow only the language of the "
        "customer's latest message and ignore the language used in your earlier replies. "
        "ORDERS: you cannot place or confirm orders yourself. Collect the customer's name, "
        "phone number, wilaya and address, then tell them the store team will contact them "
        "to confirm. Never say an order is confirmed or registered. "
        "Use plain text only, no markdown. "
        f"Store: {json.dumps(store, ensure_ascii=False)}"
    )
    context = get_catalogue_context(phone_id, msg, use_rag)
    history_text = "\n".join(history[-HISTORY_IN_PROMPT:])

    prompt = (
        f"Chat history:\n{history_text}\n\n"
        f"Context from catalogue:\n{context}\n\n"
        f"User: {msg}"
    )
    reply = safe_generate_content(prompt, system_instruction, api_key=api_key)
    reply = reply.replace("**", "").replace("*", "").strip()

    with history_lock:
        if history_key not in chat_histories and len(chat_histories) >= MAX_CONVERSATIONS:
            evicted = next(iter(chat_histories))
            chat_histories.pop(evicted)
            chat_langs.pop(evicted, None)
        h = chat_histories.setdefault(history_key, [])
        h.append(f"User: {msg}")
        h.append(f"Bot: {reply}")
        if len(h) > MAX_HISTORY:
            chat_histories[history_key] = h[-MAX_HISTORY:]
        chat_langs[history_key] = lang
    return reply


def bot_globally_enabled():
    return db.get_setting("bot_enabled", "1") == "1"


# ---------------------------------------------------------------------------
# رؤوس الأمان
# ---------------------------------------------------------------------------
ADMIN_CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/admin"):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        if request.path == "/admin":
            resp.headers["Content-Security-Policy"] = ADMIN_CSP
    return resp


# ---------------------------------------------------------------------------
# المسارات العامة
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    return render_template("chat.html")


@app.route("/chat", methods=["POST"])
def chat():
    try:
        if not bot_globally_enabled():
            return jsonify({"reply": "The assistant is temporarily unavailable."})
        if db.now() > db.global_expiry():
            return jsonify({"reply": "Trial ended. Contact admin for activation."})

        data = request.get_json(silent=True)
        msg = (data or {}).get("message", "")
        msg = msg.strip() if isinstance(msg, str) else ""
        if not msg:
            return jsonify({"reply": "Please send a message."})
        msg = msg[:2000]

        sid = str((data or {}).get("session_id", ""))[:64] or "anon"
        reply = build_reply(f"web:{sid}", msg)
        return jsonify({"reply": reply})
    except Exception:
        log.exception("/chat failed")
        return jsonify({"reply": "Sorry, something went wrong. Please try again."})


@app.route("/status", methods=["GET"])
def status():
    return jsonify({"ok": True})


@app.route("/save-config", methods=["POST"])
def save_config():
    """تسجيل ذاتي: يُنشأ المشترك بحالة «بانتظار الموافقة» ولا يعمل حتى توافق عليه من اللوحة."""
    try:
        ip = request.remote_addr or "?"
        if rate_count(f"save:{ip}", 3600) >= 6:
            return jsonify({"success": False, "error": "Too many attempts"}), 429
        rate_hit(f"save:{ip}")

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400
        phone_id = str(data.get("phoneId", "")).strip()
        token = str(data.get("token", "")).strip()
        if not phone_id or not token:
            return jsonify({"success": False, "error": "Missing fields"}), 400

        try:
            db.create_client({"phone_id": phone_id, "whatsapp_token": token,
                              "pending": True, "enabled": False})
        except ValueError as e:
            msg = str(e)
            if "مسجّل مسبقاً" in msg:
                return jsonify({"success": False,
                                "error": "This number is already registered. Contact the admin to update it."}), 409
            return jsonify({"success": False, "error": "Invalid data"}), 400
        return jsonify({"success": True, "pending": True,
                        "message": "Request received. The admin will review and activate it."})
    except Exception:
        log.exception("/save-config failed")
        return jsonify({"success": False, "error": "Server error"}), 500


@app.route("/webhook/whatsapp", methods=["GET"])
def verify_whatsapp():
    if VERIFY_TOKEN and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    return "fail", 403


def valid_signature(raw_body):
    """يتحقق من توقيع ميتا إذا تم ضبط META_APP_SECRET، وإلا يتجاوز التحقق."""
    if not META_APP_SECRET:
        return True
    header = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header, expected)


@app.route("/webhook/whatsapp", methods=["POST"])
def receive_whatsapp():
    raw = request.get_data()
    if not valid_signature(raw):
        return "forbidden", 403

    data = request.get_json(silent=True) or {}
    try:
        value = data["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:  # إشعارات الحالة (تم التسليم/القراءة) تُتجاهل
            return "ok", 200
        message = messages[0]
        if message.get("type") != "text":
            return "ok", 200

        phone_id_real = value["metadata"]["phone_number_id"]
        msg = str(message["text"]["body"])[:2000]
        num = message["from"]

        # الإيقاف العام لجميع البوتات
        if not bot_globally_enabled():
            return "ok", 200

        client = db.get_client_by_phone(phone_id_real)
        client_id = None
        gemini_key = None
        feats = {}
        if client:
            # مشترك موقوف أو بانتظار الموافقة أو منتهٍ: لا رد إطلاقاً
            if client["status"] != "active":
                log.info("تجاهل رسالة لمشترك %s بحالة %s", client["id"], client["status"])
                return "ok", 200
            whatsapp_token = client["whatsapp_token"]
            phone_id = client["phone_id"]
            gemini_key = client["gemini_key"] or None
            feats = client["features"]
            client_id = client["id"]
            reply = None
        else:
            whatsapp_token = DEFAULT_WHATSAPP_TOKEN
            phone_id = phone_id_real
            reply = ("Trial ended. Contact admin for activation."
                     if db.now() > db.global_expiry() else None)

        if not whatsapp_token:
            log.error("لا يوجد توكن واتساب للرقم %s", phone_id_real)
            return "ok", 200

        if reply is None:
            try:
                reply = build_reply(f"{phone_id_real}:{num}", msg, phone_id_real,
                                    api_key=gemini_key, use_rag=feats.get("pdf_rag", False))
                if client_id:
                    try:
                        db.record_message(client_id)
                        if feats.get("save_chat"):
                            db.save_chat_turn(client_id, num, msg, reply)
                    except Exception:
                        log.exception("تعذّر تسجيل الاستعمال")
            except Exception as e:
                log.exception("Reply generation failed")
                if client_id:
                    try:
                        db.record_error(client_id, str(e))
                    except Exception:
                        log.exception("تعذّر تسجيل الخطأ")
                reply = ("Sorry, the system is currently experiencing high traffic. "
                         "Please resend your message in a few moments.")

        r = requests.post(
            f"https://graph.facebook.com/v20.0/{phone_id}/messages",
            headers={"Authorization": f"Bearer {whatsapp_token}"},
            json={"messaging_product": "whatsapp", "to": num, "text": {"body": reply}},
            timeout=15,
        )
        if r.status_code >= 400:
            log.error("WhatsApp send failed: %s %s", r.status_code, r.text[:300])
            if client_id:
                db.record_error(client_id, f"WhatsApp send failed: {r.status_code}")
    except Exception:
        log.exception("Webhook processing failed")
    return "ok", 200


# ---------------------------------------------------------------------------
# لوحة التحكم: الدخول
# ---------------------------------------------------------------------------
CSRF_HEADER = ("X-Requested-With", "admin-dashboard")


def admin_api(fn):
    """يتطلب جلسة مسؤول، ويحمي الطلبات المغيِّرة من CSRF، ويوحّد معالجة الأخطاء."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not ADMIN_KEY:
            return jsonify({"error": "لوحة التحكم معطّلة: اضبط ADMIN_KEY في Render"}), 503
        if not session.get("admin"):
            return jsonify({"error": "unauthorized"}), 401
        if request.method != "GET" and request.headers.get(CSRF_HEADER[0]) != CSRF_HEADER[1]:
            return jsonify({"error": "forbidden"}), 403
        try:
            return fn(*args, **kwargs)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except LookupError as e:
            return jsonify({"error": str(e)}), 404
        except Exception:
            log.exception("admin api failed: %s", request.path)
            return jsonify({"error": "حدث خطأ في الخادم، راجع سجلات Render"}), 500
    return wrapper


@app.route("/admin")
def admin_page():
    return render_template("admin.html")


@app.route("/admin/api/login", methods=["POST"])
def admin_login():
    if not ADMIN_KEY:
        return jsonify({"error": "لوحة التحكم معطّلة: اضبط ADMIN_KEY في Render"}), 503
    if request.headers.get(CSRF_HEADER[0]) != CSRF_HEADER[1]:
        return jsonify({"error": "forbidden"}), 403
    ip = request.remote_addr or "?"
    if rate_count(f"login:{ip}", 600) >= 8:
        return jsonify({"error": "محاولات كثيرة، انتظر 10 دقائق ثم أعد المحاولة"}), 429
    data = request.get_json(silent=True) or {}
    if not hmac.compare_digest(str(data.get("key", "")), ADMIN_KEY):
        rate_hit(f"login:{ip}")
        db.audit("محاولة دخول فاشلة", ip)
        return jsonify({"error": "المفتاح غير صحيح"}), 401
    session.clear()
    session.permanent = True
    session["admin"] = True
    db.audit("تسجيل دخول", ip)
    return jsonify({"ok": True})


@app.route("/admin/api/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/admin/api/me")
@admin_api
def admin_me():
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# لوحة التحكم: البيانات
# ---------------------------------------------------------------------------
@app.route("/admin/api/overview")
@admin_api
def admin_overview():
    return jsonify(db.overview())


@app.route("/admin/api/features")
@admin_api
def admin_features():
    return jsonify(db.FEATURE_CATALOG)


@app.route("/admin/api/clients", methods=["GET"])
@admin_api
def admin_clients_list():
    return jsonify(db.list_clients(request.args.get("q"), request.args.get("status")))


@app.route("/admin/api/clients", methods=["POST"])
@admin_api
def admin_clients_create():
    data = request.get_json(silent=True) or {}
    return jsonify(db.create_client(data)), 201


@app.route("/admin/api/clients/<int:cid>", methods=["PATCH"])
@admin_api
def admin_clients_update(cid):
    data = request.get_json(silent=True) or {}
    return jsonify(db.update_client(cid, data))


@app.route("/admin/api/clients/<int:cid>", methods=["DELETE"])
@admin_api
def admin_clients_delete(cid):
    db.delete_client(cid)
    return jsonify({"ok": True})


@app.route("/admin/api/clients/<int:cid>/chats")
@admin_api
def admin_clients_chats(cid):
    c = db.get_client(cid)
    if not c:
        raise LookupError("المشترك غير موجود")
    if not c["features"].get("save_chat"):
        return jsonify({"enabled": False, "chats": []})
    return jsonify({"enabled": True, "chats": db.list_chats(cid)})


@app.route("/admin/api/clients/<int:cid>/test-gemini", methods=["POST"])
@admin_api
def admin_clients_test_gemini(cid):
    if rate_count("testkey", 60) >= 10:
        return jsonify({"ok": False, "message": "انتظر قليلاً قبل إعادة الاختبار"}), 429
    rate_hit("testkey")
    with_secrets = None
    c = db.get_client(cid)
    if not c:
        raise LookupError("المشترك غير موجود")
    with_secrets = db.get_client_by_phone(c["phone_id"])
    key = with_secrets["gemini_key"]
    if not key:
        return jsonify({"ok": False, "message": "لا يوجد مفتاح Gemini خاص بهذا المشترك (يستعمل المفتاح العام)"})
    try:
        safe_generate_content("Reply with the single word: OK", "", api_key=key,
                              attempts=1, timeout=15)
        return jsonify({"ok": True, "message": "المفتاح يعمل"})
    except RuntimeError as e:
        code = re.search(r"status (\d+)", str(e))
        hint = {"400": "المفتاح غير صالح", "403": "المفتاح مرفوض أو غير مفعّل",
                "404": "النموذج غير متاح لهذا المفتاح", "429": "تم تجاوز حصة المفتاح"}
        return jsonify({"ok": False,
                        "message": hint.get(code.group(1) if code else "", "فشل الاختبار: " + str(e))})


@app.route("/admin/api/plans", methods=["GET"])
@admin_api
def admin_plans_list():
    return jsonify(db.list_plans())


@app.route("/admin/api/plans", methods=["POST"])
@admin_api
def admin_plans_save():
    return jsonify(db.save_plan(request.get_json(silent=True) or {}))


@app.route("/admin/api/plans/<key>", methods=["DELETE"])
@admin_api
def admin_plans_delete(key):
    db.delete_plan(key)
    return jsonify({"ok": True})


@app.route("/admin/api/audit")
@admin_api
def admin_audit():
    return jsonify(db.list_audit(150))


@app.route("/admin/api/settings", methods=["GET"])
@admin_api
def admin_settings_get():
    return jsonify({
        "bot_enabled": bot_globally_enabled(),
        "currency": db.get_setting("currency", "DZD"),
        "global_expiry": db._iso(db.global_expiry()),
        "storage": db.backend,
        "custom_encryption_key": db.custom_encryption_key,
        "model": GEMINI_MODEL,
        "rag_enabled": ENABLE_PDF_RAG,
    })


@app.route("/admin/api/settings", methods=["POST"])
@admin_api
def admin_settings_set():
    data = request.get_json(silent=True) or {}
    if "bot_enabled" in data:
        enabled = bool(data["bot_enabled"])
        db.set_setting("bot_enabled", "1" if enabled else "0")
        db.audit("تشغيل جميع البوتات" if enabled else "إيقاف جميع البوتات")
    if "currency" in data:
        cur = str(data["currency"]).strip()[:8] or "DZD"
        db.set_setting("currency", cur)
    if data.get("extend_global_trial_days"):
        exp = db.extend_global_trial(data["extend_global_trial_days"])
        db.audit("تمديد التجربة العامة", "", f"حتى {exp.date()}")
    return admin_settings_get.__wrapped__()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)
