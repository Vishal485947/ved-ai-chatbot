import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import jsonify, redirect, render_template, request, session, url_for
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

SYSTEM_PROMPT = """
You are Ved, a friendly AI chatbot. Explain things clearly, keep answers useful,
and ask a short follow-up question when it helps the user.
If the user asks who created you, who made you, who your creator is, or any
similar question, answer exactly: My creator is Vishal Raj,a student of class X B  SPSTDSC
"""

FALLBACK_GEMINI_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

VISITOR_MESSAGE_LOG = {}


def visitor_ip():
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limit_exceeded():
    max_messages = int(os.getenv("MAX_MESSAGES_PER_HOUR", "20"))
    now = time.time()
    one_hour_ago = now - 3600
    ip_address = visitor_ip()
    recent_messages = [
        timestamp
        for timestamp in VISITOR_MESSAGE_LOG.get(ip_address, [])
        if timestamp > one_hour_ago
    ]

    if len(recent_messages) >= max_messages:
        VISITOR_MESSAGE_LOG[ip_address] = recent_messages
        return True

    recent_messages.append(now)
    VISITOR_MESSAGE_LOG[ip_address] = recent_messages
    return False


def real_time_search_enabled():
    value = os.getenv("ENABLE_REAL_TIME_SEARCH", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def object_to_dict(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return {}


def extract_grounding(response):
    sources = []
    seen_sources = set()
    search_html = ""

    for candidate in getattr(response, "candidates", []) or []:
        metadata = (
            getattr(candidate, "grounding_metadata", None)
            or getattr(candidate, "groundingMetadata", None)
        )
        metadata = object_to_dict(metadata)

        entry_point = (
            metadata.get("search_entry_point")
            or metadata.get("searchEntryPoint")
            or {}
        )
        search_html = (
            entry_point.get("rendered_content")
            or entry_point.get("renderedContent")
            or search_html
        )

        chunks = metadata.get("grounding_chunks") or metadata.get("groundingChunks") or []
        for chunk in chunks:
            web = object_to_dict(chunk).get("web") or {}
            uri = web.get("uri")
            if not uri or uri in seen_sources:
                continue

            sources.append({
                "title": web.get("title") or uri,
                "uri": uri,
            })
            seen_sources.add(uri)

            if len(sources) >= 5:
                break

    return {"sources": sources, "searchHtml": search_html}


def user_timezone(timezone_name):
    if not timezone_name or not isinstance(timezone_name, str) or len(timezone_name) > 80:
        timezone_name = "UTC"

    try:
        return timezone_name, ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return "UTC", timezone.utc


def build_system_prompt(timezone_name):
    timezone_label, tzinfo = user_timezone(timezone_name)
    user_now = datetime.now(tzinfo)
    utc_now = datetime.now(timezone.utc)

    return f"""
{SYSTEM_PROMPT.strip()}

Current date and time for the user: {user_now.strftime("%A, %B %d, %Y at %H:%M")} ({timezone_label}).
Current UTC date and time: {utc_now.strftime("%A, %B %d, %Y at %H:%M")} (UTC).

If the user asks for today's date or current time, use the current date/time above.
For current events, recent facts, news, prices, sports, releases, or anything likely
to have changed recently, use Google Search grounding when available. Do not say your
knowledge cutoff. Do not prefix replies with "Ved:".
"""


def asks_about_creator(message):
    normalized = message.lower()
    creator_phrases = [
        "who is your creator",
        "who created you",
        "who made you",
        "who is your developer",
        "who developed you",
        "who built you",
        "your creator",
        "your maker",
    ]
    return any(phrase in normalized for phrase in creator_phrases)


@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/")
def home():
    return render_template("index.html", user=session.get("user"))


@app.get("/ved")
def ved_home():
    return render_template("index.html", user=session.get("user"))


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "app": "Ved"})


@app.get("/login")
def login():
    if not os.getenv("GOOGLE_CLIENT_ID") or not os.getenv("GOOGLE_CLIENT_SECRET"):
        return render_template(
            "index.html",
            user=None,
            auth_error="Google login is not configured yet. Add GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and SECRET_KEY in your hosting environment.",
        )

    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.get("/auth/google/callback")
