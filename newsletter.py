"""
Daily Telecom Newsletter Generator
===================================
Reads config from Google Sheets, fetches RSS feeds, filters by keywords,
summarises with Claude API, and sends email directly via Gmail SMTP.
"""

import os
import json
import datetime
import requests
import feedparser
import anthropic
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# ─────────────────────────────────────────────
# 1. LOAD CONFIG FROM GOOGLE SHEET
# ─────────────────────────────────────────────

def load_config_from_sheet(sheet_csv_url: dict) -> dict:
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
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=max_age_days)
    articles = []

    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
            source_name = feed.feed.get("title", url)

            for entry in feed.entries:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime.datetime(*entry.published_parsed[:6])
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime.datetime(*entry.updated_parsed[:6])

                if published and published < cutoff:
                    continue

                title   = entry.get("title", "")
                summary = entry.get("summary", "") or entry.get("description", "")
                link    = entry.get("link", "")

                text = (title + " " + summary).lower()
                matched_keywords = [kw for kw in keywords if kw.lower() in text]

                if matched_keywords:
                    articles.append({
                        "source":           source_name,
                        "original_title":   title,
                        "original_summary": summary[:1000],
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
    if not articles:
        return []

    client = anthropic.Anthropic(api_key=api_key)
    summarised = []

    batch_size = 10
    for i in range(0, len(articles), batch_size):
        batch = articles[i:i+batch_size]

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
                model="claude-haiku-4-5-20251001",
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}]
            )

            raw = response.content[0].text.strip()
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
            for art in batch:
                art["title"]   = art["original_title"]
                art["summary"] = art["original_summary"][:300]
                summarised.append(art)

    return summarised


# ─────────────────────────────────────────────
# 4. BUILD HTML EMAIL
# ─────────────────────────────────────────────

def build_html_email(articles: list, date_str: str) -> str:
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

  <div style="background:linear-gradient(135deg,#1a73e8,#0d47a1);padding:24px 28px;border-radius:8px;margin-bottom:28px;">
    <h1 style="margin:0;color:white;font-size:22px;font-weight:700;">📡 Telecom Regulatory Intelligence</h1>
    <p style="margin:6px 0 0 0;color:#b3d4ff;font-size:13px;">{date_str} &nbsp;·&nbsp; {len(articles)} article{'s' if len(articles)!=1 else ''} today</p>
  </div>

  {body}

  <div style="border-top:1px solid #eee;margin-top:32px;padding-top:16px;font-size:11px;color:#999;">
    <p>This newsletter is automatically generated and sent to regulatory affairs teams at NOS.<br>
    Keywords monitored: Digital · 5G · Net Neutrality · Open Internet · Cloud · Fair Contribution · Interconnection · Market Analysis · Universal Service · Spectrum</p>
  </div>

</body>
</html>"""

    return html


# ─────────────────────────────────────────────
# 5. SEND VIA GMAIL SMTP
# ─────────────────────────────────────────────

def send_email(gmail_address: str, gmail_app_password: str, recipients: list, subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Telecom Newsletter <{gmail_address}>"
    msg["To"]      = ", ".join(recipients)

    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, recipients, msg.as_string())

    print(f"[OK] Email sent to {', '.join(recipients)}")


# ─────────────────────────────────────────────
# 6. MAIN
# ─────────────────────────────────────────────

def main():
    today    = datetime.date.today()
    date_str = today.strftime("%A, %d %B %Y")

    CLAUDE_API_KEY       = os.environ["CLAUDE_API_KEY"]
    SHEET_URL_FEEDS      = os.environ["SHEET_URL_FEEDS"]
    SHEET_URL_KEYWORDS   = os.environ["SHEET_URL_KEYWORDS"]
    SHEET_URL_RECIPIENTS = os.environ["SHEET_URL_RECIPIENTS"]
    GMAIL_ADDRESS        = os.environ["GMAIL_ADDRESS"]
    GMAIL_APP_PASSWORD   = os.environ["GMAIL_APP_PASSWORD"]

    print(f"[{date_str}] Starting newsletter generation...")

    # 1. Load config
    print("Loading config from Google Sheets...")
    config = load_config_from_sheet({
        "feeds":      SHEET_URL_FEEDS,
        "keywords":   SHEET_URL_KEYWORDS,
        "recipients": SHEET_URL_RECIPIENTS,
    })
    print(f"  {len(config['feeds'])} feeds | {len(config['keywords'])} keywords | {len(config['recipients'])} recipients")

    # 2. Fetch & filter
    print("Fetching RSS feeds...")
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

    # 5. Send
    print("Sending email via Gmail...")
    send_email(GMAIL_ADDRESS, GMAIL_APP_PASSWORD, config["recipients"], subject, html_body)

    print("Done! ✓")


if __name__ == "__main__":
    main()
