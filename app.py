import base64
import binascii
import ipaddress
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
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

MAX_ATTACHMENTS = 4
MAX_INLINE_ATTACHMENT_BYTES = 6 * 1024 * 1024
MAX_LINK_FETCH_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENT_TEXT_CHARS = 60000
SUPPORTED_INLINE_MIME_PREFIXES = ()
SUPPORTED_INLINE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/heic",
    "image/heif",
    "application/pdf",
}


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
        "election",
        "elections",
        "election result",
        "election results",
        "vote count",
        "votes",
        "poll",
        "polls",
        "exit poll",
        "winner",
        "leading",
        "results",
        "headlines",
        "breaking",
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


def merge_sources(*source_lists):
    merged = []
    seen = set()
    for source_list in source_lists:
        for source in source_list or []:
            uri = source.get("uri") if isinstance(source, dict) else ""
            if not uri or uri in seen:
                continue
            merged.append(source)
            seen.add(uri)
            if len(merged) >= 8:
                return merged
    return merged


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


def fetch_json(url, timeout=8):
    request = Request(url, headers={"User-Agent": "VedAIChatbot/1.0"})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def is_supported_inline_mime(mime_type):
    mime = (mime_type or "").split(";")[0].strip().lower()
    return mime in SUPPORTED_INLINE_MIME_TYPES or any(
        mime.startswith(prefix) for prefix in SUPPORTED_INLINE_MIME_PREFIXES
    )


def is_text_mime(mime_type):
    mime = (mime_type or "").split(";")[0].strip().lower()
    return mime.startswith("text/") or mime in {
        "application/json",
        "application/xml",
        "application/javascript",
    }


def safe_link_url(value):
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return ""

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    hostname = (parsed.hostname or "").lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return ""
    try:
        address = ipaddress.ip_address(hostname)
        if address.is_private or address.is_loopback or address.is_link_local:
            return ""
    except ValueError:
        pass
    return parsed.geturl()


def strip_html(value):
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return " ".join(text.split())


def fetch_link_for_attachment(url):
    request = Request(url, headers={"User-Agent": "VedAIChatbot/1.0"})
    with urlopen(request, timeout=8) as response:
        content_type = response.headers.get("Content-Type", "text/plain")
        mime_type = content_type.split(";")[0].strip().lower() or "text/plain"
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read(MAX_LINK_FETCH_BYTES + 1)

    if len(payload) > MAX_LINK_FETCH_BYTES:
        raise ValueError("Linked content is too large.")

    if is_supported_inline_mime(mime_type):
        return {"mimeType": mime_type, "bytes": payload}

    if not is_text_mime(mime_type):
        raise ValueError(f"Unsupported linked content type {mime_type}.")

    text = payload.decode(charset, errors="replace")
    if "html" in mime_type:
        text = strip_html(text)
    return {"mimeType": mime_type, "text": compact_text(text, MAX_ATTACHMENT_TEXT_CHARS)}


def attachment_label(attachment):
    return compact_text(
        attachment.get("name") or attachment.get("url") or "attachment",
        160,
    )


def build_attachment_context(attachments, types):
    if not isinstance(attachments, list):
        return "", []

    context_lines = []
    parts = []

    for index, attachment in enumerate(attachments[:MAX_ATTACHMENTS], start=1):
        if not isinstance(attachment, dict):
            continue

        attachment_type = attachment.get("type")
        name = attachment_label(attachment)

        if attachment_type == "text":
            text = compact_text(attachment.get("text"), MAX_ATTACHMENT_TEXT_CHARS)
            if text:
                context_lines.append(f"Attachment {index} ({name}):\n{text}")
            continue

        if attachment_type == "link":
            url = safe_link_url(attachment.get("url"))
            if not url:
                context_lines.append(f"Attachment {index}: skipped invalid link.")
                continue

            try:
                linked = fetch_link_for_attachment(url)
            except Exception as exc:
                context_lines.append(f"Attachment {index} ({url}): could not fetch link: {exc}")
                continue

            if linked.get("bytes"):
                parts.append(types.Part.from_bytes(
                    data=linked["bytes"],
                    mime_type=linked["mimeType"],
                ))
                context_lines.append(f"Attachment {index}: linked file from {url} ({linked['mimeType']}).")
            elif linked.get("text"):
                context_lines.append(f"Attachment {index}: linked page {url}\n{linked['text']}")
            continue

        if attachment_type == "inline":
            mime_type = (attachment.get("mimeType") or "application/octet-stream").split(";")[0].strip().lower()
            if not is_supported_inline_mime(mime_type):
                context_lines.append(f"Attachment {index} ({name}): unsupported file type {mime_type}.")
                continue

            try:
                file_bytes = base64.b64decode(attachment.get("data") or "", validate=True)
            except (binascii.Error, ValueError):
                context_lines.append(f"Attachment {index} ({name}): invalid file data.")
                continue

            if len(file_bytes) > MAX_INLINE_ATTACHMENT_BYTES:
                context_lines.append(f"Attachment {index} ({name}): file is too large.")
                continue

            parts.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
            context_lines.append(f"Attachment {index}: {name} ({mime_type}).")

    if context_lines:
        context_lines.insert(
            0,
            "Use the user's attachments below when they ask about files, images, documents, or links.",
        )

    return "\n\n".join(context_lines), parts


