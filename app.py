import os 
import requests
from dotenv import load_dotenv 
load_dotenv()

from flask import Flask, jsonify, request, render_template

from google import genai

from google.genai import types

import json 

from datetime import datetime, timedelta

from rag_strong import build_or_load_db

from features import CURRENT_TIER_FEATURES

app = Flask(__name__)

API_KEY = os.environ.get("GEMINI_API_KEY")

VERIFY_TOKEN = "bot123"
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_ID = os.environ.get("PHONE_ID")


if not API_KEY:
    raise ValueError("set GEMINI_API_KEY in CMD")

RAG_DB = build_or_load_db("catalogue.pdf")

client = genai.Client(api_key=API_KEY)

chat_history = [] 

FILE = "subscription.json"
ADMIN_KEY = "Zero_Bot#!_Tlm9Xq2"

def save_expiry(date):
    with open(FILE, "w") as f:
        json.dump({"expiry": date.isoformat()}, f)

def get_expiry():
    if not os.path.exists(FILE):
        expiry = datetime.now() + timedelta(days=3)
        save_expiry(expiry)
        return expiry
    with open(FILE, "r") as f:
        data = json.load(f)
        return datetime.fromisoformat(data["expiry"])

def is_expired():
    return datetime.now() > get_expiry()

def activate_subscription(days=30):
    expiry = datetime.now() + timedelta(days=days)
    save_expiry(expiry)
    return expiry

def load_store_data():
    try:
        with open("store_data.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return{"products": [], "delivery": "58 wilaya", "payment": "COD"}
store = load_store_data()
SYSTEM_INSTRUCTION = f"You are a sales assistant. Store: {json.dumps(store)}"
config = types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.3)

@app.route("/")
def home():
    return render_template("chat.html")

@app.route("/chat", methods=["POST"])
def chat():
    global chat_history 
    if is_expired():
      return jsonify({"reply": "Trail ended. Contact admin for activation."})
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"reply": "Please send a message."})

    msg = data.get("message", "").strip()
    if not msg:
        return jsonify({"reply": "Please send a message."})
    
    try:
        context = RAG_DB.search(msg, top_k=3)

    except:
        context = ""
        history_text = "\n".join(chat_history[-6:])
    full_prompt = f"Chat history:\n{history_text}\n\nContext from catalogue:\n{context}\n\nUser: {msg}"
    response = client.models.generate_content(model="gemini-flash-latest", contents=full_prompt, config=config)
    clean_reply = response.text.replace("**", "").replace("*", "").strip()
    chat_history.append(f"User: {msg}")
    chat_history.append(f"Bot: {clean_reply}")
    if len(chat_history) > 20:
        chat_history = chat_history[-20:]
    return jsonify({"reply": clean_reply})

@app.route("/activate", methods=["POST"])
def activate():
    data = request.get_json(silent=True)
    if not data or data.get("key") != ADMIN_KEY:
        return jsonify({"error": "wrong key"}), 403
    days = data.get("days", 30)
    new_date = activate_subscription(days)
    return jsonify({"message": f"Activated until {new_date}"})

@app.route("/status", methods=["GET"])
def status():
    expiry = get_expiry()
    return jsonify({"expiry": expiry.isoformat(), "expired": is_expired()})
    
    @app.route('/webhook/whatsapp', methods=['GET'])
def verify_whatsapp():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "fail", 403

@app.route('/webhook/whatsapp', methods=['POST'])
def receive_whatsapp():
    data = request.get_json()
    try:
        msg = data['entry'][0]['changes'][0]['value']['messages'][0]['text']['body']
        num = data['entry'][0]['changes'][0]['value']['messages'][0]['from']

        if is_expired():
            reply = "Trail ended. Contact admin for activation."
        else:
            try: context = RAG_DB.search(msg, top_k=3)
            except: context = ""
            history_text = "\n".join(chat_history[-6:])
            full_prompt = f"Chat history:\n{history_text}\n\nContext from catalogue:\n{context}\n\nUser: {msg}"
            response = client.models.generate_content(model="gemini-flash-latest", contents=full_prompt, config=config)
            reply = response.text.replace("**", "").replace("*", "").strip()

        requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, json={"messaging_product": "whatsapp", "to": num, "text": {"body": reply}})
    except Exception as e:
        print(e)
    return "ok", 200

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)
