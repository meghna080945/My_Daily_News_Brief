#!/usr/bin/env python3
"""news_brief.py

Fetches recent articles from a set of RSS feeds, asks Gemini to produce a
three-sentence brief for each story, and upserts the results into a Supabase
"stories" table (deduplicated on source_url).

Environment variables (loaded from a .env file):
    GEMINI_API_KEY        - Google Generative AI API key
    SUPABASE_URL          - Supabase project URL
    SUPABASE_SERVICE_KEY  - Supabase service role key
"""

import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import certifi
import feedparser
from dotenv import load_dotenv
from google import genai
from google.genai import types
from supabase import create_client

# --- Feed configuration ------------------------------------------------------

FEEDS = {
    "AI": [
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://venturebeat.com/category/ai/feed/",
    ],
    "Tech": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.wired.com/wired/index",
    ],
    "Commerce": [
        "https://www.retaildive.com/feeds/news/",
        "https://www.modernretail.co/feed/",
    ],
    "Fintech": [
        "https://www.finextra.com/rss/headlines.aspx",
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    ],
    "Venture": [
        "https://techcrunch.com/category/venture/feed/",
    ],
    "Geopolitics": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.dw.com/rdf/rss-en-all",
        "https://feeds.npr.org/1004/rss.xml",
    ],
}

MAX_AGE = timedelta(hours=24)

# Color-coded category headings for the email brief.
CATEGORY_COLORS = {
    "AI": "#7c3aed",          # violet
    "Tech": "#2563eb",        # blue
    "Commerce": "#059669",    # green
    "Fintech": "#0d9488",     # teal
    "Venture": "#ea580c",     # orange
    "Geopolitics": "#dc2626", # red
}
DEFAULT_CATEGORY_COLOR = "#334155"  # slate, for any unexpected category

GEMINI_PROMPT = (
    "You are a sharp analyst briefing a founder. You will receive a list of "
    "articles, each tagged with a suggested category. Do the following:\n"
    "1. DEDUPLICATE: When several articles cover the same underlying event, "
    "keep only the single best one (most authoritative, complete, and recent) "
    "and discard the rest. Each real-world event must appear at most once.\n"
    "2. CATEGORIZE: Assign each kept story to exactly ONE category — the single "
    "most fitting one from this list: {categories}. The suggested category is "
    "only a hint; override it if another category fits better.\n"
    "3. SUMMARIZE: For each kept story, write exactly three lines in a "
    "question-and-answer form, each a single sentence, separated by newline "
    "characters (\\n). Use exactly these questions as the prefix of each line:\n"
    "   What happened: <one sentence>\n"
    "   Why it signals a bigger trend: <one sentence>\n"
    "   Why it matters now: <one sentence>\n"
    "Summarize ONLY what is stated in the source text; do not infer or add "
    "facts. Skip anything irrelevant.\n"
    "Return only valid JSON: an array of objects with fields headline, summary, "
    "source_url, source_name, category. Use the source_url of the single article "
    "you kept for each event."
)


# --- Helpers -----------------------------------------------------------------

def entry_datetime(entry):
    """Return a timezone-aware UTC datetime for the entry, or None."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return None


def clean_text(value):
    """Strip HTML tags and collapse whitespace from a summary/description."""
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", text).strip()


# Query params that identify campaigns/sessions rather than content. Anything
# matching these is dropped so the same article under different tracking tags
# collapses to one canonical URL.
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {
    "gclid", "fbclid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src",
    "cmpid", "source", "ncid", "spm", "cid", "at_medium", "at_campaign",
}


def canonical_url(url):
    """Normalize a URL so variants of the same article collapse to one key.

    Lowercases the scheme/host, drops a leading ``www.``, forces https, strips
    tracking query params (``utm_*``, ``gclid``, ...), sorts any remaining
    params, and removes the fragment and a trailing slash. Returns the input
    unchanged if it cannot be parsed as an http(s) URL.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return url

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith(_TRACKING_PARAM_PREFIXES)
                or k.lower() in _TRACKING_PARAMS)
    ]
    query = "&".join(f"{k}={v}" for k, v in sorted(kept))

    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


