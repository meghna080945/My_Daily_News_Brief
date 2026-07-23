# News Brief

An automated daily news digest. It pulls recent articles from a set of RSS
feeds, uses Google's Gemini model to deduplicate and summarize them into
three-sentence briefs, stores the results in Supabase, and emails a formatted
digest via Gmail.

## How it works

1. **Fetch** — Pulls articles published in the last 24 hours from the RSS feeds
   defined in `FEEDS` (grouped by category: AI, Tech, Commerce, Fintech,
   Venture, Geopolitics). URLs are canonicalized so the same article under
   different tracking tags collapses to one entry.
2. **Summarize** — Sends the articles to Gemini, which deduplicates stories
   covering the same event, assigns each to a single category, and writes a
   three-sentence brief.
3. **Deduplicate against history** — Checks Supabase for stories already sent in
   the last 24 hours so the same story isn't emailed twice.
4. **Store** — Upserts every summarized story into the Supabase `stories` table
   (deduplicated on `source_url`).
5. **Email** — Sends the new (unsent) stories as a color-coded HTML brief via
   Gmail SMTP.

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable               | Description                              |
| ---------------------- | ---------------------------------------- |
| `GEMINI_API_KEY`       | Google Generative AI API key             |
| `SUPABASE_URL`         | Supabase project URL                     |
| `SUPABASE_SERVICE_KEY` | Supabase service role key                |
| `GMAIL_ADDRESS`        | Gmail address used to send/receive brief |
| `GMAIL_APP_PASSWORD`   | Gmail [app password][app-pw] (not your account password) |

[app-pw]: https://support.google.com/accounts/answer/185833

If `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` are missing, the script still runs and
stores stories in Supabase — it just skips the email.

### 3. Supabase table

The script expects a `stories` table with at least these columns:

| Column        | Type        | Notes                            |
| ------------- | ----------- | -------------------------------- |
| `source_url`  | text        | Unique — used as the conflict key |
| `headline`    | text        |                                  |
| `summary`     | text        |                                  |
| `source_name` | text        |                                  |
| `category`    | text        |                                  |
| `created_at`  | timestamptz | Defaults to `now()`              |

## Usage

```bash
python news_brief.py
```

The script prints a summary of how many articles were fetched, summarized, and
emailed.

## Automated runs

`.github/workflows/daily_brief.yml` runs the script daily at 14:00 UTC via GitHub
Actions (and on manual dispatch). Add `GEMINI_API_KEY`, `SUPABASE_URL`, and
`SUPABASE_SERVICE_KEY` as repository secrets. To enable email from CI, also add
`GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` as secrets and pass them through in the
workflow's `env` block.

## Configuration

Common things to tweak in `news_brief.py`:

- **`FEEDS`** — the RSS feeds and their categories.
- **`MAX_AGE`** — how far back to look for articles (default: 24 hours).
- **`CATEGORY_COLORS`** — accent colors for each category in the email.
- **`GEMINI_PROMPT`** — the instructions given to the model.
