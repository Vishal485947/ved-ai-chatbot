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
Keep replies concise by default: use 3-6 short sentences or a few clear bullets.
Give longer step-by-step detail only when the user asks for it.
If the user asks who created you, who made you, who your creator is, or any
similar question, answer exactly: My creator is Vishal Raj,a student of class X B  SPSTDSC
Older conversation context may be summarized to save tokens. Use the summary for
continuity, but rely on recent messages for exact wording and ask a clarifying
question if important details are missing.
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


def smart_real_time_search_enabled():
    value = os.getenv("SMART_REAL_TIME_SEARCH", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def needs_real_time_search(message):
    normalized = f" {message.lower()} "
    current_year = str(datetime.now().year)
    search_triggers = [
        "latest",
        "current",
        "currently",
        "right now",
        "today",
        "todays",
        "tomorrow",
        "yesterday",
        "this week",
        "this month",
        "this year",
        "now",
        "recent",
        "recently",
        "new",
        "news",
        "live",
        "update",
        "updates",
        "weather",
        "temperature",
        "forecast",
        "score",
        "match",
        "fixture",
        "standings",
        "price",
        "stock",
        "share price",
        "crypto",
        "bitcoin",
        "exchange rate",
        "gold rate",
        "release date",
        "available now",
        "near me",
        "search web",
        "search google",
        "look up",
    ]

    if current_year in normalized:
        return True

    return any(trigger in normalized for trigger in search_triggers)


def should_use_real_time_search(message):
    if not real_time_search_enabled():
        return False
    if not smart_real_time_search_enabled():
        return True
    return needs_real_time_search(message)


def env_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default

    return max(minimum, min(maximum, value))


def compact_text(value, max_chars):
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def format_history_line(item, message_limit):
    if not isinstance(item, dict):
        return ""

    role = item.get("role")
    content = compact_text(item.get("content"), message_limit)
    if role not in {"user", "assistant"} or not content:
        return ""

    speaker = "User" if role == "user" else "Ved"
    return f"{speaker}: {content}"


def summarize_history_items(history, summary_limit, message_limit):
    lines = []
    for item in history:
        line = format_history_line(item, message_limit)
        if line:
            lines.append(f"- {line}")

    return compact_text("\n".join(lines), summary_limit)


def build_conversation_context(history, user_message, context_summary):
    recent_count = env_int("PROMPT_RECENT_MESSAGES", 8, 2, 20)
    summary_limit = env_int("PROMPT_SUMMARY_CHARS", 1600, 300, 5000)
    message_limit = env_int("PROMPT_MESSAGE_CHARS", 700, 120, 2000)

    older_history = history[:-recent_count]
    recent_history = history[-recent_count:]
    summary = compact_text(context_summary, summary_limit)

    if not summary and older_history:
        summary = summarize_history_items(older_history, summary_limit, message_limit)

    context = []
    if summary:
        context.append(f"Earlier conversation summary:\n{summary}")

    recent_lines = [
        line
        for line in (format_history_line(item, message_limit) for item in recent_history)
        if line
    ]
    if recent_lines:
        context.append("Recent conversation:\n" + "\n".join(recent_lines))

    context.append(f"User: {compact_text(user_message, message_limit)}")
    return context


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
For normal questions, answer directly without needing live search. For current events,
recent facts, news, prices, sports, releases, or anything likely to have changed
recently, use Google Search grounding when available. Do not say your knowledge
cutoff. Do not prefix replies with "Ved:".
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


def is_gemini_unavailable_error(error_text):
    return (
        "503" in error_text
        or "unavailable" in error_text
        or "high demand" in error_text
        or "overloaded" in error_text
    )


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
    context_summary = data.get("contextSummary") or ""
    timezone_name = data.get("timezone") or "UTC"
    if not isinstance(history, list):
        history = []

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

    conversation = build_conversation_context(history, user_message, context_summary)

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
        unavailable_models = []

        for model in model_candidates:
            try:
                config_options = {
                    "system_instruction": build_system_prompt(timezone_name),
                    "max_output_tokens": env_int("MAX_OUTPUT_TOKENS", 700, 120, 2000),
                }
                if should_use_real_time_search(user_message):
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
                if is_gemini_unavailable_error(error_text):
                    unavailable_models.append(model)
                    continue
                raise
        else:
            if unavailable_models and len(unavailable_models) == len(model_candidates):
                return jsonify({
                    "error": "Gemini is temporarily overloaded. Ved is switching to the backup AI if available; otherwise, please try again in a minute."
                }), 503

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

        if is_gemini_unavailable_error(error_text):
            return jsonify({
                "error": "Gemini is temporarily overloaded. Ved is switching to the backup AI if available; otherwise, please try again in a minute."
            }), 503

        if "not_found" in error_text or "not found" in error_text or "404" in error_text:
            return jsonify({
                "error": "The Gemini model name is not available. In your .env file, use GEMINI_MODEL=gemini-2.0-flash or GEMINI_MODEL=gemini-2.0-flash-lite."
            }), 404

        return jsonify({"error": f"Ved could not reply right now: {exc}"}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
