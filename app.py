import os
import json
import time
import hmac
import hashlib
import logging
import threading
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template

load_dotenv()

# ---------------------------------------------------------------------------
# الإعدادات (كلها من متغيرات البيئة، لا أسرار داخل الكود)
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
ADMIN_KEY = os.environ.get("ADMIN_KEY")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
DEFAULT_WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
META_APP_SECRET = os.environ.get("META_APP_SECRET")  # اختياري: للتحقق من توقيع ميتا
ENABLE_PDF_RAG = os.environ.get("ENABLE_PDF_RAG", "false").lower() == "true"

# مجلد حفظ الملفات. على Render يجب أن يكون مسار قرص دائم (Persistent Disk)
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

SUBSCRIPTION_FILE = os.path.join(DATA_DIR, "subscription.json")
CLIENTS_FILE = os.path.join(DATA_DIR, "clients.json")

if not API_KEY:
    raise ValueError("يجب ضبط المتغير GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

if ENABLE_PDF_RAG:
    from rag_strong import build_or_load_db  # يُستورد فقط عند التفعيل

app = Flask(__name__)

file_lock = threading.RLock()
history_lock = threading.Lock()
chat_histories = {}
rag_dbs = {}

MAX_HISTORY = 20
MAX_CONVERSATIONS = 500  # سقف عدد المحادثات في الذاكرة (لحماية السيرفر الضعيف)
HISTORY_IN_PROMPT = 6


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def safe_generate_content(prompt, system_instruction=""):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": API_KEY}
    data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3},
    }
    if system_instruction:
        data["system_instruction"] = {"parts": [{"text": system_instruction}]}

    last_error = None
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=data, timeout=20)
        except requests.RequestException as e:
            last_error = f"network error: {type(e).__name__}"
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            break

        if r.status_code == 200:
            try:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError, ValueError):
                raise RuntimeError("Gemini returned an empty or blocked response")

        last_error = f"status {r.status_code}"
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        break

    raise RuntimeError(f"Gemini error: {last_error}")


# ---------------------------------------------------------------------------
# التخزين (JSON مع كتابة آمنة)
# ---------------------------------------------------------------------------
def read_json(path, default):
    with file_lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return default


def write_json(path, data):
    with file_lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def load_clients():
    data = read_json(CLIENTS_FILE, [])
    return data if isinstance(data, list) else []


def save_clients(data):
    write_json(CLIENTS_FILE, data)


def get_client_by_phone(phone_id):
    for c in load_clients():
        if str(c.get("phone_id")) == str(phone_id):
            return c
    return None


def get_expiry():
    with file_lock:
        data = read_json(SUBSCRIPTION_FILE, None)
        if not data or "expiry" not in data:
            expiry = datetime.now() + timedelta(days=12)
            write_json(SUBSCRIPTION_FILE, {"expiry": expiry.isoformat()})
            return expiry
        return datetime.fromisoformat(data["expiry"])


def is_expired():
    return datetime.now() > get_expiry()


def is_client_expired(client):
    try:
        return datetime.now() > datetime.fromisoformat(client["expiry"])
    except (KeyError, TypeError, ValueError):
        return False


def activate_subscription(days=30):
    expiry = datetime.now() + timedelta(days=days)
    write_json(SUBSCRIPTION_FILE, {"expiry": expiry.isoformat()})
    return expiry


def load_store_data(phone_id=None):
    default = {"products": [], "delivery": "58 wilaya", "payment": "COD"}
    candidates = []
    if phone_id:
        candidates.append(os.path.join(DATA_DIR, f"store_data_{phone_id}.json"))
    candidates.append(os.path.join(DATA_DIR, "store_data.json"))
    for path in candidates:
        if os.path.exists(path):
            return read_json(path, default)
    return default


# ---------------------------------------------------------------------------
# RAG (اختياري)
# ---------------------------------------------------------------------------
def get_client_rag(phone_id):
    if phone_id in rag_dbs:
        return rag_dbs[phone_id]
    pdf_filename = os.path.join(DATA_DIR, f"catalogue_{phone_id}.pdf")
    if not os.path.exists(pdf_filename):
        pdf_filename = os.path.join(DATA_DIR, "catalogue.pdf")
    rag_dbs[phone_id] = build_or_load_db(pdf_filename)
    return rag_dbs[phone_id]


def get_catalogue_context(phone_id, msg):
    if not ENABLE_PDF_RAG:
        return ""
    try:
        return get_client_rag(phone_id).search(msg, top_k=3)
    except Exception:
        log.exception("RAG lookup failed")
        return ""


