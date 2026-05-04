# Ved AI Chatbot

Ved is a simple AI chatbot web app built with Python, Flask, HTML, CSS, and JavaScript.
It supports Google login, saved browser-side conversations, real-time search grounding,
voice input, spoken replies for voice messages, and a browser-side Puter.js backup
AI if Gemini is temporarily rate-limited.

## Files

- `app.py` starts the Flask server and talks to the AI model.
- `templates/index.html` creates the chatbot page.
- `static/styles.css` styles the page.
- `static/script.js` sends messages to the Python backend.
- `requirements.txt` lists the Python packages.

## Setup

1. Install Python from https://www.python.org/downloads/ if it is not already installed.
2. Open a terminal in this folder.
3. Create a virtual environment:

```powershell
python -m venv .venv
```

4. Activate it:

```powershell
.venv\Scripts\Activate.ps1
```

5. Install the packages:

```powershell
pip install -r requirements.txt
```

6. Create a file named `.env` in this folder and add your API key. It must be in the same folder as `app.py`:

```env
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash-lite
ENABLE_REAL_TIME_SEARCH=true
SMART_REAL_TIME_SEARCH=true
MAX_MESSAGES_PER_HOUR=20
MAX_OUTPUT_TOKENS=700
PROMPT_RECENT_MESSAGES=8
PROMPT_SUMMARY_CHARS=1600
PROMPT_MESSAGE_CHARS=700
SECRET_KEY=replace_with_a_long_random_secret
GOOGLE_CLIENT_ID=your_google_oauth_client_id
GOOGLE_CLIENT_SECRET=your_google_oauth_client_secret
```

7. Run the app:

```powershell
python app.py
```

8. Open this URL in your browser:

```text
http://127.0.0.1:5000
```

## How It Works

The webpage collects your message and sends it to `/chat`. The Python server receives
the message, adds Ved's personality prompt, asks the AI model for a reply, and sends
the reply back to the page.

To reduce token usage, Ved sends only the most recent messages exactly and compresses
older chat context into a short summary before calling Gemini. You can tune this with
`PROMPT_RECENT_MESSAGES`, `PROMPT_SUMMARY_CHARS`, and `PROMPT_MESSAGE_CHARS`.

Ved also keeps real-time search smart. With `ENABLE_REAL_TIME_SEARCH=true` and
`SMART_REAL_TIME_SEARCH=true`, Gemini Search grounding is used only for questions
that look current, live, or time-sensitive. Shorter replies are encouraged by the
system prompt and capped with `MAX_OUTPUT_TOKENS`.

If Gemini returns a quota or rate-limit error, the browser tries Puter.js as a
backup AI. For live or current questions, the fallback uses Puter web search instead
of showing a "try again later" message. Gemini remains the primary model.

Voice input uses the browser's speech recognition APIs. Spoken replies use the
browser's speech synthesis APIs. These features work best in Chrome and Edge.

## Make Ved 24/7 Live

The temporary localhost link only works while your computer and tunnel are running.
For a real public chatbot, deploy Ved to a cloud web service.

### Render Setup

1. Put this project in a GitHub repository.
2. Go to https://render.com and create a Web Service.
3. Connect your GitHub repository.
4. Use these settings:

```text
Build Command: pip install -r requirements.txt
Start Command: gunicorn app:app
```

5. Add these environment variables in Render:

```env
GEMINI_API_KEY=your_real_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash-lite
ENABLE_REAL_TIME_SEARCH=true
SMART_REAL_TIME_SEARCH=true
MAX_MESSAGES_PER_HOUR=20
MAX_OUTPUT_TOKENS=700
PROMPT_RECENT_MESSAGES=8
PROMPT_SUMMARY_CHARS=1600
PROMPT_MESSAGE_CHARS=700
SECRET_KEY=use_a_long_random_secret
GOOGLE_CLIENT_ID=your_google_oauth_client_id
GOOGLE_CLIENT_SECRET=your_google_oauth_client_secret
```

`ENABLE_REAL_TIME_SEARCH=true` lets Ved use Gemini Grounding with Google Search
for current information and source links. `SMART_REAL_TIME_SEARCH=true` saves quota
by using search only for live or time-sensitive questions. Search grounding can
affect API usage or billing depending on your Gemini plan.

6. Deploy the service.

### Google Login Setup

Create a Google OAuth web client in Google Cloud Console. Add these authorized
redirect URIs:

```text
http://127.0.0.1:5000/auth/google/callback
https://your-render-url.onrender.com/auth/google/callback
```

Put the OAuth client ID and client secret into Render as environment variables:

```env
GOOGLE_CLIENT_ID=your_google_oauth_client_id
GOOGLE_CLIENT_SECRET=your_google_oauth_client_secret
```

Render will give you a public URL like:

```text
https://ved-ai-chatbot.onrender.com
```

Use a paid always-on instance if you want it to stay awake 24/7. Free services can
spin down after inactivity.
