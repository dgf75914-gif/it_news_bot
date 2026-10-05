#!/usr/bin/env python3
"""Telegram IT News Bot v10.0 - Groq edition."""

import os, json, sys, time, logging, re
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
import requests, feedparser
from bs4 import BeautifulSoup

CONFIG_FILE = "config.json"
HISTORY_FILE = "posts_history.json"
LOG_FILE = "bot.log"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)])


log = logging.getLogger("bot")


def load_config():
    if Path(CONFIG_FILE).exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except json.JSONDecodeError as e:
            print(f"[FAIL] {CONFIG_FILE} is not valid JSON: {e}")
            sys.exit(1)
    else:
        cfg = {"telegram": {}, "claude": {}, "posts": {}, "filters": {}, "rss_feeds": []}
    if os.environ.get("BOT_TOKEN"):
        cfg.setdefault("telegram", {})["bot_token"] = os.environ["BOT_TOKEN"]
    if os.environ.get("CHANNEL_ID"):
        cfg.setdefault("telegram", {})["channel_id"] = int(os.environ["CHANNEL_ID"])
    if os.environ.get("GEMINI_KEY"):
        cfg.setdefault("claude", {})["api_key"] = os.environ["GEMINI_KEY"]
    if os.environ.get("GROQ_KEY"):
        cfg.setdefault("claude", {})["api_key"] = os.environ["GROQ_KEY"]
    if os.environ.get("GEMINI_MODEL"):
        cfg.setdefault("claude", {})["model"] = os.environ["GEMINI_MODEL"]
    if os.environ.get("ENABLE_AI"):
        cfg.setdefault("claude", {})["enable_rewriting"] = os.environ["ENABLE_AI"].lower() == "true"
    cfg.setdefault("posts", {})
    cfg["posts"].setdefault("interval_seconds", 43200)
    cfg["posts"].setdefault("max_per_day", 3)
    cfg["posts"].setdefault("post_at_times", ["08:00", "15:00", "20:00"])
    cfg["posts"].setdefault("max_title_length", 120)
    cfg["posts"].setdefault("max_description_length", 800)
    cfg["posts"].setdefault("language", "en")
    cfg.setdefault("filters", {})
    cfg["filters"].setdefault("skip_if_no_media", False)
    cfg["filters"].setdefault("min_title_length", 15)
    cfg["filters"].setdefault("blocked_keywords", [])
    if not cfg.get("rss_feeds"):
        cfg["rss_feeds"] = [
            "https://www.producthunt.com/feed", "https://hnrss.org/show",
            "https://hnrss.org/newest", "https://dev.to/feed/tag/tooling",
            "https://dev.to/feed/tag/productivity", "https://github.blog/feed/",
            "https://techcrunch.com/feed/", "https://www.theverge.com/rss/index.xml"]
    return cfg


CONFIG = load_config()
TG = CONFIG["telegram"]
BOT_TOKEN = TG.get("bot_token", "")
CHANNEL_ID = TG.get("channel_id")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
POSTS = CONFIG.get("posts", {})
INTERVAL = POSTS.get("interval_seconds", 43200)
MAX_PER_DAY = POSTS.get("max_per_day", 3)
MAX_TITLE = POSTS.get("max_title_length", 120)
MAX_DESC = POSTS.get("max_description_length", 800)
AI = CONFIG.get("claude", {})
AI_KEY = AI.get("api_key", "")
AI_MODEL = AI.get("model", "llama-3.3-70b-versatile")
USE_AI = AI.get("enable_rewriting", False) and bool(AI_KEY) and len(AI_KEY) > 20
FILTERS = CONFIG.get("filters", {})
SKIP_NO_MEDIA = FILTERS.get("skip_if_no_media", False)
MIN_TITLE = FILTERS.get("min_title_length", 15)
BLOCKED = [k.lower() for k in FILTERS.get("blocked_keywords", [])]
RSS_FEEDS = CONFIG.get("rss_feeds", [])


