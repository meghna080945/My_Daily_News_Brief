#!/usr/bin/env python3
"""news_brief.py

Fetches recent articles from a set of RSS feeds, asks Gemini 1.5 Flash to
produce a two-sentence brief for each story, and upserts the results into a
Supabase "stories" table (deduplicated on source_url).

Environment variables (loaded from a .env file):
    GEMINI_API_KEY        - Google Generative AI API key
    SUPABASE_URL          - Supabase project URL
    SUPABASE_SERVICE_KEY  - Supabase service role key
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import feedparser
import google.generativeai as genai
from dotenv import load_dotenv
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

GEMINI_PROMPT = (
    "You are a sharp analyst briefing a founder. For each story, "
    "write exactly two sentences: one on what happened, one on why "
    "it matters. Summarize ONLY what is stated in the source text; "
    "do not infer or add facts. Skip anything irrelevant. Return "
    "only valid JSON: an array of objects with fields headline, "
    "summary, source_url, source_name, category."
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
    import re

    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", text).strip()


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
                parsed = feedparser.parse(url)
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

                    link = entry.get("link")
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


def summarize_with_gemini(articles):
    """Send articles to Gemini 2.5 Flash and return a list of story dicts."""
    if not articles:
        return []

    model = genai.GenerativeModel("gemini-flash-latest")

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

    prompt = f"{GEMINI_PROMPT}\n\nArticles:\n{json.dumps(payload, ensure_ascii=False)}"

    try:
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"},
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

    # Keep only well-formed records.
    valid = []
    required = {"headline", "summary", "source_url", "source_name", "category"}
    for story in stories:
        if isinstance(story, dict) and required.issubset(story.keys()) and story.get("source_url"):
            valid.append({field: story[field] for field in required})
    return valid


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

    genai.configure(api_key=gemini_key)
    supabase = create_client(supabase_url, supabase_key)

    print("Fetching articles from the last 24 hours...")
    articles = fetch_articles()
    print(f"Collected {len(articles)} recent article(s) across all feeds.")

    print("Summarizing with Gemini 2.5 Flash...")
    stories = summarize_with_gemini(articles)
    print(f"Gemini returned {len(stories)} story summar(ies).")

    print("Upserting into Supabase...")
    added = upsert_stories(supabase, stories)

    print("\n--- News Brief Summary ---")
    print(f"Articles fetched : {len(articles)}")
    print(f"Stories summarized: {len(stories)}")
    print(f"Stories upserted  : {added}")


if __name__ == "__main__":
    main()
