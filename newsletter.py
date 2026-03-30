"""
Daily Telecom Newsletter Generator v5
======================================
- Personalised greeting per recipient
- Content in Portuguese
- Articles grouped by source type
- RSS + web scraping
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
from collections import defaultdict
import base64
 
 
# ─────────────────────────────────────────────
# 0. SEEN URLs — persist via GitHub API
# ─────────────────────────────────────────────
 
SEEN_URLS_FILE = "seen_urls.txt"
SEEN_URLS_EXPIRY_DAYS = 7
 
def load_seen_urls(github_token: str, github_repo: str) -> tuple[set, str]:
    """Load seen URLs. Format per line: url|||YYYY-MM-DD
    Entries older than SEEN_URLS_EXPIRY_DAYS are discarded automatically."""
    if not github_token or not github_repo:
        return set(), ""
    try:
        api_url = f"https://api.github.com/repos/{github_repo}/contents/{SEEN_URLS_FILE}"
        r = requests.get(api_url, headers={"Authorization": f"token {github_token}"}, timeout=10)
        if r.status_code == 404:
            print("  seen_urls.txt nao existe ainda, a criar...")
            return set(), ""
        r.raise_for_status()
        data = r.json()
        sha = data.get("sha", "")
        decoded = base64.b64decode(data["content"]).decode("utf-8")
        cutoff = datetime.date.today() - datetime.timedelta(days=SEEN_URLS_EXPIRY_DAYS)
        urls = set()
        expired = 0
        for line in decoded.splitlines():
            line = line.strip()
            if not line:
                continue
            if "|||" in line:
                url_part, date_str = line.rsplit("|||", 1)
                try:
                    if datetime.date.fromisoformat(date_str) >= cutoff:
                        urls.add(url_part.strip())
                    else:
                        expired += 1
                except ValueError:
                    urls.add(url_part.strip())
            else:
                urls.add(line)
        print(f"  {len(urls)} URLs validos carregados, {expired} expirados removidos")
        return urls, sha
    except Exception as e:
        print(f"[WARN] Nao foi possivel carregar seen_urls.txt: {e}")
        return set(), ""
 
 
def save_seen_urls(github_token: str, github_repo: str, existing_urls: set, new_urls: set, sha: str):
    """Save seen URLs. New URLs get today's date; existing keep their original date."""
    if not github_token or not github_repo:
        return
    try:
        today_str = datetime.date.today().isoformat()
        cutoff = datetime.date.today() - datetime.timedelta(days=SEEN_URLS_EXPIRY_DAYS)
        api_url = f"https://api.github.com/repos/{github_repo}/contents/{SEEN_URLS_FILE}"
        # Reload existing lines to preserve original dates
        dated = {}
        r = requests.get(api_url, headers={"Authorization": f"token {github_token}"}, timeout=10)
        if r.status_code == 200:
            decoded = base64.b64decode(r.json()["content"]).decode("utf-8")
            for line in decoded.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "|||" in line:
                    url_part, date_str = line.rsplit("|||", 1)
                    try:
                        if datetime.date.fromisoformat(date_str) >= cutoff:
                            dated[url_part.strip()] = date_str
                    except ValueError:
                        dated[url_part.strip()] = today_str
                else:
                    dated[line] = today_str
        for url in new_urls:
            dated[url] = today_str
        lines = [f"{url}|||{date}" for url, date in sorted(dated.items())]
        encoded = base64.b64encode("\n".join(lines).encode("utf-8")).decode("utf-8")
        payload = {"message": "chore: update seen_urls.txt", "content": encoded}
        if sha:
            payload["sha"] = sha
        r = requests.put(api_url, json=payload,
                         headers={"Authorization": f"token {github_token}"}, timeout=15)
        r.raise_for_status()
        print(f"  seen_urls.txt actualizado ({len(dated)} URLs, expiram ao fim de {SEEN_URLS_EXPIRY_DAYS} dias)")
    except Exception as e:
        print(f"[WARN] Nao foi possivel guardar seen_urls.txt: {e}")
 
 
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
 
    # feeds: name, url, active, group
    feeds = [
        {"url": r["url"], "group": r.get("Group") or r.get("group") or "Outras Fontes"}
        for r in feeds_rows
        if r.get("active","").lower() == "yes" and r.get("url")
    ]
 
    # keywords: keyword, active, groups (optional — comma-separated group names that restrict this keyword)
    keywords = []
    for r in keywords_rows:
        if r.get("active","").lower() != "yes" or not r.get("keyword"):
            continue
        groups_str = (r.get("Groups") or r.get("groups") or "").strip()
        restricted_groups = [g.strip() for g in groups_str.split(",") if g.strip()] if groups_str else []
        keywords.append({"keyword": r["keyword"], "restricted_groups": restricted_groups})
 
    # recipients: email, active, name
    recipients = [
        {"email": r["email"], "name": r.get("name", r["email"])}
        for r in recipients_rows
        if r.get("active","").lower() == "yes" and r.get("email")
    ]
 
    # scrape: name, url, selector, active, group
    scrape = [
        {"name": r["name"], "url": r["url"], "selector": r.get("selector","a"), "group": r.get("Group") or r.get("group") or "Outras Fontes"}
        for r in scrape_rows
        if r.get("active","").lower() == "yes" and r.get("url")
    ]
 
    return {"feeds": feeds, "keywords": keywords, "recipients": recipients, "scrape": scrape}
 
 