def load_history():
    empty = {"posted_urls": [], "posted_titles": [], "posted_images": [],
             "daily": {}, "last_run": None, "last_slot": None}
    if not Path(HISTORY_FILE).exists():
        return empty
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {**empty, "posted_urls": data}
            data.setdefault("posted_urls", [])
            data.setdefault("posted_titles", [])
            data.setdefault("posted_images", [])
            data.setdefault("daily", {})
            data.setdefault("last_run", None)
            data.setdefault("last_slot", None)
            return data
    except Exception as e:
        log.warning(f"History read failed: {e}. Starting fresh.")
        return empty

def save_history(h):
    h["last_run"] = datetime.now().isoformat()
    cutoff = datetime.now().timestamp() - 7 * 86400
    h["daily"] = {
        d: c for d, c in h["daily"].items()
        if datetime.strptime(d, "%Y-%m-%d").timestamp() > cutoff
    }
    # Keep last 2000 items
    h["posted_urls"] = h.get("posted_urls", [])[-2000:]
    h["posted_titles"] = h.get("posted_titles", [])[-2000:]
    h["posted_images"] = h.get("posted_images", [])[-2000:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=2, ensure_ascii=False)

def today_key():
    return datetime.now().strftime("%Y-%m-%d")


def posted_today(h):
    return h["daily"].get(today_key(), 0)


def increment_today(h):
    k = today_key()
    h["daily"][k] = h["daily"].get(k, 0) + 1