# Some feeds reject the default urllib user agent; present a browser-like one.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Reuse a single SSL context backed by certifi's CA bundle. Without this,
# feedparser's built-in urllib fetch fails with CERTIFICATE_VERIFY_FAILED on
# Python installs that lack system CA certs (common on macOS).
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _fetch_feed(url):
    """Fetch a feed URL over HTTPS with a certifi CA bundle and parse it.

    We download the bytes ourselves (rather than letting feedparser fetch)
    so we control the SSL context and user agent.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30, context=_SSL_CONTEXT) as response:
        raw = response.read()
    return feedparser.parse(raw)


def fetch_articles():
    """Fetch and return recent articles across all feeds.

    Each article is a dict with title, link, summary, source_name and
    category. Failures on a single feed are logged and skipped so one bad
    feed cannot crash the run.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - MAX_AGE
    articles = []

    for category, urls in FEEDS.items():
        for url in urls:
            try:
                parsed = _fetch_feed(url)
                if parsed.bozo and not parsed.entries:
                    raise RuntimeError(parsed.get("bozo_exception", "unknown parse error"))

                source_name = clean_text(parsed.feed.get("title")) or url
                kept = 0
                for entry in parsed.entries:
                    published = entry_datetime(entry)
                    # Skip anything older than 24h. If we cannot determine a
                    # date, skip it rather than risk including stale content.
                    if published is None or published < cutoff:
                        continue

                    link = canonical_url(entry.get("link"))
                    title = clean_text(entry.get("title"))
                    if not link or not title:
                        continue

                    articles.append(
                        {
                            "title": title,
                            "link": link,
                            "summary": clean_text(
                                entry.get("summary") or entry.get("description")
                            ),
                            "source_name": source_name,
                            "category": category,
                        }
                    )
                    kept += 1

                print(f"[ok] {category}: {kept} recent article(s) from {url}")
            except Exception as exc:  # noqa: BLE001 - one feed must not kill the run
                print(f"[warn] failed to fetch {url}: {exc}", file=sys.stderr)

    return articles


def summarize_with_gemini(client, articles):
    """Send articles to Gemini and return a list of story dicts."""
    if not articles:
        return []

    # Give the model a compact, structured view of the source text.
    payload = [
        {
            "headline": a["title"],
            "source_text": a["summary"],
            "source_url": a["link"],
            "source_name": a["source_name"],
            "category": a["category"],
        }
        for a in articles
    ]

    instructions = GEMINI_PROMPT.format(categories=", ".join(FEEDS.keys()))
    prompt = f"{instructions}\n\nArticles:\n{json.dumps(payload, ensure_ascii=False)}"

    try:
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        text = response.text.strip()
        stories = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"[error] Gemini did not return valid JSON: {exc}", file=sys.stderr)
        return []
    except Exception as exc:  # noqa: BLE001
        print(f"[error] Gemini request failed: {exc}", file=sys.stderr)
        return []

    if not isinstance(stories, list):
        print("[error] Gemini response was not a JSON array.", file=sys.stderr)
        return []

    # Keep only well-formed records, enforcing a valid single category and a
    # single record per source_url.
    valid = []
    seen_urls = set()
    allowed_categories = set(FEEDS.keys())
    required = {"headline", "summary", "source_url", "source_name", "category"}
    for story in stories:
        if not (isinstance(story, dict) and required.issubset(story.keys())):
            continue
        url = canonical_url(story.get("source_url"))
        category = story.get("category")
        if not url or url in seen_urls:
            continue
        story["source_url"] = url
        if category not in allowed_categories:
            print(
                f"[warn] dropping story with unknown category {category!r}: {url}",
                file=sys.stderr,
            )
            continue
        seen_urls.add(url)
        valid.append({field: story[field] for field in required})
    return valid