def weather_code_description(code):
    descriptions = {
        0: "clear sky",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "rime fog",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "dense drizzle",
        61: "slight rain",
        63: "moderate rain",
        65: "heavy rain",
        71: "slight snow",
        73: "moderate snow",
        75: "heavy snow",
        80: "slight rain showers",
        81: "moderate rain showers",
        82: "violent rain showers",
        95: "thunderstorm",
        96: "thunderstorm with hail",
        99: "thunderstorm with heavy hail",
    }
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        return "conditions unavailable"
    return descriptions.get(numeric_code, "mixed conditions")


def format_number(value, decimals=0):
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "n/a"


def clean_unit(value, fallback):
    return str(value or fallback).replace(chr(176), "").strip()


def is_weather_query(message):
    normalized = " " + re.sub(r"[^a-z0-9]+", " ", message.lower()) + " "
    weather_terms = [
        " weather ",
        " forecast ",
        " temperature ",
        " rain ",
        " raining ",
        " humidity ",
        " wind ",
        " cloudy ",
        " sunny ",
        " thunderstorm ",
    ]
    return any(term in normalized for term in weather_terms)


def extract_weather_location(message):
    text = re.sub(
        r"\b(weather|forecast|temperature|rain|raining|humidity|wind|cloudy|sunny|thunderstorm|showers?|precipitation|precip|chance|chances|probability|possibility|risk)\b",
        " ",
        message,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(what'?s|what is|tell me|show me|give me|today|tomorrow|now|current|right now|in|for|at|near|please|will it|is it|the|of)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[^a-zA-Z0-9,\s-]", " ", text)
    return " ".join(text.split()).strip(" ,-")


def weather_location_candidates(location_query):
    candidates = []
    seen = set()

    def add(value):
        cleaned = " ".join(str(value or "").replace(",", " ").split()).strip(" ,-")
        if cleaned and cleaned.lower() not in seen:
            candidates.append(cleaned)
            seen.add(cleaned.lower())

    add(location_query)

    words = location_query.split()
    if len(words) > 1:
        for end in range(len(words) - 1, 0, -1):
            add(" ".join(words[:end]))
        for start in range(1, len(words)):
            add(" ".join(words[start:]))

    return candidates


def geocode_weather_location(location_query):
    last_url = ""
    for candidate in weather_location_candidates(location_query):
        geocode_params = urlencode({
            "name": candidate,
            "count": 1,
            "language": "en",
            "format": "json",
        })
        geocode_url = f"https://geocoding-api.open-meteo.com/v1/search?{geocode_params}"
        last_url = geocode_url
        geocode_data = fetch_json(geocode_url)
        results = geocode_data.get("results") or []
        if results:
            return results[0], geocode_url, candidate

    return None, last_url, location_query


def build_weather_forecast_reply(message):
    if not is_weather_query(message):
        return None

    location_query = extract_weather_location(message)
    if not location_query:
        return {
            "reply": "Which city or place should I check the weather for?",
            "sources": [],
        }

    location, geocode_url, matched_query = geocode_weather_location(location_query)
    if not location:
        return {
            "reply": f"I could not find a weather location for {location_query}. Try a nearby city name.",
            "sources": [{"title": "Open-Meteo Geocoding", "uri": geocode_url}],
        }

    latitude = location.get("latitude")
    longitude = location.get("longitude")
    label = ", ".join(
        part for part in [
            location.get("name"),
            location.get("admin1"),
            location.get("country"),
        ]
        if part
    )

    forecast_params = urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
        "forecast_days": 5,
        "timezone": "auto",
    })
    forecast_url = f"https://api.open-meteo.com/v1/forecast?{forecast_params}"
    forecast_data = fetch_json(forecast_url)
    current = forecast_data.get("current") or {}
    current_units = forecast_data.get("current_units") or {}
    daily = forecast_data.get("daily") or {}

    temp_unit = clean_unit(current_units.get("temperature_2m"), "C")
    wind_unit = clean_unit(current_units.get("wind_speed_10m"), "km/h")
    current_description = weather_code_description(current.get("weather_code"))
    lines = [
        f"Weather for {label}: {format_number(current.get('temperature_2m'), 1)} degrees {temp_unit} and {current_description}.",
        f"Feels like {format_number(current.get('apparent_temperature'), 1)} degrees {temp_unit}; humidity {format_number(current.get('relative_humidity_2m'))}%; wind {format_number(current.get('wind_speed_10m'))} {wind_unit}.",
    ]

    times = daily.get("time") or []
    max_temps = daily.get("temperature_2m_max") or []
    min_temps = daily.get("temperature_2m_min") or []
    rain_chances = daily.get("precipitation_probability_max") or []
    weather_codes = daily.get("weather_code") or []

    forecast_lines = []
    for index, date_text in enumerate(times[:5]):
        try:
            day_label = datetime.fromisoformat(date_text).strftime("%a, %b %d")
        except ValueError:
            day_label = date_text
        max_temp = max_temps[index] if index < len(max_temps) else None
        min_temp = min_temps[index] if index < len(min_temps) else None
        weather_code = weather_codes[index] if index < len(weather_codes) else None
        rain_chance = rain_chances[index] if index < len(rain_chances) else None
        forecast_lines.append(
            f"- {day_label}: {format_number(max_temp, 1)}/{format_number(min_temp, 1)} degrees, {weather_code_description(weather_code)}, rain chance {format_number(rain_chance)}%"
        )

    if forecast_lines:
        lines.append("5-day forecast:\n" + "\n".join(forecast_lines))

    if matched_query.lower() != location_query.lower():
        lines.append(f"I searched for {matched_query} because {location_query} was not an exact weather-location match.")

    return {
        "reply": "\n".join(lines),
        "sources": [
            {"title": "Open-Meteo Forecast", "uri": forecast_url},
            {"title": "Open-Meteo Geocoding", "uri": geocode_url},
        ],
    }