def clean_html(text):
    if not text:
        return ""
    try:
        soup = BeautifulSoup(text, "html.parser")
        cleaned = re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True))
    except Exception:
        cleaned = re.sub(r"<[^>]+>", "", text or "").strip()

    # Remove Hacker News prefixes: "Show HN:", "Ask HN:", "Tell HN:"
    cleaned = re.sub(r"^(Show|Ask|Tell)\s+HN\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

def escape_html(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def source_domain(url):
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return "source"


def extract_media_from_entry(entry):
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href") or enc.get("url") or ""
        typ = enc.get("type", "")
        if typ.startswith("video/") or href.endswith(".mp4"):
            return ("video", href)
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href") or enc.get("url") or ""
        typ = enc.get("type", "")
        if typ.startswith("image/"):
            return ("image", href)
    for m in entry.get("media_content", []) or []:
        url = m.get("url")
        medium = m.get("medium", "")
        typ = m.get("type", "")
        if not url:
            continue
        if medium == "video" or typ.startswith("video/"):
            return ("video", url)
        if medium == "image" or typ.startswith("image/"):
            return ("image", url)
    for t in entry.get("media_thumbnail", []) or []:
        if t.get("url"):
            return ("image", t["url"])
    html = ""
    if entry.get("content"):
        html = entry["content"][0].get("value", "")
    elif entry.get("summary"):
        html = entry["summary"]
    if html:
        try:
            soup = BeautifulSoup(html, "html.parser")
            img = soup.find("img")
            if img and img.get("src", "").startswith("http"):
                return ("image", img["src"])
        except Exception:
            pass
    return (None, None)


def fetch_og_image(article_url):
    """Try og:image → twitter:image → Microlink preview → favicon (non-aggregators)."""
    # 1. Try og:image from HTML
    try:
        r = requests.get(article_url, timeout=15, headers=HEADERS, allow_redirects=True)
        if r.ok:
            soup = BeautifulSoup(r.content, "html.parser")
            for prop in ("og:image:secure_url", "og:image:url", "og:image"):
                tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
                if tag and tag.get("content", "").startswith("http"):
                    return tag["content"]
            for name in ("twitter:image", "twitter:image:src"):
                tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
                if tag and tag.get("content", "").startswith("http"):
                    return tag["content"]
            link = soup.find("link", rel="image_src")
            if link and link.get("href", "").startswith("http"):
                return link["href"]
    except Exception as e:
        log.warning(f"og:image fetch failed: {e}")

    # 2. Microlink — генерирует preview-картинку для любой страницы (в т.ч. Product Hunt)
    try:
        api = f"https://api.microlink.io/?url={article_url}&embed=image.url&meta=false"
        r = requests.get(api, timeout=20, headers=HEADERS)
        if r.ok:
            data = r.json()
            img = data.get("data", {}).get("image", {}).get("url")
            if img and img.startswith("http"):
                log.info(f"   -> Microlink preview: {img[:70]}")
                return img
    except Exception as e:
        log.warning(f"Microlink failed: {e}")

    # 3. Fallback: favicon для не-агрегаторов
    domain = source_domain(article_url)
    AGGREGATORS = {"producthunt.com", "news.ycombinator.com", "reddit.com",
                   "hnrss.org", "betalist.com", "indiehackers.com"}
    if domain and "." in domain and domain not in AGGREGATORS:
        fav = f"https://www.google.com/s2/favicons?domain={domain}&sz=256"
        log.info(f"   -> Google favicon: {fav}")
        return fav

    log.info(f"   -> no image source found")
    return None
def fetch_articles():
    articles = []
    for url in RSS_FEEDS:
        try:
            log.info(f"Feed: {url}")
            r = requests.get(url, timeout=15, headers=HEADERS)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            for e in feed.entries[:20]:
                link = e.get("link")
                if not link:
                    continue
                title = clean_html(e.get("title", ""))
                summary = clean_html(e.get("summary", e.get("description", "")))
                if len(title) < MIN_TITLE:
                    continue
                low = (title + " " + summary).lower()
                if any(b in low for b in BLOCKED):
                    continue
                mtype, murl = extract_media_from_entry(e)
                articles.append({"title": title[:MAX_TITLE], "summary": summary[:MAX_DESC],
                    "url": link, "media_type": mtype, "media_url": murl})
        except Exception as ex:
            log.warning(f"Feed failed {url}: {ex}")
    log.info(f"Total articles: {len(articles)}")
    return articles


def ai_rewrite(title, summary, url):
    if not USE_AI:
        return None
    domain = source_domain(url)
    prompt = f"""You are a popular tech editor writing for an English-language IT Telegram channel called "IT step by step".

Write a short, engaging review of the following IT project / news item.

STRICT STRUCTURE (2 paragraphs):
- Paragraph 1: WHAT it is and WHY it stands out (hook the reader, 2-3 sentences)
- Paragraph 2: WHO it's for and THE CATCH / pricing / verdict (2-3 sentences)

TOTAL LENGTH: 100-140 words. Keep it TIGHT and PUNCHY — no filler.

TONE: Friendly, confident, like explaining to a tech-savvy friend.

RULES:
- English only
- NO URLs, NO hashtags, NO emojis
- Do NOT start with "This article" or "In this post"
- Do NOT repeat the title
- Output ONLY the 2 paragraphs separated by a blank line

Title: {title}
Summary: {summary}
Source: {domain}

Write now."""
    models_to_try = [
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
    ]
    for model in models_to_try:
        for attempt in range(3):
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.85, "max_tokens": 800}, timeout=90,
                )
                if r.status_code in (429, 503):
                    log.warning(f"   [{model}] {r.status_code}, retry in 8s ({attempt+1}/3)")
                    time.sleep(8)
                    continue
                if not r.ok:
                    log.warning(f"   [{model}] HTTP {r.status_code}: {r.text[:200]}")
                    break
                data = r.json()
                choices = data.get("choices", [])
                if not choices:
                    log.warning(f"   [{model}] no choices")
                    break
                text = choices[0].get("message", {}).get("content", "").strip()
                if not text or len(text) < 80:
                    log.warning(f"   [{model}] too short")
                    break
                log.info(f"   [{model}] Groq OK ({len(text)} chars)")
                return text
            except Exception as e:
                log.warning(f"   [{model}] error: {e}")
                break
    log.warning("   -> All Groq models failed")
    return None