# ─────────────────────────────────────────────
# 2. FETCH & FILTER RSS FEEDS
# ─────────────────────────────────────────────
 
def fetch_rss_articles(feeds: list, keywords: list, max_age_days: int = 1) -> list:
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=max_age_days)
    articles = []
 
    for feed_cfg in feeds:
        url   = feed_cfg["url"]
        group = feed_cfg["group"]
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
                matched_keywords = [
                    kw["keyword"] for kw in keywords
                    if kw["keyword"].lower() in text
                    and (not kw["restricted_groups"] or group in kw["restricted_groups"])
                ]
 
                if matched_keywords:
                    articles.append({
                        "source":           source_name,
                        "group":            group,
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
 
        print(f"  [{source_name}] {len(unique)} links encontrados")
        return unique[:30]
 
    except Exception as e:
        print(f"[WARN] Falhou ao obter links de {index_url}: {e}")
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
        print("[WARN] beautifulsoup4 não instalado, a ignorar scraping")
        return []
 
    articles = []
    seen_urls = set()
 
    for cfg in scrape_config:
        source_name = cfg["name"]
        index_url   = cfg["url"]
        selector    = cfg["selector"]
        group       = cfg["group"]
 
        print(f"  A fazer scraping de {source_name}...")
        links = get_article_links(source_name, index_url, selector)
 
        for link_title, article_url in links:
            if article_url in seen_urls:
                continue
            seen_urls.add(article_url)
 
            text = scrape_article(article_url)
            if not text:
                continue
 
            full_text = (link_title + " " + text).lower()
            matched_keywords = [
                kw["keyword"] for kw in keywords
                if kw["keyword"].lower() in full_text
                and (not kw["restricted_groups"] or cfg["group"] in kw["restricted_groups"])
            ]
 
            if matched_keywords:
                articles.append({
                    "source":           source_name,
                    "group":            group,
                    "original_title":   link_title or article_url,
                    "original_summary": text[:1000],
                    "link":             article_url,
                    "published":        "unknown",
                    "matched_keywords": matched_keywords,
                    "type":             "scraped",
                })
 
            time.sleep(0.5)
 
    print(f"  Scraping concluído — {len(articles)} artigos encontrados")
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
# 5. SUMMARISE WITH CLAUDE API (in Portuguese)
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
ARTIGO {j+1}:
Fonte: {art['source']}
Título original: {art['original_title']}
Conteúdo: {art['original_summary']}
---"""
 
        prompt = f"""És o editor de uma newsletter profissional de telecomunicações lida por especialistas em assuntos regulatórios da NOS (operadora portuguesa de telecomunicações).
 
Para cada artigo abaixo, produz:
1. Um TÍTULO claro e profissional em português (máximo 12 palavras)
2. Um RESUMO em português de exatamente ~100 palavras que capture os factos principais, as implicações regulatórias e a relevância para a indústria europeia de telecomunicações.
 
Devolve a resposta como um array JSON, um objeto por artigo, com as chaves: "title" e "summary".
Devolve APENAS o array JSON, sem qualquer outro texto.
 
{articles_text}"""
 
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}]
            )
 
            raw = response.content[0].text.strip()
            # Strip markdown fences
            if "```" in raw:
                parts = raw.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("["):
                        raw = part
                        break
            raw = raw.strip()
 
            summaries = json.loads(raw)
 
            for j, art in enumerate(batch):
                s = summaries[j] if j < len(summaries) else {}
                art["title"]   = s.get("title", art["original_title"])
                art["summary"] = s.get("summary", "")
                # Validate we got a real summary, not empty
                if not art["summary"] or len(art["summary"]) < 20:
                    print(f"[WARN] Resumo vazio para artigo {j+1}, a tentar novamente...")
                    retry = client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=500,
                        messages=[{"role": "user", "content": f'''Resume em português em ~100 palavras este artigo para uma newsletter de regulação de telecomunicações. Devolve apenas o resumo, sem mais texto.
 
Título: {art["original_title"]}
Conteúdo: {art["original_summary"]}'''}]
                    )
                    art["summary"] = retry.content[0].text.strip()
                summarised.append(art)
 
        except Exception as e:
            print(f"[WARN] Resumo falhou para o lote {i//batch_size + 1}: {e}")
            # Retry each article individually
            for art in batch:
                try:
                    retry = client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=500,
                        messages=[{"role": "user", "content": f'''Resume em português em ~100 palavras este artigo para uma newsletter de regulação de telecomunicações. Devolve apenas o resumo, sem mais texto.
 
Título: {art["original_title"]}
Conteúdo: {art["original_summary"]}'''}]
                    )
                    art["title"]   = art["original_title"]
                    art["summary"] = retry.content[0].text.strip()
                except Exception as e2:
                    print(f"[WARN] Retry também falhou: {e2}")
                    art["title"]   = art["original_title"]
                    art["summary"] = "[Resumo não disponível]"
                summarised.append(art)
 
    return summarised
 
 
# ─────────────────────────────────────────────
# 6. BUILD HTML EMAIL
# ─────────────────────────────────────────────
 
# Group order and Portuguese labels
GROUP_ORDER = [
    "Reguladores Nacionais",
    "Instituições Europeias",
    "Publicações Nacionais",
    "Publicações Especializadas",
    "Reguladores Europeus",
    "Outras Fontes",
]
 
def build_html_email(articles: list, date_str: str, recipient_name: str) -> str:
    # Group articles
    grouped = defaultdict(list)
    for art in articles:
        grouped[art.get("group", "Outras Fontes")].append(art)
 
    # Build sections in defined order
    sections = ""
    total = 0
    for group_name in GROUP_ORDER:
        group_articles = grouped.get(group_name, [])
        if not group_articles:
            continue
 
        total += len(group_articles)
        items = ""
        for art in group_articles:
            kw_tags = " ".join(
                f"<span style='background:#e8f4fd;color:#1a73e8;padding:2px 8px;border-radius:12px;font-size:11px;margin-right:4px;'>{kw}</span>"
                for kw in art.get("matched_keywords", [])
            )
            source_badge = ""
            if art.get("type") == "scraped":
                source_badge = "<span style='background:#fff3e0;color:#e65100;padding:2px 6px;border-radius:4px;font-size:10px;margin-left:6px;'>WEB</span>"
 
            pub = art.get("published","")
            pub_str = f" &nbsp;·&nbsp; {pub}" if pub and pub != "unknown" else ""
 
            items += f"""
            <div style="border-left:3px solid #1a73e8;padding:12px 16px;margin-bottom:20px;background:#fafafa;border-radius:0 6px 6px 0;">
                <p style="margin:0 0 4px 0;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;">
                    {art['source']}{source_badge}{pub_str}
                </p>
                <h3 style="margin:4px 0 8px 0;font-size:16px;color:#1a1a1a;">
                    <a href="{art['link']}" style="color:#1a1a1a;text-decoration:none;">{art['title']}</a>
                </h3>
                <p style="margin:0 0 10px 0;font-size:14px;color:#444;line-height:1.6;">{art['summary']}</p>
                <div style="margin-bottom:6px;">{kw_tags}</div>
                <a href="{art['link']}" style="font-size:12px;color:#1a73e8;">Ler artigo completo →</a>
            </div>"""
 
        sections += f"""
        <div style="margin-bottom:32px;">
            <h2 style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#1a73e8;border-bottom:2px solid #e8f0fe;padding-bottom:8px;margin-bottom:16px;">
                {group_name} <span style="font-weight:400;color:#999;">({len(group_articles)})</span>
            </h2>
            {items}
        </div>"""
 
    if not sections:
        sections = "<p style='color:#666;'>Não foram encontrados artigos relevantes hoje para as palavras-chave definidas.</p>"
 
    # First name only for greeting
    first_name = recipient_name.split()[0] if recipient_name else "colega"
 
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#1a1a1a;">
 
  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1a73e8,#0d47a1);padding:24px 28px;border-radius:8px;margin-bottom:24px;">
    <h1 style="margin:0;color:white;font-size:22px;font-weight:700;">📡 Inteligência Regulatória Telecom</h1>
    <p style="margin:6px 0 0 0;color:#b3d4ff;font-size:13px;">{date_str} &nbsp;·&nbsp; {total} artigo{'s' if total!=1 else ''}</p>
  </div>
 
  <!-- Personalised greeting -->
  <p style="font-size:15px;color:#333;margin-bottom:24px;">Olá, {first_name}!</p>
 
  <!-- Grouped articles -->
  {sections}
 
  <!-- Footer -->
  <div style="border-top:1px solid #eee;margin-top:32px;padding-top:16px;font-size:11px;color:#999;">
    <p>Esta newsletter é gerada automaticamente e enviada às equipas de assuntos regulatórios da NOS.<br>
    Palavras-chave monitorizadas: Digital · 5G · Neutralidade da Rede · Internet Aberta · Cloud · Contribuição Justa · Interligação · Análise de Mercado · Serviço Universal · Espectro</p>
  </div>
 
</body>
</html>"""
 
    return html
 
 
# ─────────────────────────────────────────────
# 7. SEND VIA GMAIL SMTP (one email per recipient)
# ─────────────────────────────────────────────
 
 
# ─────────────────────────────────────────────
# 8. MAIN
# ─────────────────────────────────────────────
 
def main():
    today    = datetime.date.today()
    # Portuguese date format
    months_pt = ["janeiro","fevereiro","março","abril","maio","junho",
                  "julho","agosto","setembro","outubro","novembro","dezembro"]
    weekdays_pt = ["segunda-feira","terça-feira","quarta-feira","quinta-feira",
                   "sexta-feira","sábado","domingo"]
    date_str = f"{weekdays_pt[today.weekday()]}, {today.day} de {months_pt[today.month-1]} de {today.year}"
 
    CLAUDE_API_KEY       = os.environ["CLAUDE_API_KEY"]
    SHEET_URL_FEEDS      = os.environ["SHEET_URL_FEEDS"]
    SHEET_URL_KEYWORDS   = os.environ["SHEET_URL_KEYWORDS"]
    SHEET_URL_RECIPIENTS = os.environ["SHEET_URL_RECIPIENTS"]
    SHEET_URL_SCRAPE     = os.environ["SHEET_URL_SCRAPE"]
    GMAIL_ADDRESS        = os.environ["GMAIL_ADDRESS"]
    GMAIL_APP_PASSWORD   = os.environ["GMAIL_APP_PASSWORD"]
    GITHUB_TOKEN         = os.environ.get("GITHUB_TOKEN", "")
    GITHUB_REPO          = os.environ.get("GITHUB_REPOSITORY", "")
 
    print(f"[{date_str}] A iniciar geração da newsletter...")
 
    # 1. Load config
    print("A carregar configuração do Google Sheets...")
    config = load_config_from_sheet({
        "feeds":      SHEET_URL_FEEDS,
        "keywords":   SHEET_URL_KEYWORDS,
        "recipients": SHEET_URL_RECIPIENTS,
        "scrape":     SHEET_URL_SCRAPE,
    })
    print(f"  {len(config['feeds'])} feeds RSS | {len(config['scrape'])} sites scraping | {len(config['keywords'])} palavras-chave | {len(config['recipients'])} destinatários")
 
    # Load seen URLs
    print("A carregar URLs já vistos...")
    seen_urls, seen_sha = load_seen_urls(GITHUB_TOKEN, GITHUB_REPO)
 
    lookback = 3 if today.weekday() == 0 else 1
 
    # 2. Fetch RSS
    print("A obter feeds RSS...")
    rss_articles = fetch_rss_articles(config["feeds"], config["keywords"], max_age_days=lookback)
    print(f"  {len(rss_articles)} artigos RSS encontrados")
 
    # 3. Scrape websites
    print("A fazer scraping dos sites...")
    scraped_articles = fetch_scraped_articles(config["scrape"], config["keywords"])
 
    # 3b. Filter scraped articles against seen_urls
    new_scraped = [a for a in scraped_articles if a["link"] not in seen_urls]
    print(f"  {len(scraped_articles) - len(new_scraped)} artigos scraping já vistos ignorados, {len(new_scraped)} novos")
    scraped_articles = new_scraped
 
    # 4. Merge and deduplicate
    all_articles = deduplicate(rss_articles + scraped_articles)
    print(f"  Total após deduplicação: {len(all_articles)} artigos")
 
    # 5. Summarise
    if all_articles:
        print("A gerar resumos com Claude...")
        all_articles = summarise_articles(all_articles, CLAUDE_API_KEY)
 
    subject = f"📡 Inteligência Regulatória Telecom – {today.day} de {months_pt[today.month-1]} de {today.year}"
 
    # 6. Send one personalised email per recipient
    print(f"A enviar emails para {len(config['recipients'])} destinatários...")
    for r in config["recipients"]:
        print(f"  Destinatário: {r['email']} ({r['name']})")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        gmail_address      = GMAIL_ADDRESS.strip()
        gmail_app_password = GMAIL_APP_PASSWORD.strip().replace(" ", "")
        server.login(gmail_address, gmail_app_password)
 
        for recipient in config["recipients"]:
            first_name = recipient["name"].split()[0] if recipient["name"] else "colega"
            html_body  = build_html_email(all_articles, date_str, recipient["name"])
 
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"Inteligência Regulatória Telecom <{gmail_address}>"
            msg["To"]      = recipient["email"]
            msg.attach(MIMEText(html_body, "html"))
 
            server.sendmail(gmail_address, [recipient["email"]], msg.as_string())
            print(f"  [OK] Enviado para {recipient['email']} ({recipient['name']})")
 
    # Update seen_urls with all articles sent today
    new_urls_today = {a["link"] for a in all_articles}
    print("A guardar URLs vistos...")
    save_seen_urls(GITHUB_TOKEN, GITHUB_REPO, seen_urls, new_urls_today, seen_sha)
 
    print("Concluído! ✓")
 
 
if __name__ == "__main__":
    main()
