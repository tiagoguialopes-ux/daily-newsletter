"""
Daily Telecom Newsletter Generator
===================================
Reads config from Google Sheets, fetches RSS feeds AND scrapes websites,
filters by keywords, summarises with Claude API, sends via Gmail SMTP.
"""

import os
import json
import datetime
import requests
import feedparser
import anthropic
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse


# ─────────────────────────────────────────────
# 1. LOAD CONFIG FROM GOOGLE SHEET
# ─────────────────────────────────────────────

def load_config_from_sheet(sheet_csv_urls: dict) -> dict:
    import csv, io

    def fetch_tab(url):
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        return list(reader)

    feeds_rows      = fetch_tab(sheet_csv_urls["feeds"])
    keywords_rows   = fetch_tab(sheet_csv_urls["keywords"])
    recipients_rows = fetch_tab(sheet_csv_urls["recipients"])
    scrape_rows     = fetch_tab(sheet_csv_urls["scrape"])

    feeds      = [r["url"]     for r in feeds_rows      if r.get("active","").lower() == "yes" and r.get("url")]
    keywords   = [r["keyword"] for r in keywords_rows   if r.get("active","").lower() == "yes" and r.get("keyword")]
    recipients = [r["email"]   for r in recipients_rows if r.get("active","").lower() == "yes" and r.get("email")]
    scrape     = [(r["name"], r["url"], r.get("selector","a"))
                  for r in scrape_rows
                  if r.get("active","").lower() == "yes" and r.get("url")]

    return {"feeds": feeds, "keywords": keywords, "recipients": recipients, "scrape": scrape}


# ─────────────────────────────────────────────
# 2. FETCH & FILTER RSS FEEDS
# ─────────────────────────────────────────────

def fetch_rss_articles(feed_urls: list, keywords: list, max_age_days: int = 1) -> list:
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
                        "type":             "rss",
                    })

        except Exception as e:
            print(f"[WARN] RSS failed for {url}: {e}")

    return articles