def build_caption(article, rewritten=None):
    title = escape_html(article["title"])
    url = article["url"]
    body = rewritten or article["summary"] or ""

    body = re.sub(r"^\s*[\.\,\;\:]+\s*", "", body)
    body = re.sub(r"\(\s*\d+\s*words?[^)]*\)", "", body, flags=re.I)
    body = re.sub(r"\(\s*\d+\s*[a-zA-Z]+\s*\)", "", body)
    body = re.sub(r"\s+", " ", body).strip()

    parts = [f'<a href="{url}">{title}</a>']

    if body:
        parts.append("")
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        if len(paras) < 2:
            sentences = re.split(r"(?<=[.!?])\s+", body)
            if len(sentences) >= 4:
                half = len(sentences) // 2
                paras = [
                    " ".join(sentences[:half]),
                    " ".join(sentences[half:]),
                ]
        for para in paras[:2]:
            if para and para[-1] not in ".!?":
                para += "."
            parts.append(escape_html(para))
            parts.append("")

    while parts and parts[-1] == "":
        parts.pop()

    return "\n".join(parts)


def _tg_post(method, payload, timeout=30):
    try:
        r = requests.post(f"{TG_API}/{method}", json=payload, timeout=timeout)
        if r.ok:
            return True
        log.warning(f"{method} failed: {r.text[:300]}")
    except Exception as e:
        log.warning(f"{method} error: {e}")
    return False


def _download_bytes(url, max_size=10 * 1024 * 1024):
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return None
    if url.startswith(("data:", "blob:")):
        return None
    try:
        r = requests.get(url, timeout=20, headers=HEADERS, stream=True, allow_redirects=True)
        if not r.ok:
            log.warning(f"download failed ({r.status_code}): {url[:80]}")
            return None
        ctype = (r.headers.get("content-type") or "").lower()
        if ctype and not ctype.startswith("image/"):
            log.warning(f"skip non-image content-type: {ctype}")
            return None
        if "svg" in ctype or "icon" in ctype:
            log.warning(f"skip SVG/icon: {ctype}")
            return None
        cl = int(r.headers.get("content-length", 0) or 0)
        if cl and cl > max_size:
            log.warning(f"image too big ({cl} bytes)")
            return None
        data = r.content
        if len(data) < 1000:
            log.warning(f"image too small ({len(data)} bytes)")
            return None
        if len(data) > max_size:
            log.warning(f"image too big ({len(data)} bytes)")
            return None
        ok_magic = (
            data[:8] == b"\x89PNG\r\n\x1a\n"
            or data[:3] == b"\xff\xd8\xff"
            or data[:6] in (b"GIF87a", b"GIF89a")
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        )
        if not ok_magic:
            log.warning(f"unknown image format: {data[:8]!r}")
            return None
        return data
    except Exception as e:
        log.warning(f"download error: {e}")
        return None


def _guess_ext(url):
    url_low = (url or "").lower().split("?")[0]
    if url_low.endswith(".png"):
        return "png"
    if url_low.endswith(".gif"):
        return "gif"
    if url_low.endswith(".webp"):
        return "webp"
    return "jpg"


def send_media_with_caption(media_type, media_url, caption):
    if media_type not in ("image", "video"):
        return False
    log.info("   -> downloading media...")
    max_size = 20 * 1024 * 1024 if media_type == "video" else 10 * 1024 * 1024
    data = _download_bytes(media_url, max_size=max_size)
    if not data:
        return False
    ext = _guess_ext(media_url)
    method = "sendVideo" if media_type == "video" else "sendPhoto"
    field = "video" if media_type == "video" else "photo"
    mime = "video/mp4" if media_type == "video" else f"image/{ext if ext != 'jpg' else 'jpeg'}"
    try:
        r = requests.post(f"{TG_API}/{method}",
            data={"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"},
            files={field: (f"media.{ext}", data, mime)}, timeout=120)
        if r.ok:
            log.info(f"   -> {method} uploaded OK ({len(data)} bytes)")
            return True
        log.warning(f"   -> {method} upload failed: {r.text[:250]}")
    except Exception as e:
        log.warning(f"   -> upload error: {e}")
    return False


def send_text(text):
    return _tg_post("sendMessage", {"chat_id": CHANNEL_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True})

