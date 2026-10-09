import os
import requests
import json
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, jsonify, request, render_template
from google import genai
from google.genai import types
from datetime import datetime, timedelta
from rag_strong import build_or_load_db
from features import CURRENT_TIER_FEATURES

app = Flask(__name__)
API_KEY = os.environ.get("GEMINI_API_KEY")
VERIFY_TOKEN = "bot123"
if not API_KEY:
    raise ValueError("set GEMINI_API_KEY in CMD")

rag_dbs = {}
def get_client_rag(phone_id):
    if phone_id in rag_dbs:
        return rag_dbs[phone_id]
    
    pdf_filename = f"catalogue_{phone_id}.pdf"
    if not os.path.exists(pdf_filename):
        pdf_filename = "catalogue.pdf"
        
    try:
        rag_dbs[phone_id] = build_or_load_db(pdf_filename)
    except:
        rag_dbs[phone_id] = build_or_load_db("catalogue.pdf")
    return rag_dbs[phone_id]

client = genai.Client(api_key=API_KEY)
chat_histories = {}

FILE = "subscription.json"
ADMIN_KEY = "Zero_Bot#!_Tlm9Xq2"
CLIENTS_FILE = "clients.json"

def load_clients():
    try:
        with open(CLIENTS_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: return []

def save_clients(data):
    with open(CLIENTS_FILE, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

def get_client_by_phone(phone_id):
    for c in load_clients():
        if str(c.get("phone_id")) == str(phone_id): return c
    return None

def save_expiry(date):
    with open(FILE, "w") as f: json.dump({"expiry": date.isoformat()}, f)

def get_expiry():
    if not os.path.exists(FILE):
        expiry = datetime.now() + timedelta(days=3)
        save_expiry(expiry)
        return expiry
    with open(FILE, "r") as f: data = json.load(f)
    return datetime.fromisoformat(data["expiry"])

def is_expired(): return datetime.now() > get_expiry()

def activate_subscription(days=30):
    expiry = datetime.now() + timedelta(days=days)
    save_expiry(expiry)
    return expiry

def load_store_data(phone_id=None):
    try:
        store_file = f"store_data_{phone_id}.json" if phone_id else "store_data.json"
        if not os.path.exists(store_file):
            store_file = "store_data.json"
        with open(store_file, "r", encoding="utf-8") as f: return json.load(f)
    except: return {"products": [], "delivery": "58 wilaya", "payment": "COD"}

@app.route("/")
def home(): return render_template("chat.html")

@app.route("/chat", methods=["POST"])
def chat():
    if is_expired(): return jsonify({"reply": "Trail ended. Contact admin for activation."})
    data = request.get_json(silent=True)
    if not data: return jsonify({"reply": "Please send a message."})
    msg = data.get("message", "").strip()
    if not msg: return jsonify({"reply": "Please send a message."})
    
    global chat_histories
    if "web_user" not in chat_histories:
        chat_histories["web_user"] = []
    
    store = load_store_data()
    system_instruction = f"You are a sales assistant. Store: {json.dumps(store)}"
    config = types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.3)
    
    try: 
        rag_db = build_or_load_db("catalogue.pdf")
        context = rag_db.search(msg, top_k=3)
    except: 
        context = ""
        
    history_text = "\n".join(chat_histories["web_user"][-6:])
    full_prompt = f"Chat history:\n{history_text}\n\nContext from catalogue:\n{context}\n\nUser: {msg}"
    
    response = client.models.generate_content(model="gemini-2.5-flash", contents=full_prompt, config=config)
    clean_reply = response.text.replace("**", "").replace("*", "").strip()
    
    chat_histories["web_user"].append(f"User: {msg}")
    chat_histories["web_user"].append(f"Bot: {clean_reply}")
    if len(chat_histories["web_user"]) > 20: 
        chat_histories["web_user"] = chat_histories["web_user"][-20:]
        
    return jsonify({"reply": clean_reply})

@app.route("/activate", methods=["POST"])
def activate():
    data = request.get_json(silent=True)
    if not data or data.get("key") != ADMIN_KEY: return jsonify({"error": "wrong key"}), 403
    days = data.get("days", 30)
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

        clients = load_clients()

        existing_client = None
        for c in clients:
            if str(c.get("phone_id")) == str(phone_id):
                existing_client = c
                break

        if existing_client:
            existing_client["whatsapp_token"] = token
        else:
            clients.append({
                "phone_id": phone_id,
                "whatsapp_token": token,
                "store_name": "My Store",
                "expiry": (datetime.now() + timedelta(days=30)).isoformat()
            })

        save_clients(clients)
        return jsonify({"success": True, "message": "Configuration saved successfully!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/webhook/whatsapp', methods=['GET'])
def verify_whatsapp():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "fail", 403

@app.route('/webhook/whatsapp', methods=['POST'])
def receive_whatsapp():
    data = request.get_json()
    try:
        value = data['entry'][0]['changes'][0]['value']
        phone_id_real = value['metadata']['phone_number_id']
        msg = value['messages'][0]['text']['body']
        num = value['messages'][0]['from']

        c = get_client_by_phone(phone_id_real)
        if c:
            WHATSAPP_TOKEN = c['whatsapp_token']
            PHONE_ID = c['phone_id']
        else:
            WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
            PHONE_ID = phone_id_real

        global chat_histories
        if num not in chat_histories:
            chat_histories[num] = []

        if is_expired(): 
            reply = "Trail ended. Contact admin for activation."
        else:
            store = load_store_data(phone_id_real)
            system_instruction = f"You are a sales assistant. Store: {json.dumps(store)}"
            config = types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.3)
            
            try: 
                rag_db = get_client_rag(phone_id_real)
                context = rag_db.search(msg, top_k=3)
            except: 
                context = ""
                
            history_text = "\n".join(chat_histories[num][-6:])
            full_prompt = f"Chat history:\n{history_text}\n\nContext from catalogue:\n{context}\n\nUser: {msg}"
            
            response = client.models.generate_content(model="gemini-2.5-flash", contents=full_prompt, config=config)
            reply = response.text.replace("**", "").replace("*", "").strip()
            
            chat_histories[num].append(f"User: {msg}")
            chat_histories[num].append(f"Bot: {reply}")
            if len(chat_histories[num]) > 20: 
                chat_histories[num] = chat_histories[num][-20:]

        requests.post(
            f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages", 
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, 
            json={"messaging_product": "whatsapp", "to": num, "text": {"body": reply}}
        )
    except Exception as e: 
        print(e)
    return "ok", 200

@app.route('/add-client', methods=['POST'])
def add_client():
    data = request.get_json(silent=True)
    if not data or data.get("key") != ADMIN_KEY: return jsonify({"error": "wrong key"}), 403
    clients = load_clients()
    clients.append({
        "phone_id": data["phone_id"],
        "whatsapp_token": data["whatsapp_token"],
        "store_name": data.get("store_name", "store"),
        "expiry": (datetime.now() + timedelta(days=int(data.get("days", 30)))).isoformat()
    })
    save_clients(clients)
    return jsonify({"ok": True, "count": len(clients)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port) 