def select_unsent_stories(supabase, stories):
    """Return the subset of stories not already emailed in the last 24 hours.

    A story counts as already sent if the 'stories' table already holds a row
    with the same source_url whose created_at falls within MAX_AGE. This must
    be called BEFORE upsert_stories, otherwise the upsert stamps every story
    with a fresh created_at and they all look already-sent.

    On any query failure we fail open (return all stories): missing a brief is
    worse than an occasional duplicate.
    """
    if not stories:
        return []

    urls = [story["source_url"] for story in stories]
    cutoff = (datetime.now(timezone.utc) - MAX_AGE).isoformat()
    try:
        resp = (
            supabase.table("stories")
            .select("source_url")
            .in_("source_url", urls)
            .gte("created_at", cutoff)
            .execute()
        )
        already_sent = {row["source_url"] for row in resp.data}
    except Exception as exc:  # noqa: BLE001
        print(
            f"[warn] could not check for already-sent stories, sending all: {exc}",
            file=sys.stderr,
        )
        return stories

    return [story for story in stories if story["source_url"] not in already_sent]


def upsert_stories(supabase, stories):
    """Upsert stories into the Supabase 'stories' table, deduped on source_url."""
    if not stories:
        return 0

    # Postgres rejects an upsert that touches the same conflict key twice in one
    # command, so collapse duplicate source_urls (keeping the last occurrence).
    deduped = {story["source_url"]: story for story in stories}
    stories = list(deduped.values())

    try:
        supabase.table("stories").upsert(stories, on_conflict="source_url").execute()
        return len(stories)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] Supabase upsert failed: {exc}", file=sys.stderr)
        return 0


# --- Email -------------------------------------------------------------------

def render_summary(summary):
    """Render a Q&A summary (one ``Question: answer`` per line) as HTML rows.

    Each newline-separated line becomes its own row with the question part
    bolded. A line without a colon is rendered as-is, so the output degrades
    gracefully if the model returns plain sentences.
    """
    rows = []
    for line in summary.splitlines():
        line = line.strip()
        if not line:
            continue
        question, sep, answer = line.partition(":")
        if sep:
            rows.append(
                f'<div style="margin:0 0 6px 0;">'
                f'<strong style="color:#111827;">{escape(question.strip())}:</strong> '
                f'{escape(answer.strip())}</div>'
            )
        else:
            rows.append(f'<div style="margin:0 0 6px 0;">{escape(line)}</div>')
    return "".join(rows)


def build_email_html(stories, date_label):
    """Render the day's stories as an HTML email, grouped by category.

    Categories appear in the order defined by FEEDS; any unexpected category is
    appended afterwards. Each story is a card with headline, summary, source
    name, and a link to the original article.
    """
    # Group stories by category, preserving the canonical FEEDS ordering.
    grouped = {}
    for story in stories:
        grouped.setdefault(story["category"], []).append(story)
    ordered_categories = [c for c in FEEDS if c in grouped]
    ordered_categories += [c for c in grouped if c not in FEEDS]

    sections = []
    for category in ordered_categories:
        color = CATEGORY_COLORS.get(category, DEFAULT_CATEGORY_COLOR)
        cards = []
        for story in grouped[category]:
            headline = escape(story["headline"])
            summary = render_summary(story["summary"])
            source_name = escape(story["source_name"])
            url = escape(story["source_url"], quote=True)
            cards.append(
                f"""
                <div style="border:1px solid #e5e7eb;border-left:4px solid {color};
                            border-radius:8px;padding:16px 18px;margin:0 0 14px 0;
                            background:#ffffff;">
                  <div style="font-size:17px;font-weight:700;line-height:1.35;
                              color:#111827;margin:0 0 8px 0;">{headline}</div>
                  <div style="font-size:14px;line-height:1.55;color:#374151;
                              margin:0 0 12px 0;">{summary}</div>
                  <div style="font-size:13px;color:#6b7280;">
                    <span>{source_name}</span>
                    &nbsp;&middot;&nbsp;
                    <a href="{url}" style="color:{color};text-decoration:none;
                       font-weight:600;">Read the original &rarr;</a>
                  </div>
                </div>
                """
            )
        heading = (
            f'<h2 style="font-size:13px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:0.06em;color:#ffffff;background:{color};'
            f'display:inline-block;padding:5px 12px;border-radius:6px;'
            f'margin:26px 0 14px 0;">{escape(category)}</h2>'
        )
        sections.append(heading + "".join(cards))

    body = "".join(sections)
    return f"""\
<!DOCTYPE html>
<html>
  <body style="margin:0;padding:0;background:#f3f4f6;
               font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
    <div style="max-width:640px;margin:0 auto;padding:28px 20px;">
      <div style="margin:0 0 4px 0;font-size:24px;font-weight:800;color:#111827;">
        Meghna's Daily Brief - {len(stories)} briefs
      </div>
      <div style="font-size:14px;color:#6b7280;margin:0 0 6px 0;">{escape(date_label)}</div>
      <div style="display:inline-block;font-size:12px;color:#6b7280;
                  background:#eef2ff;border:1px solid #e0e7ff;border-radius:999px;
                  padding:4px 12px;margin:0 0 8px 0;">
        &#129302; AI-generated summary &middot; verify against the linked source
      </div>
      {body}
      <div style="font-size:12px;color:#9ca3af;margin-top:28px;
                  border-top:1px solid #e5e7eb;padding-top:14px;">
        Summaries generated automatically from RSS feeds. Always confirm details
        with the original article.
      </div>
    </div>
  </body>
</html>
"""