def google_callback():
    token = oauth.google.authorize_access_token()
    user_info = token.get("userinfo")
    if not user_info:
        user_info = oauth.google.userinfo(token=token)

    session["user"] = {
        "name": user_info.get("name") or "there",
        "email": user_info.get("email"),
        "picture": user_info.get("picture"),
    }
    return redirect(url_for("home"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.post("/chat")
def chat():
    if not session.get("user"):
        return jsonify({
            "error": "Please log in with Google before chatting with Ved."
        }), 401

    load_dotenv(ENV_FILE, override=True)
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()

    if not api_key or api_key == "your_gemini_api_key_here":
        return jsonify({
            "error": f"Ved needs a Gemini API key. Add GEMINI_API_KEY=your_key_here to {ENV_FILE}, then send a new message."
        }), 500

    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    history = data.get("history") or []
    timezone_name = data.get("timezone") or "UTC"

    if not user_message:
        return jsonify({"error": "Please type a message."}), 400

    if asks_about_creator(user_message):
        return jsonify({
            "reply": "My creator is Vishal Raj,a student of class X B  SPSTDSC",
            "sources": [],
            "searchHtml": "",
        })

    if rate_limit_exceeded():
        return jsonify({
            "error": "Ved is getting a lot of messages from this visitor. Please wait before sending more."
        }), 429

    conversation = []

    for item in history[-10:]:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and content:
            speaker = "User" if role == "user" else "Ved"
            conversation.append(f"{speaker}: {content}")

    conversation.append(f"User: {user_message}")

    try:
        try:
            from google import genai
            from google.genai import types
        except ModuleNotFoundError:
            return jsonify({
                "error": "The Gemini Python package is missing. Run pip install -r requirements.txt, then restart the server."
            }), 500

        client = genai.Client(api_key=api_key)
        preferred_model = (os.getenv("GEMINI_MODEL") or "").strip()
        model_candidates = [
            model for model in [preferred_model, *FALLBACK_GEMINI_MODELS] if model
        ]
        model_candidates = list(dict.fromkeys(model_candidates))

        last_error = None
        not_found_models = []

        for model in model_candidates:
            try:
                config_options = {
                    "system_instruction": build_system_prompt(timezone_name)
                }
                if real_time_search_enabled():
                    config_options["tools"] = [
                        types.Tool(google_search=types.GoogleSearch())
                    ]

                response = client.models.generate_content(
                    model=model,
                    contents="\n".join(conversation),
                    config=types.GenerateContentConfig(**config_options),
                )
                break
            except Exception as exc:
                error_text = str(exc).lower()
                last_error = exc
                if "not_found" in error_text or "not found" in error_text or "404" in error_text:
                    not_found_models.append(model)
                    continue
                raise
        else:
            tried = ", ".join(not_found_models or model_candidates)
            return jsonify({
                "error": f"None of these Gemini models were available for your API key: {tried}. Open Google AI Studio, check the model list for your project, and put one supported text model in GEMINI_MODEL."
            }), 404

        answer = (response.text or "").strip()
        if not answer:
            answer = "I could not generate a reply for that. Please try asking another way."
        return jsonify({"reply": answer, **extract_grounding(response)})
    except Exception as exc:
        error_text = str(exc).lower()
        if "api key" in error_text or "unauthenticated" in error_text:
            return jsonify({
                "error": "The Gemini API key is not valid. Check that GEMINI_API_KEY in your .env file is copied correctly."
            }), 401

        if "quota" in error_text or "429" in error_text:
            return jsonify({
                "error": "Your Gemini API key works, but Gemini returned a quota or rate-limit error. Wait a minute and try again, or check Usage & Billing in Google AI Studio."
            }), 429

        if "not_found" in error_text or "not found" in error_text or "404" in error_text:
            return jsonify({
                "error": "The Gemini model name is not available. In your .env file, use GEMINI_MODEL=gemini-2.0-flash or GEMINI_MODEL=gemini-2.0-flash-lite."
            }), 404

        return jsonify({"error": f"Ved could not reply right now: {exc}"}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
