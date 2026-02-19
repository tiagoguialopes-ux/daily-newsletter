"""
Daily Telecom Newsletter Generator
===================================
Reads config from Google Sheets, fetches RSS feeds, filters by keywords,
summarises with Claude API, and sends via Power Automate → Outlook.

Setup: See README.md for full instructions.
"""

import os
import json
import datetime
import requests
import feedparser
import anthropic

# ─────────────────────────────────────────────
# 1. LOAD CONFIG FROM GOOGLE SHEET
# ─────────────────────────────────────────────

def load_config_from_sheet(sheet_csv_url: str) -> dict:
    """
    Reads a published Google Sheet (CSV format) and returns config.
    The sheet must have 3 tabs published as CSV (one URL per tab):
      Tab 1 - 'feeds':      columns: name, url, active(yes/no)
      Tab 2 - 'keywords':   columns: keyword, active(yes/no)
      Tab 3 - 'recipients': columns: email, active(yes/no)
    
    sheet_csv_url is a dict with keys: feeds, keywords, recipients
    """
    import csv, io

    def fetch_tab(url):
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        return list(reader)

    feeds_rows      = fetch_tab(sheet_csv_url["feeds"])
    keywords_rows   = fetch_tab(sheet_csv_url["keywords"])
    recipients_rows = fetch_tab(sheet_csv_url["recipients"])

    feeds      = [r["url"]     for r in feeds_rows      if r.get("active","").lower() == "yes" and r.get("url")]
    keywords   = [r["keyword"] for r in keywords_rows   if r.get("active","").lower() == "yes" and r.get("keyword")]
    recipients = [r["email"]   for r in recipients_rows if r.get("active","").lower() == "yes" and r.get("email")]

    return {"feeds": feeds, "keywords": keywords, "recipients": recipients}


# ─────────────────────────────────────────────
# 2. FETCH & FILTER RSS FEEDS
# ─────────────────────────────────────────────

def fetch_articles(feed_urls: list, keywords: list, max_age_days: int = 1) -> list:
    """
    Parses each RSS feed and returns articles from the last max_age_days
    that contain at least one keyword (case-insensitive) in title or summary.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=max_age_days)
    articles = []

    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
            source_name = feed.feed.get("title", url)

            for entry in feed.entries:
                # Parse publication date
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime.datetime(*entry.published_parsed[:6])
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime.datetime(*entry.updated_parsed[:6])

                # Skip if too old (be lenient on weekends — include up to 3 days)
                if published and published < cutoff:
                    continue

                title   = entry.get("title", "")
                summary = entry.get("summary", "") or entry.get("description", "")
                link    = entry.get("link", "")

                # Keyword filter
                text = (title + " " + summary).lower()
                matched_keywords = [kw for kw in keywords if kw.lower() in text]

                if matched_keywords:
                    articles.append({
                        "source":           source_name,
                        "original_title":   title,
                        "original_summary": summary[:1000],  # cap for API efficiency
                        "link":             link,
                        "published":        published.strftime("%Y-%m-%d") if published else "unknown",
                        "matched_keywords": matched_keywords,
                    })

        except Exception as e:
            print(f"[WARN] Failed to fetch {url}: {e}")

    return articles


# ─────────────────────────────────────────────
# 3. SUMMARISE WITH CLAUDE API
# ─────────────────────────────────────────────

def summarise_articles(articles: list, api_key: str) -> list:
    """
    Calls Claude to generate a clean title + 100-word summary for each article.
    Batches up to 10 articles per API call for efficiency.
    """
    if not articles:
        return []

    client = anthropic.Anthropic(api_key=api_key)
    summarised = []

    # Process in batches of 10
    batch_size = 10
    for i in range(0, len(articles), batch_size):
        batch = articles[i:i+batch_size]

        # Build prompt
        articles_text = ""
        for j, art in enumerate(batch):
            articles_text += f"""
ARTICLE {j+1}:
Source: {art['source']}
Original Title: {art['original_title']}
Content: {art['original_summary']}
---"""

        prompt = f"""You are an editor for a professional telecom industry newsletter read by regulatory affairs experts at NOS (a Portuguese telco).

For each article below, produce:
1. A clear, professional TITLE (max 12 words)
2. A SUMMARY of exactly ~100 words that captures the key facts, regulatory implications, and why it matters to the European telecom industry.

Return your response as a JSON array, one object per article, with keys: "title" and "summary".
Return ONLY the JSON array, no other text.

