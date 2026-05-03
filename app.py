import os

from openai import OpenAI
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = """
You are Ved, a friendly AI chatbot. Explain things clearly, keep answers useful,
and ask a short follow-up question when it helps the user.
"""


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/chat")
def chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    history = data.get("history") or []

    if not user_message:
        return jsonify({"error": "Please type a message."}), 400

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for item in history[-10:]:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
            messages=messages,
        )
        answer = response.choices[0].message.content
        return jsonify({"reply": answer})
    except Exception as exc:
        return jsonify({"error": f"Ved could not reply right now: {exc}"}), 500


if __name__ == "__main__":
    app.run(debug=True)