# ─────────────────────────────────────────────
# 3. SCRAPE WEBSITES
# ─────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def get_article_links(source_name: str, index_url: str, selector: str) -> list:
    try:
        from bs4 import BeautifulSoup
        r = requests.get(index_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        base_domain = f"{urlparse(index_url).scheme}://{urlparse(index_url).netloc}"
        links = []

        for sel in selector.split(","):
            sel = sel.strip()
            for tag in soup.select(sel):
                href = tag.get("href", "")
                if not href or href.startswith("#") or href.startswith("javascript"):
                    continue
                full_url = urljoin(base_domain, href)
                if urlparse(full_url).netloc == urlparse(index_url).netloc:
                    links.append((tag.get_text(strip=True), full_url))

        seen = set()
        unique = []
        for title, url in links:
            if url not in seen and len(title) > 5:
                seen.add(url)
                unique.append((title, url))

        print(f"  [{source_name}] Found {len(unique)} links")
        return unique[:30]

    except Exception as e:
        print(f"[WARN] Failed to get links from {index_url}: {e}")
        return []


def scrape_article(url: str) -> str:
    try:
        from bs4 import BeautifulSoup
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for tag in soup(["nav", "header", "footer", "script", "style", "aside", "form"]):
            tag.decompose()

        for selector in ["article", "main", ".content", ".article-body", ".entry-content", ".post-content", "#content"]:
            container = soup.select_one(selector)
            if container:
                text = container.get_text(separator=" ", strip=True)
                if len(text) > 200:
                    return text[:2000]

        paragraphs = soup.find_all("p")
        text = " ".join(p.get_text(strip=True) for p in paragraphs)
        return text[:2000]

    except Exception as e:
        return ""


def fetch_scraped_articles(scrape_config: list, keywords: list) -> list:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[WARN] beautifulsoup4 not installed, skipping scraping")
        return []

    articles = []
    seen_urls = set()

    for source_name, index_url, selector in scrape_config:
        print(f"  Scraping {source_name}...")
        links = get_article_links(source_name, index_url, selector)

        for link_title, article_url in links:
            if article_url in seen_urls:
                continue
            seen_urls.add(article_url)

            title_lower = link_title.lower()
            quick_match = [kw for kw in keywords if kw.lower() in title_lower]

            text = scrape_article(article_url)
            if not text and not quick_match:
                continue

            full_text = (link_title + " " + text).lower()
            matched_keywords = [kw for kw in keywords if kw.lower() in full_text]

            if matched_keywords:
                articles.append({
                    "source":           source_name,
                    "original_title":   link_title or article_url,
                    "original_summary": text[:1000],
                    "link":             article_url,
                    "published":        "unknown",
                    "matched_keywords": matched_keywords,
                    "type":             "scraped",
                })

            time.sleep(0.5)

    print(f"  Scraping complete — {len(articles)} matching articles found")
    return articles


# ─────────────────────────────────────────────
# 4. DEDUPLICATE
# ─────────────────────────────────────────────

def deduplicate(articles: list) -> list:
    seen_urls   = set()
    seen_titles = set()
    unique = []

    for art in articles:
        url       = art["link"]
        title_key = art["original_title"].lower().strip()[:60]

        if url in seen_urls or title_key in seen_titles:
            continue

        seen_urls.add(url)
        seen_titles.add(title_key)
        unique.append(art)

    return unique


# ─────────────────────────────────────────────
# 5. SUMMARISE WITH CLAUDE API
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
# 6. BUILD HTML EMAIL
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
            source_badge = ""
            if art.get("type") == "scraped":
                source_badge = "<span style='background:#fff3e0;color:#e65100;padding:2px 6px;border-radius:4px;font-size:10px;margin-left:6px;'>WEB</span>"

            items += f"""
            <div style="border-left:3px solid #1a73e8;padding:12px 16px;margin-bottom:24px;background:#fafafa;border-radius:0 6px 6px 0;">
                <p style="margin:0 0 4px 0;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;">
                    {art['source']}{source_badge} &nbsp;·&nbsp; {art.get('published','')}
                </p>
                <h3 style="margin:4px 0 8px 0;font-size:16px;color:#1a1a1a;">
                    <a href="{art['link']}" style="color:#1a1a1a;text-decoration:none;">{art['title']}</a>
                </h3>
                <p style="margin:0 0 10px 0;font-size:14px;color:#444;line-height:1.6;">{art['summary']}</p>
                <div style="margin-bottom:6px;">{kw_tags}</div>
                <a href="{art['link']}" style="font-size:12px;color:#1a73e8;">Read full article →</a>
            </div>"""
        body = items

    rss_count     = sum(1 for a in articles if a.get("type") == "rss")
    scraped_count = sum(1 for a in articles if a.get("type") == "scraped")

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#1a1a1a;">

  <div style="background:linear-gradient(135deg,#1a73e8,#0d47a1);padding:24px 28px;border-radius:8px;margin-bottom:28px;">
    <h1 style="margin:0;color:white;font-size:22px;font-weight:700;">📡 Telecom Regulatory Intelligence</h1>
    <p style="margin:6px 0 0 0;color:#b3d4ff;font-size:13px;">{date_str} &nbsp;·&nbsp; {len(articles)} article{'s' if len(articles)!=1 else ''} &nbsp;·&nbsp; {rss_count} RSS · {scraped_count} Web</p>
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
# 7. SEND VIA GMAIL SMTP
# ─────────────────────────────────────────────

def send_email(gmail_address: str, gmail_app_password: str, recipients: list, subject: str, html_body: str):
    gmail_address      = gmail_address.strip()
    gmail_app_password = gmail_app_password.strip().replace(" ", "")

    print(f"  Connecting as: '{gmail_address}'")
    print(f"  Password length: {len(gmail_app_password)} chars")

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
# 8. MAIN
# ─────────────────────────────────────────────

def main():
    today    = datetime.date.today()
    date_str = today.strftime("%A, %d %B %Y")

    CLAUDE_API_KEY       = os.environ["CLAUDE_API_KEY"]
    SHEET_URL_FEEDS      = os.environ["SHEET_URL_FEEDS"]
    SHEET_URL_KEYWORDS   = os.environ["SHEET_URL_KEYWORDS"]
    SHEET_URL_RECIPIENTS = os.environ["SHEET_URL_RECIPIENTS"]
    SHEET_URL_SCRAPE     = os.environ["SHEET_URL_SCRAPE"]
    GMAIL_ADDRESS        = os.environ["GMAIL_ADDRESS"]
    GMAIL_APP_PASSWORD   = os.environ["GMAIL_APP_PASSWORD"]

    print(f"[{date_str}] Starting newsletter generation...")

    # 1. Load config
    print("Loading config from Google Sheets...")
    config = load_config_from_sheet({
        "feeds":      SHEET_URL_FEEDS,
        "keywords":   SHEET_URL_KEYWORDS,
        "recipients": SHEET_URL_RECIPIENTS,
        "scrape":     SHEET_URL_SCRAPE,
    })
    print(f"  {len(config['feeds'])} RSS feeds | {len(config['scrape'])} scrape sites | {len(config['keywords'])} keywords | {len(config['recipients'])} recipients")

    lookback = 3 if today.weekday() == 0 else 1

    # 2. Fetch RSS
    print("Fetching RSS feeds...")
    rss_articles = fetch_rss_articles(config["feeds"], config["keywords"], max_age_days=lookback)
    print(f"  Found {len(rss_articles)} matching RSS articles")

    # 3. Scrape websites
    print("Scraping websites...")
    scraped_articles = fetch_scraped_articles(config["scrape"], config["keywords"])

    # 4. Merge and deduplicate
    all_articles = deduplicate(rss_articles + scraped_articles)
    print(f"  Total after deduplication: {len(all_articles)} articles")

    # 5. Summarise
    if all_articles:
        print("Generating summaries with Claude...")
        all_articles = summarise_articles(all_articles, CLAUDE_API_KEY)

    # 6. Build email
    html_body = build_html_email(all_articles, date_str)
    subject   = f"📡 Telecom Regulatory Intelligence – {date_str}"

    # 7. Send
    print("Sending email via Gmail...")
    send_email(GMAIL_ADDRESS, GMAIL_APP_PASSWORD, config["recipients"], subject, html_body)

    print("Done! ✓")


if __name__ == "__main__":
    main()



if __name__ == "__main__":
    main()