def is_live_news_query(message):
    if is_weather_query(message):
        return False
    normalized = " " + re.sub(r"[^a-z0-9]+", " ", message.lower()) + " "
    live_news_terms = [
        " news ",
        " headline",
        " breaking ",
        " election",
        " vote count",
        " election result",
        " election results",
        " exit poll",
        " who won",
        " winner",
        " leading",
    ]
    return any(term in normalized for term in live_news_terms)


def extract_live_news_query(message):
    text = re.sub(
        r"\b(latest|current|today|now|right now|show me|tell me|give me|updates?|headlines?|news|breaking|live|please)\b",
        " ",
        message,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[^a-zA-Z0-9,\s-]", " ", text)
    query = " ".join(text.split()).strip(" ,-")
    return compact_text(query or message or "breaking news", 180)


def fetch_live_news_articles(message, max_records=5):
    if not is_live_news_query(message):
        return "", [], []

    query = extract_live_news_query(message)
    gdelt_params = urlencode({
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records,
        "sort": "datedesc",
        "timespan": "2d",
    })
    gdelt_url = f"https://api.gdeltproject.org/api/v2/doc/doc?{gdelt_params}"
    data = fetch_json(gdelt_url)
    raw_articles = data.get("articles") or []
    articles = []

    for item in raw_articles[:max_records]:
        url = item.get("url")
        title = compact_text(item.get("title"), 180)
        if not url or not title:
            continue
        articles.append({
            "title": title,
            "uri": url,
            "domain": item.get("domain") or "",
            "seenDate": item.get("seendate") or item.get("seenDate") or "",
            "sourceCountry": item.get("sourcecountry") or item.get("sourceCountry") or "",
        })

    sources = [{"title": article["title"], "uri": article["uri"]} for article in articles]
    if articles:
        sources.append({"title": "GDELT DOC 2.0", "uri": gdelt_url})

    return query, articles, sources


def build_live_news_context(query, articles):
    if not articles:
        return ""

    lines = [f"Recent live news context for query: {query}"]
    for article in articles:
        source_bits = ", ".join(
            bit for bit in [article.get("domain"), article.get("sourceCountry"), article.get("seenDate")] if bit
        )
        lines.append(f"- {article['title']} ({source_bits}): {article['uri']}")
    return "\n".join(lines)


def format_live_news_fallback(query, articles):
    if not articles:
        return "I could not fetch fresh live results right now. Please try again in a minute."

    lines = [f"I found these recent live sources for {query}:"]
    for index, article in enumerate(articles, start=1):
        source = article.get("domain") or "source"
        seen = article.get("seenDate") or "recent"
        lines.append(f"{index}. {article['title']} ({source}, {seen})")
    lines.append("Open the source links for the latest details, because live news and election counts can change quickly.")
    return "\n".join(lines)


def quick_local_reply(message, user_name):
    normalized = "".join(
        character.lower() if character.isalnum() or character.isspace() else " "
        for character in message
    )
    words = [word for word in normalized.split() if word not in {"ved", "assistant"}]
    greeting_words = {"hi", "hello", "hey", "hii", "helo", "namaste"}

    if words and len(words) <= 3 and all(word in greeting_words for word in words):
        name = (user_name or "there").split()[0]
        return f"Hi {name}! I am here. What would you like help with today?"

    return ""


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
    attachments = data.get("attachments") or []
    if not isinstance(history, list):
        history = []
    if not isinstance(attachments, list):
        attachments = []

    if not user_message:
        return jsonify({"error": "Please type a message."}), 400

    if asks_about_creator(user_message):
        return jsonify({
            "reply": "My creator is Vishal Raj,a student of class X B  SPSTDSC",
            "sources": [],
            "searchHtml": "",
        })

    local_reply = "" if attachments else quick_local_reply(user_message, session.get("user", {}).get("name"))
    if local_reply:
        return jsonify({
            "reply": local_reply,
            "sources": [],
            "searchHtml": "",
        })

    if is_weather_query(user_message) and not attachments:
        try:
            weather_result = build_weather_forecast_reply(user_message)
        except Exception:
            weather_result = {
                "reply": "I could not fetch the live weather forecast right now. Please try again in a minute.",
                "sources": [],
            }

        return jsonify({
            "reply": weather_result["reply"],
            "sources": weather_result.get("sources", []),
            "searchHtml": "",
        })

    if rate_limit_exceeded():
        return jsonify({
            "error": "Ved is getting a lot of messages from this visitor. Please wait before sending more."
        }), 429

    conversation = build_conversation_context(history, user_message, context_summary)
    live_news_query = ""
    live_news_articles = []
    live_news_sources = []
    if is_live_news_query(user_message):
        try:
            live_news_query, live_news_articles, live_news_sources = fetch_live_news_articles(user_message)
        except Exception:
            live_news_query = extract_live_news_query(user_message)
            live_news_articles = []
            live_news_sources = []

        live_news_context = build_live_news_context(live_news_query, live_news_articles)
        if live_news_context:
            conversation.insert(-1, live_news_context)

    try:
        try:
            from google import genai
            from google.genai import types
        except ModuleNotFoundError:
            return jsonify({
                "error": "The Gemini Python package is missing. Run pip install -r requirements.txt, then restart the server."
            }), 500

        attachment_context, attachment_parts = build_attachment_context(attachments, types)
        if attachment_context:
            conversation.insert(-1, attachment_context)
        gemini_contents = (
            [*attachment_parts, "\n".join(conversation)]
            if attachment_parts
            else "\n".join(conversation)
        )

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
                    contents=gemini_contents,
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
            known_failures = len(unavailable_models) + len(not_found_models)
            if unavailable_models and known_failures == len(model_candidates):
                if live_news_articles:
                    return jsonify({
                        "reply": format_live_news_fallback(live_news_query, live_news_articles),
                        "sources": live_news_sources,
                        "searchHtml": "",
                    })

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
        grounding = extract_grounding(response)
        return jsonify({
            "reply": answer,
            "sources": merge_sources(grounding.get("sources"), live_news_sources),
            "searchHtml": grounding.get("searchHtml", ""),
        })
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
            if live_news_articles:
                return jsonify({
                    "reply": format_live_news_fallback(live_news_query, live_news_articles),
                    "sources": live_news_sources,
                    "searchHtml": "",
                })

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