# ---------------------------------------------------------------------------
# منطق الرد المشترك
# ---------------------------------------------------------------------------
def build_reply(history_key, msg, phone_id=None):
    store = load_store_data(phone_id)
    system_instruction = (
        "You are a sales assistant for this store. Answer only from the store data "
        "and the catalogue context. If the information is not available, say you will "
        "check with the store. "
        "LANGUAGE: mirror the customer's language, script and dialect in every reply. "
        "If they write Algerian Arabic (Darija) in Latin letters with numbers (Arabizi, "
        "for example '3lach', 'kb3t', 'saha', 'wesh'), reply in Algerian Darija written "
        "in Latin letters in the same way. If they write in Arabic script, reply in Arabic "
        "script using simple Algerian Darija or easy Arabic matching their level. "
        "If they write French, reply in French. If they write English, reply in English. "
        "Keep replies short, friendly and natural. "
        "ORDERS: you cannot place or confirm orders yourself. Collect the customer's name, "
        "phone number, wilaya and address, then tell them the store team will contact them "
        "to confirm. Never say an order is confirmed or registered. "
        "Use plain text only, no markdown. "
        f"Store: {json.dumps(store, ensure_ascii=False)}"
    )
    context = get_catalogue_context(phone_id, msg)

    with history_lock:
        history = list(chat_histories.get(history_key, []))
    history_text = "\n".join(history[-HISTORY_IN_PROMPT:])

    prompt = (
        f"Chat history:\n{history_text}\n\n"
        f"Context from catalogue:\n{context}\n\n"
        f"User: {msg}"
    )
    reply = safe_generate_content(prompt, system_instruction)
    reply = reply.replace("**", "").replace("*", "").strip()

    with history_lock:
        if history_key not in chat_histories and len(chat_histories) >= MAX_CONVERSATIONS:
            chat_histories.pop(next(iter(chat_histories)))
        h = chat_histories.setdefault(history_key, [])
        h.append(f"User: {msg}")
        h.append(f"Bot: {reply}")
        if len(h) > MAX_HISTORY:
            chat_histories[history_key] = h[-MAX_HISTORY:]
    return reply


def is_admin(data):
    if not ADMIN_KEY or not data:
        return False
    return hmac.compare_digest(str(data.get("key", "")), ADMIN_KEY)


# ---------------------------------------------------------------------------
# المسارات
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    return render_template("chat.html")


@app.route("/chat", methods=["POST"])
def chat():
    try:
        if is_expired():
            return jsonify({"reply": "Trial ended. Contact admin for activation."})

        data = request.get_json(silent=True)
        msg = (data or {}).get("message", "")
        msg = msg.strip() if isinstance(msg, str) else ""
        if not msg:
            return jsonify({"reply": "Please send a message."})

        sid = str((data or {}).get("session_id", ""))[:64] or "anon"
        reply = build_reply(f"web:{sid}", msg)
        return jsonify({"reply": reply})
    except Exception:
        log.exception("/chat failed")
        return jsonify({"reply": "Sorry, something went wrong. Please try again."})


@app.route("/activate", methods=["POST"])
def activate():
    data = request.get_json(silent=True)
    if not is_admin(data):
        return jsonify({"error": "wrong key"}), 403
    try:
        days = int(data.get("days", 30))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid days"}), 400

    phone_id = data.get("phone_id")
    if phone_id:
        clients = load_clients()
        for c in clients:
            if str(c.get("phone_id")) == str(phone_id):
                c["expiry"] = (datetime.now() + timedelta(days=days)).isoformat()
                save_clients(clients)
                return jsonify({"message": f"Client activated until {c['expiry']}"})
        return jsonify({"error": "client not found"}), 404

    new_date = activate_subscription(days)
    return jsonify({"message": f"Activated until {new_date}"})


@app.route("/status", methods=["GET"])
def status():
    expiry = get_expiry()
    return jsonify({"expiry": expiry.isoformat(), "expired": is_expired()})


@app.route("/save-config", methods=["POST"])
def save_config():
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        phone_id = data.get("phoneId")
        token = data.get("token")
        if not phone_id or not token:
            return jsonify({"success": False, "error": "Missing fields"}), 400

        with file_lock:
            clients = load_clients()
            existing = next(
                (c for c in clients if str(c.get("phone_id")) == str(phone_id)), None
            )
            if existing:
                existing["whatsapp_token"] = token
            else:
                clients.append({
                    "phone_id": phone_id,
                    "whatsapp_token": token,
                    "store_name": "My Store",
                    "expiry": (datetime.now() + timedelta(days=30)).isoformat(),
                })
            save_clients(clients)
        return jsonify({"success": True, "message": "Configuration saved successfully!"})
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
        msg = message["text"]["body"]
        num = message["from"]

        client = get_client_by_phone(phone_id_real)
        if client:
            whatsapp_token = client.get("whatsapp_token")
            phone_id = client.get("phone_id", phone_id_real)
            expired = is_client_expired(client)
        else:
            whatsapp_token = DEFAULT_WHATSAPP_TOKEN
            phone_id = phone_id_real
            expired = is_expired()

        if not whatsapp_token:
            log.error("No WhatsApp token for phone_id %s", phone_id_real)
            return "ok", 200

        if expired:
            reply = "Trial ended. Contact admin for activation."
        else:
            try:
                reply = build_reply(f"{phone_id_real}:{num}", msg, phone_id_real)
            except Exception:
                log.exception("Reply generation failed")
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
    except Exception:
        log.exception("Webhook processing failed")
    return "ok", 200


@app.route("/add-client", methods=["POST"])
def add_client():
    data = request.get_json(silent=True)
    if not is_admin(data):
        return jsonify({"error": "wrong key"}), 403
    if not data.get("phone_id") or not data.get("whatsapp_token"):
        return jsonify({"error": "phone_id and whatsapp_token are required"}), 400
    try:
        days = int(data.get("days", 30))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid days"}), 400

    with file_lock:
        clients = load_clients()
        entry = {
            "phone_id": data["phone_id"],
            "whatsapp_token": data["whatsapp_token"],
            "store_name": data.get("store_name", "store"),
            "expiry": (datetime.now() + timedelta(days=days)).isoformat(),
        }
        for i, c in enumerate(clients):
            if str(c.get("phone_id")) == str(entry["phone_id"]):
                clients[i] = entry
                break
        else:
            clients.append(entry)
        save_clients(clients)
    return jsonify({"ok": True, "count": len(clients)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)