def normalize_url(url):
    """Strip tracking params, fragments, trailing slashes."""
    try:
        url = url.split("#")[0]
        url = re.sub(r"[?&](utm_[^&]*|ref=[^&]*|source=[^&]*|fbclid=[^&]*)", "", url)
        url = url.rstrip("/")
        url = re.sub(r"^https?://", "", url)
        url = re.sub(r"^www\.", "", url)
        return url.lower()
    except Exception:
        return url or ""


def normalize_title(title):
    """Lowercase, strip punctuation, collapse spaces."""
    t = (title or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def is_duplicate(article, history):
    """Check if article is a duplicate by URL, title, or image."""
    url_key = normalize_url(article.get("url", ""))
    title_key = normalize_title(article.get("title", ""))
    img_key = normalize_url(article.get("media_url") or "")

    posted_urls = {normalize_url(u) for u in history.get("posted_urls", [])}
    posted_titles = set(history.get("posted_titles", []))
    posted_images = set(history.get("posted_images", []))

    if url_key and url_key in posted_urls:
        log.info(f"   -> SKIP: URL already posted")
        return True

    if title_key and len(title_key) > 15 and title_key in posted_titles:
        log.info(f"   -> SKIP: title already posted")
        return True

    # Image duplication — только если картинка не favicon-aggregator (там могут быть повторы)
    if img_key and img_key in posted_images:
        AGGREGATOR_IMG_PARTS = ("google.com/s2/favicons", "duckduckgo.com/ip3/")
        if not any(p in img_key for p in AGGREGATOR_IMG_PARTS):
            log.info(f"   -> SKIP: image already posted")
            return True

    return False


def remember_post(article, history):
    """Add URL, title, image to history."""
    url_key = normalize_url(article.get("url", ""))
    title_key = normalize_title(article.get("title", ""))
    img_key = normalize_url(article.get("media_url") or "")

    if url_key:
        history.setdefault("posted_urls", []).append(url_key)
    if title_key:
        history.setdefault("posted_titles", []).append(title_key)
    if img_key:
        history.setdefault("posted_images", []).append(img_key)

def publish(article):
    mtype, murl = article["media_type"], article["media_url"]
    if not murl:
        log.info("   -> no media in RSS, trying og:image...")
        og = fetch_og_image(article["url"])
        if og:
            mtype, murl = "image", og
            log.info(f"   -> image found: {og[:70]}")
            article["media_url"] = og  # сохранить для истории

    if SKIP_NO_MEDIA and not murl:
        log.info("   -> skip (no media)")
        return False

    rewritten = None
    if USE_AI:
        log.info("   -> AI rewrite via Groq")
        rewritten = ai_rewrite(article["title"], article["summary"], article["url"])
        if not rewritten:
            log.warning("   -> AI failed — SKIP this article")
            return False
        log.info("   -> AI OK")

    caption = build_caption(article, rewritten)
    if len(caption) > 1024:
        caption = caption[:1020] + "..."

    if mtype and murl:
        if send_media_with_caption(mtype, murl, caption):
            return True
        log.warning("   -> send with media failed, sending text only")
    return send_text(caption)

def check_telegram():
    for attempt in range(3):
        try:
            t = 30 + attempt * 20
            r = requests.get(f"{TG_API}/getMe", timeout=t)
            if not r.ok:
                log.error(f"getMe failed: {r.text[:200]}")
                return False
            me = r.json()["result"]
            log.info(f"Bot: @{me['username']}")
            return True
        except Exception as e:
            log.warning(f"Telegram check attempt {attempt+1}/3 failed: {e}")
            time.sleep(5)
    log.error("Cannot reach Telegram after 3 attempts")
    return False


def check_groq():
    if not USE_AI:
        log.warning("AI OFF - check config")
        return
    try:
        r = requests.get("https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {AI_KEY}"}, timeout=15)
        if r.ok:
            models = r.json().get("data", [])
            log.info(f"Groq: OK | {len(models)} models available | using {AI_MODEL}")
        else:
            log.warning(f"Groq auth: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.warning(f"Groq check failed: {e}")


def start_health_server():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    port = int(os.environ.get("PORT", 10000))
    class Handler(BaseHTTPRequestHandler):
        def _respond(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "20")
            self.end_headers()
        def do_GET(self):
            self._respond()
            try:
                self.wfile.write(b"OK - bot is running")
            except Exception:
                pass
        def do_HEAD(self):
            self._respond()
        def log_message(self, *args):
            pass
    def run():
        try:
            server = HTTPServer(("0.0.0.0", port), Handler)
            log.info(f"Health server on port {port}")
            server.serve_forever()
        except Exception as e:
            log.warning(f"Health server failed: {e}")
    t = threading.Thread(target=run, daemon=True)
    t.start()


def main():
    setup_logging()
    start_health_server()
    log.info("=" * 60)
    log.info("Telegram IT News Bot v10.0 - Groq edition")
    log.info("=" * 60)
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE") or BOT_TOKEN.startswith("YOUR_"):
        log.error("bot_token is empty. Check config.json or BOT_TOKEN env.")
        sys.exit(1)
    if CHANNEL_ID in (None, 0, -1000000000000, -1001234567890):
        log.error("channel_id looks like a placeholder.")
        sys.exit(1)
    if not RSS_FEEDS:
        log.error("No RSS feeds configured.")
        sys.exit(1)
    if not check_telegram():
        sys.exit(1)
    check_groq()
    post_times = POSTS.get("post_at_times", [])
    if post_times:
        log.info(f"Scheduled posting at: {', '.join(post_times)}")
    else:
        log.info(f"Interval mode: every {INTERVAL}s")
    log.info(f"Feeds: {len(RSS_FEEDS)} | Max/day: {MAX_PER_DAY}")
    history = load_history()
    try:
        while True:
            try:
                if posted_today(history) >= MAX_PER_DAY:
                    log.info(f"Daily limit reached ({MAX_PER_DAY}). Sleep 30 min.")
                    time.sleep(1800)
                    continue
                if post_times:
                    now = datetime.now()
                    today = now.strftime("%Y-%m-%d")
                    current_hm = now.strftime("%H:%M")
                    matched = None
                    for slot in post_times:
                        if slot == current_hm:
                            key = f"{today} {slot}"
                            if history.get("last_slot") != key:
                                matched = key
                                break
                    if matched:
                        log.info(f"Scheduled slot reached: {matched}")
                        articles = fetch_articles()
                        posted = False
                        for art in articles:
                            if art["url"] in history["posted_urls"]:
                                continue
                            log.info(f"Publishing: {art['title'][:70]}")
                            if publish(art):
                                history["posted_urls"].append(art["url"])
                                history["posted_urls"] = history["posted_urls"][-2000:]
                                increment_today(history)
                                history["last_slot"] = matched
                                save_history(history)
                                log.info(f"Posted. Today: {posted_today(history)}")
                                posted = True
                                break
                            else:
                                log.warning("failed, trying next...")
                        if not posted:
                            log.warning("Nothing new to publish this slot.")
                            history["last_slot"] = matched
                            save_history(history)
                        time.sleep(60)
                    else:
                        time.sleep(30)
                else:
                    articles = fetch_articles()
                    published = False
                    for art in articles:
                        if art["url"] in history["posted_urls"]:
                            continue
                        log.info(f"Publishing: {art['title'][:70]}")
                        if publish(art):
                            history["posted_urls"].append(art["url"])
                            history["posted_urls"] = history["posted_urls"][-2000:]
                            increment_today(history)
                            save_history(history)
                            log.info(f"Posted. Today: {posted_today(history)}")
                            published = True
                            break
                        else:
                            log.warning("failed, trying next...")
                    if not published:
                        log.warning("Nothing new this cycle.")
                    log.info(f"Sleep {INTERVAL}s...")
                    time.sleep(INTERVAL)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.exception(f"Loop error: {e}")
                time.sleep(60)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
        save_history(history)


if __name__ == "__main__":
    main()