def send_brief_email(stories):
    """Send the day's stories as an HTML email via Gmail SMTP.

    Reads GMAIL_ADDRESS and GMAIL_APP_PASSWORD from the environment. Does
    nothing (returns False) if credentials are missing or there are no stories.
    """
    if not stories:
        return False

    gmail_address = os.getenv("GMAIL_ADDRESS")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_address or not gmail_password:
        print(
            "[warn] GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set; skipping email.",
            file=sys.stderr,
        )
        return False

    date_label = datetime.now(timezone.utc).strftime("%A, %B %-d, %Y")
    html = build_email_html(stories, date_label)

    message = EmailMessage()
    message["Subject"] = "Good Morning! How are you feeling today?"
    message["From"] = gmail_address
    message["To"] = gmail_address
    message.set_content(
        "Your daily news brief is ready. This email requires an HTML-capable "
        "client to view the formatted stories."
    )
    message.add_alternative(html, subtype="html")

    try:
        context = ssl.create_default_context(cafile=certifi.where())
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(gmail_address, gmail_password)
            server.send_message(message)
        print(f"[ok] emailed {len(stories)} story(ies) to {gmail_address}")
        return True
    except Exception as exc:  # noqa: BLE001 - email failure must not crash the run
        print(f"[error] failed to send email: {exc}", file=sys.stderr)
        return False


# --- Main --------------------------------------------------------------------

def main():
    load_dotenv()

    gemini_key = os.getenv("GEMINI_API_KEY")
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")

    missing = [
        name
        for name, value in (
            ("GEMINI_API_KEY", gemini_key),
            ("SUPABASE_URL", supabase_url),
            ("SUPABASE_SERVICE_KEY", supabase_key),
        )
        if not value
    ]
    if missing:
        print(f"[fatal] missing environment variable(s): {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    gemini = genai.Client(api_key=gemini_key)
    supabase = create_client(supabase_url, supabase_key)

    print("Fetching articles from the last 24 hours...")
    articles = fetch_articles()
    print(f"Collected {len(articles)} recent article(s) across all feeds.")

    print("Summarizing with Gemini...")
    stories = summarize_with_gemini(gemini, articles)
    print(f"Gemini returned {len(stories)} story summar(ies).")

    # Determine which stories are new (not already emailed in the last 24h)
    # BEFORE upserting, since the upsert would reset their created_at.
    unsent = select_unsent_stories(supabase, stories)
    print(f"{len(unsent)} story(ies) not already sent in the last 24 hours.")

    print("Upserting into Supabase...")
    added = upsert_stories(supabase, stories)

    # Only email stories that have not already gone out in the last 24 hours.
    emailed = False
    if unsent:
        print("Sending email brief...")
        emailed = send_brief_email(unsent)
    else:
        print("No new stories to email; skipping email.")

    print("\n--- News Brief Summary ---")
    print(f"Articles fetched  : {len(articles)}")
    print(f"Stories summarized: {len(stories)}")
    print(f"New (unsent)      : {len(unsent)}")
    print(f"Stories upserted  : {added}")
    print(f"Email sent        : {'yes' if emailed else 'no'}")


if __name__ == "__main__":
    main()