{articles_text}"""

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",  # fast + cheap for summaries
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}]
            )

            raw = response.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            summaries = json.loads(raw)

            for j, art in enumerate(batch):
                art["title"]   = summaries[j]["title"]
                art["summary"] = summaries[j]["summary"]
                summarised.append(art)

        except Exception as e:
            print(f"[WARN] Summarisation failed for batch {i//batch_size + 1}: {e}")
            # Fall back: use original title/summary
            for art in batch:
                art["title"]   = art["original_title"]
                art["summary"] = art["original_summary"][:300]
                summarised.append(art)

    return summarised


# ─────────────────────────────────────────────
# 4. BUILD HTML EMAIL
# ─────────────────────────────────────────────

def build_html_email(articles: list, date_str: str) -> str:
    """Renders a clean, professional HTML email body."""

    if not articles:
        body = "<p style='color:#666;'>No relevant articles were found today matching your keywords.</p>"
    else:
        items = ""
        for art in articles:
            kw_tags = " ".join(
                f"<span style='background:#e8f4fd;color:#1a73e8;padding:2px 8px;border-radius:12px;font-size:11px;margin-right:4px;'>{kw}</span>"
                for kw in art.get("matched_keywords", [])
            )
            items += f"""
            <div style="border-left:3px solid #1a73e8;padding:12px 16px;margin-bottom:24px;background:#fafafa;border-radius:0 6px 6px 0;">
                <p style="margin:0 0 4px 0;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;">
                    {art['source']} &nbsp;·&nbsp; {art.get('published','')}
                </p>
                <h3 style="margin:4px 0 8px 0;font-size:16px;color:#1a1a1a;">
                    <a href="{art['link']}" style="color:#1a1a1a;text-decoration:none;">{art['title']}</a>
                </h3>
                <p style="margin:0 0 10px 0;font-size:14px;color:#444;line-height:1.6;">{art['summary']}</p>
                <div style="margin-bottom:6px;">{kw_tags}</div>
                <a href="{art['link']}" style="font-size:12px;color:#1a73e8;">Read full article →</a>
            </div>"""

        body = items

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#1a1a1a;">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1a73e8,#0d47a1);padding:24px 28px;border-radius:8px;margin-bottom:28px;">
    <h1 style="margin:0;color:white;font-size:22px;font-weight:700;">📡 Telecom Regulatory Intelligence</h1>
    <p style="margin:6px 0 0 0;color:#b3d4ff;font-size:13px;">{date_str} &nbsp;·&nbsp; {len(articles)} article{'s' if len(articles)!=1 else ''} today</p>
  </div>

  <!-- Articles -->
  {body}

  <!-- Footer -->
  <div style="border-top:1px solid #eee;margin-top:32px;padding-top:16px;font-size:11px;color:#999;">
    <p>This newsletter is automatically generated and sent to regulatory affairs teams at NOS.<br>
    Keywords monitored: Digital · 5G · Net Neutrality · Open Internet · Cloud · Fair Contribution · Interconnection · Market Analysis · Universal Service · Spectrum</p>
  </div>

</body>
</html>"""

    return html


# ─────────────────────────────────────────────
# 5. UPLOAD TO ONEDRIVE VIA MICROSOFT GRAPH API
# ─────────────────────────────────────────────

def get_onedrive_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Gets an access token for Microsoft Graph API using client credentials."""
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         "https://graph.microsoft.com/.default",
    }
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def upload_to_onedrive(token: str, user_email: str, html_body: str, subject: str, recipients: list):
    """
    Uploads a small JSON file to OneDrive at:
      /Newsletter/pending/newsletter.json
    Power Automate will watch this file and send the email.
    """
    payload = json.dumps({
        "subject":    subject,
        "recipients": recipients,
        "body":       html_body,
    })

    # Upload via Microsoft Graph — overwrites the file each day
    url = f"https://graph.microsoft.com/v1.0/users/{user_email}/drive/root:/Newsletter/pending/newsletter.json:/content"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    r = requests.put(url, headers=headers, data=payload.encode("utf-8"), timeout=30)
    r.raise_for_status()
    print(f"[OK] Newsletter file uploaded to OneDrive — status {r.status_code}")


# ─────────────────────────────────────────────
# 6. MAIN
# ─────────────────────────────────────────────

def main():
    today = datetime.date.today()
    date_str = today.strftime("%A, %d %B %Y")

    # ── Secrets from environment variables (set in GitHub Secrets) ──
    CLAUDE_API_KEY    = os.environ["CLAUDE_API_KEY"]
    SHEET_URL_FEEDS      = os.environ["SHEET_URL_FEEDS"]
    SHEET_URL_KEYWORDS   = os.environ["SHEET_URL_KEYWORDS"]
    SHEET_URL_RECIPIENTS = os.environ["SHEET_URL_RECIPIENTS"]
    # Microsoft Graph / OneDrive
    MS_TENANT_ID      = os.environ["MS_TENANT_ID"]
    MS_CLIENT_ID      = os.environ["MS_CLIENT_ID"]
    MS_CLIENT_SECRET  = os.environ["MS_CLIENT_SECRET"]
    MS_USER_EMAIL     = os.environ["MS_USER_EMAIL"]   # e.g. tiago.lopes@nos.pt

    print(f"[{date_str}] Starting newsletter generation...")

    # 1. Load config from Google Sheet
    print("Loading config from Google Sheets...")
    config = load_config_from_sheet({
        "feeds":      SHEET_URL_FEEDS,
        "keywords":   SHEET_URL_KEYWORDS,
        "recipients": SHEET_URL_RECIPIENTS,
    })
    print(f"  {len(config['feeds'])} feeds | {len(config['keywords'])} keywords | {len(config['recipients'])} recipients")

    # 2. Fetch & filter articles
    print("Fetching RSS feeds...")
    # On Mondays, look back 3 days to catch weekend news
    lookback = 3 if today.weekday() == 0 else 1
    articles = fetch_articles(config["feeds"], config["keywords"], max_age_days=lookback)
    print(f"  Found {len(articles)} matching articles")

    # 3. Summarise
    if articles:
        print("Generating summaries with Claude...")
        articles = summarise_articles(articles, CLAUDE_API_KEY)

    # 4. Build email
    html_body = build_html_email(articles, date_str)
    subject   = f"📡 Telecom Regulatory Intelligence – {date_str}"

    # 5. Upload to OneDrive → Power Automate picks it up and sends the email
    print("Uploading to OneDrive...")
    token = get_onedrive_token(MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET)
    upload_to_onedrive(token, MS_USER_EMAIL, html_body, subject, config["recipients"])

    print("Done! ✓")


if __name__ == "__main__":
    main()
