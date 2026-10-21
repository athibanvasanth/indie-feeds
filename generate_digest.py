import base64
import concurrent.futures
import datetime
import hashlib
import html as html_mod
import os
import re
import subprocess
import time
import urllib.parse

import feedparser
import requests

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

# Claude Sonnet 5, via the Claude Code CLI in print mode — billed against the
# user's own Claude subscription (CLAUDE_CODE_OAUTH_TOKEN), not metered API usage.
# Was opencode Zen's free-tier Muse Spark 1.2 (before that, DeepSeek V4 Flash).
CLAUDE_MODEL = "claude-sonnet-5"

# Newsletters via kill-the-newsletter: full email HTML with embedded articles
NEWSLETTER_FEEDS = [
    {"name": "Guardian", "url": "https://kill-the-newsletter.com/feeds/defau1t6a8hk8q2ifvax.xml"},
    {"name": "The Wire Daily", "url": "https://kill-the-newsletter.com/feeds/wfvis6inrr7c7as08gq4.xml"},
    {"name": "NYTimes", "url": "https://kill-the-newsletter.com/feeds/975gttn5j5rzvrylr84x.xml"},
    # "skip": The Hindu also mails non-news newsletters (books, cinema, lifestyle) to this
    # address; they took digest slots meant for news (2026-10-21).
    {"name": "The Hindu Newsletter", "url": "https://kill-the-newsletter.com/feeds/5zujdzatzifucxo5t954.xml",
     "skip": r"on Books|First Day First Show|Cinema|Lifestyle|Health Matters|Crossword|Sport|Weekend|Podcast"},
    # Scroll's Daily Brief is a multi-story newsletter, so it belongs here rather than in
    # RSS_FEEDS. Substack 403s GitHub Actions' IP range directly (clean from a residential
    # IP), so it goes through a converter that fetches from its own unblocked host and
    # hands back JSON; fetch_feed() turns that back into RSS. feed2json since 2026-10-21:
    # rss2json started answering every request with a Cloudflare challenge (403). The
    # fallbacks are tried in order if the main route fails.
    {"name": "Scroll Newsletter", "url": "https://feed2json.org/convert?url=https%3A%2F%2Fscrollnewsletter.substack.com%2Ffeed",
     "daily": True,
     "fallbacks": ["https://api.rss2json.com/v1/api.json?rss_url=https%3A%2F%2Fscrollnewsletter.substack.com%2Ffeed",
                   "https://scrollnewsletter.substack.com/feed"]},
]

# Standard RSS feeds with individual article entries
# "max" caps busy feeds (newest first) so one outlet can't crowd out the rest; default 10.
# Rebalanced 2026-10-21: The Hindu (newsletters + 10 Coimbatore items) dominated and world
# news came only from the US/UK.
RSS_FEEDS = [
    # India
    {"name": "The Hindu - Coimbatore", "url": "https://www.thehindu.com/news/cities/Coimbatore/feeder/default.rss", "max": 3},
    {"name": "The News Minute", "url": "https://www.thenewsminute.com/feed", "max": 6},
    {"name": "The Indian Express", "url": "https://indianexpress.com/section/india/feed/", "max": 6},
    # The Wire's articles from this repo's own feed (generate_feed.py), on top of its daily
    # newsletter - he reads The Wire most and wants it well represented.
    {"name": "The Wire", "url": "https://athibanvasanth.github.io/indie-feeds/feed.xml", "max": 6},
    {"name": "Mint", "url": "https://www.livemint.com/rss/economy", "max": 5},
    # Long-form, every week or two - usually 0 on a given day
    {"name": "The Caravan", "url": "https://athibanvasanth.github.io/indie-feeds/caravan.xml", "max": 3},
    # World
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "max": 8},
    {"name": "Democracy Now!", "url": "https://www.democracynow.org/democracynow.rss"},
    # Tech
    {"name": "Simon Willison", "url": "https://simonwillison.net/atom/everything/"},
    {"name": "Rest of World", "url": "https://restofworld.org/feed/latest", "max": 3},
    # His blog since 2026-10-21: nitter.net answered 410 Gone (X-only posts have no
    # free route left). The blog posts roughly yearly, so this source is usually quiet.
    {"name": "Boris Cherny", "url": "https://borischerny.com/feed.xml"},
]

# Yearly dates that never make the news but matter to an individual taxpayer in India.
# (month, day, lead days, text) - passed to the model when due within the lead window.
FIXED_DEADLINES = [
    (7, 31, 45, "Income tax return (ITR) filing deadline for most individuals (check the news for any extension)"),
    (12, 31, 30, "Last date for a belated or revised income tax return for the last financial year"),
    (3, 31, 30, "Financial year ends: last date for tax-saving investments if you use the old tax regime"),
]


def fixed_deadlines_due(today=None):
    today = today or datetime.date.today()
    due = []
    for m, d, lead, text in FIXED_DEADLINES:
        when = datetime.date(today.year, m, d)
        if when < today:
            when = datetime.date(today.year + 1, m, d)
        if (when - today).days <= lead:
            due.append(f"{when:%d %B %Y}: {text}")
    return due


SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0 (compatible; indie-feeds-digest/1.0)"

# Per-source fetch health, populated by the fetchers, rendered in the footer.
# name -> {"count": items contributed, "failed": True if the feed returned nothing/errored}
SOURCE_HEALTH = {}


def strip_html(text):
    text = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_links_from_html(html_content):
    # Only the first 15 links are ever used downstream, so dedupe and cap the
    # candidates BEFORE decode_tracking_url — its HEAD requests are the biggest
    # per-newsletter runtime cost.
    seen = set()
    candidates = []
    for match in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html_content, re.DOTALL):
        url, anchor = match.groups()
        url = html_mod.unescape(url)
        if url in seen:
            continue
        seen.add(url)
        candidates.append((url, strip_html(anchor).strip()))
        if len(candidates) == 15:
            break

    links = []
    for url, anchor in candidates:
        real_url = decode_tracking_url(url)
        if real_url and anchor and len(anchor) > 5:
            links.append({"text": anchor, "url": real_url})
    return links


def decode_tracking_url(url):
    # Skip junk links
    if any(x in url.lower() for x in ['unsubscribe', 'mailto:', 'manage-preferences', 'email-preferences']):
        return None
    if 'kill-the-newsletter.com' in url:
        return None

    # awstrack.me (Wire): actual URL encoded in path after /L0/
    m = re.search(r'awstrack\.me/L0/(https?[^/]+.*?)(/\d+)?$', url)
    if m:
        return urllib.parse.unquote(m.group(1))

    # Tracking redirects (Guardian ablink, NYT nl.nytimes, Hindu piano.io)
    # Resolve via HEAD request to get final article URL
    if any(domain in url for domain in ['ablink.editorial.theguardian.com', 'nl.nytimes.com/f/', 'api-esp.piano.io']):
        try:
            r = SESSION.head(url, allow_redirects=True, timeout=8)
            final = r.url.split('?')[0]
            # Only keep if it resolved to an actual article (not homepage/login)
            if any(d in final for d in ['theguardian.com/', 'nytimes.com/', 'thehindu.com/']):
                path = urllib.parse.urlparse(final).path
                if path and path != '/' and len(path) > 10:
                    return final
        except Exception:
            pass
        return None

    return url


def fetch_article_content(url):
    try:
        r = SESSION.get(url, timeout=10)
        r.raise_for_status()
        html_content = r.text
        # Try og:description first
        og = re.search(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', html_content)
        og_desc = html_mod.unescape(og.group(1)) if og else ""
        # Extract paragraphs
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html_content, re.DOTALL)
        body_parts = []
        for p in paragraphs:
            clean = strip_html(p)
            if len(clean) > 40:
                body_parts.append(clean)
        body = ' '.join(body_parts)[:1500]
        return og_desc + " " + body if og_desc else body
    except Exception:
        return ""


def fetch_feed(url, timeout=15, attempts=5):
    # feedparser.parse(url) makes its own request with no timeout and its own
    # bot-signature User-Agent ("feedparser/X.Y +https://github.com/kurtmckee/...").
    # Route through SESSION instead: browser-spoofed UA, explicit timeout, and a
    # non-2xx now raises (caught by the existing except block) instead of silently
    # producing an empty feed with no error anywhere in the logs.
    #
    # Retry added 2026-08-06: rss2json's upstream fetch of Scroll fails roughly
    # 1 call in 10, and a single blip was turning into a hard ✗ for the day. Every
    # feed benefits — an ordinary network wobble no longer costs a source.
    # Widened 2026-10-07 (3 tries over ~9s -> 5 tries over ~75s): rss2json 500'd all three
    # tries from Actions on Oct 1 and Oct 7, so Scroll needs a longer wait, not more tries.
    delay = 5
    for attempt in range(1, attempts + 1):
        try:
            resp = SESSION.get(url, timeout=timeout)
            resp.raise_for_status()
            if resp.text.lstrip()[:1] == "{":
                payload = resp.json()
                if "jsonfeed.org" in str(payload.get("version", "")):
                    return feedparser.parse(jsonfeed_to_rss(payload))
                return feedparser.parse(rss2json_to_rss(payload))
            return feedparser.parse(resp.content)
        except Exception as e:
            # A 4xx won't fix itself on retry — raise immediately
            if (isinstance(e, requests.exceptions.HTTPError)
                    and e.response is not None
                    and 400 <= e.response.status_code < 500):
                raise
            if attempt == attempts:
                raise
            print(f"    fetch attempt {attempt}/{attempts} failed ({str(e)[:90]}) — retrying in {delay}s")
            time.sleep(delay)
            delay *= 2


def fetch_feed_any(feed_info):
    # Main url first, then each "fallbacks" entry; re-raises the last error so the
    # health line still shows ✗ when every route is down.
    urls = [feed_info["url"]] + feed_info.get("fallbacks", [])
    for i, url in enumerate(urls):
        if "feed2json.org" in url:
            # feed2json caches by URL and served a day-old Scroll feed (2026-10-21); a
            # throwaway query on the inner URL forces a fresh fetch.
            url += f"%3Fcb%3D{int(time.time())}"
        try:
            return fetch_feed(url)
        except Exception as e:
            if i == len(urls) - 1:
                raise
            print(f"    {feed_info['name']}: {url.split('/')[2]} failed ({str(e)[:90]}) — trying next route")


def jsonfeed_to_rss(payload):
    # JSON Feed (feed2json.org) -> minimal RSS, same idea as rss2json_to_rss below.
    # date_published is ISO 8601 and must become RFC822 for the freshness cutoff.
    def rfc822(value):
        try:
            dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        except (ValueError, TypeError, AttributeError):
            return ""

    items = payload.get("items") or []
    if not items:
        raise RuntimeError(f"feed2json returned no items: {str(payload)[:120]}")
    out = []
    for it in items:
        out.append(
            "<item>"
            f"<title>{html_mod.escape(it.get('title', ''))}</title>"
            f"<link>{html_mod.escape(it.get('url', ''))}</link>"
            f"<pubDate>{rfc822(it.get('date_published', ''))}</pubDate>"
            f"<description>{html_mod.escape(it.get('content_html') or it.get('summary') or it.get('content_text') or '')}</description>"
            "</item>"
        )
    title = html_mod.escape(payload.get("title", ""))
    return f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>{title}</title>{"".join(out)}</channel></rss>'


def rss2json_to_rss(payload):
    # rss2json hands back parsed JSON, so rebuild minimal RSS and let feedparser take it
    # from there — every caller downstream keeps working unchanged. Dates arrive as
    # "YYYY-MM-DD HH:MM:SS" in UTC (verified against the native feed) and must become
    # RFC822, or feedparser leaves published_parsed empty and the freshness cutoff
    # silently drops every entry.
    # rss2json answers HTTP 200 even when ITS upstream fetch failed, putting the
    # error in the body — so raise_for_status() never fires. Returning "" here made
    # that a silent empty feed: no exception, nothing in the logs, just a ✗ in the
    # health line with no explanation. Raise instead, so fetch_feed's retry sees it
    # and a genuine outage lands in the logs with rss2json's own message.
    if payload.get("status") != "ok":
        raise RuntimeError(f"rss2json returned status={payload.get('status')!r}: {str(payload.get('message'))[:120]}")

    def rfc822(value):
        try:
            dt = datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        except (ValueError, TypeError):
            return ""

    items = []
    for it in payload.get("items", []):
        items.append(
            "<item>"
            f"<title>{html_mod.escape(it.get('title', ''))}</title>"
            f"<link>{html_mod.escape(it.get('link', ''))}</link>"
            f"<pubDate>{rfc822(it.get('pubDate', ''))}</pubDate>"
            f"<description>{html_mod.escape(it.get('content') or it.get('description') or '')}</description>"
            "</item>"
        )
    title = html_mod.escape((payload.get("feed") or {}).get("title", ""))
    return f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>{title}</title>{"".join(items)}</channel></rss>'


def fetch_newsletter_content():
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=28)
    all_newsletters = []

    for feed_info in NEWSLETTER_FEEDS:
        name = feed_info["name"]
        count = 0
        try:
            feed = fetch_feed_any(feed_info)
            raw = len(feed.entries)
            skip = feed_info.get("skip")
            entries = [e for e in feed.entries[:6] if not (skip and re.search(skip, e.get("title", ""), re.I))][:2]
            for entry in entries:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime.datetime(*entry.published_parsed[:6], tzinfo=datetime.timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime.datetime(*entry.updated_parsed[:6], tzinfo=datetime.timezone.utc)

                if published is not None and published < cutoff:
                    continue

                title = entry.get("title", "Untitled")
                raw_html = ""
                if entry.get("content"):
                    raw_html = entry.content[0].get("value", "")
                elif entry.get("summary"):
                    raw_html = entry.summary

                if not raw_html:
                    continue

                # Extract full newsletter text. Cap was 4000 until 2026-10-21, which kept only
                # 24-39% of Guardian First Edition, NYT The World and The Hindu Morning Filter
                # (10-17k chars each) - most of their stories never reached the model.
                full_text = strip_html(raw_html)[:16000]
                # Extract any decodable links
                links = extract_links_from_html(raw_html)
                link_text = ""
                if links:
                    link_items = [f"  - {l['text']}: {l['url']}" for l in links[:15]]
                    link_text = "\nEmbedded links:\n" + "\n".join(link_items)

                all_newsletters.append({
                    "source": feed_info["name"],
                    "title": title,
                    "content": full_text,
                    "links": link_text,
                })
                count += 1
                print(f"  {feed_info['name']}: '{title}' ({len(full_text)} chars, {len(links)} links)")
            stale = False
            if feed_info.get("daily") and feed.entries and feed.entries[0].get("published_parsed"):
                # A daily newsletter whose newest issue is >36h old means a stale or broken
                # route, not a quiet day - show ✗ instead of a misleading 0.
                newest = datetime.datetime(*feed.entries[0].published_parsed[:6], tzinfo=datetime.timezone.utc)
                stale = newest < datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=36)
                if stale:
                    print(f"  {name}: newest issue {newest:%Y-%m-%d %H:%M} UTC is stale")
            SOURCE_HEALTH[name] = {"count": count, "failed": raw == 0 or stale}
        except Exception as e:
            print(f"  Failed to fetch {name}: {e}")
            SOURCE_HEALTH[name] = {"count": count, "failed": True}

    return all_newsletters


def fetch_rss_articles():
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=28)
    all_articles = []

    for feed_info in RSS_FEEDS:
        name = feed_info["name"]
        count = 0
        raw = 0
        try:
            feed = fetch_feed(feed_info["url"])
            raw = len(feed.entries)
            fresh = []
            for entry in feed.entries[:feed_info.get("max", 10)]:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime.datetime(*entry.published_parsed[:6], tzinfo=datetime.timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime.datetime(*entry.updated_parsed[:6], tzinfo=datetime.timezone.utc)

                if published is not None and published < cutoff:
                    continue
                fresh.append(entry)

            # Fetch article bodies in parallel — the serial fetch loop was the
            # digest's biggest runtime cost. pool.map preserves link order.
            bodies = {}
            links_to_fetch = list(dict.fromkeys(
                e.get("link", "") for e in fresh
                if e.get("link", "")
            ))
            if links_to_fetch:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    for link, body in zip(links_to_fetch, pool.map(fetch_article_content, links_to_fetch)):
                        bodies[link] = body

            for entry in fresh:
                title = entry.get("title", "Untitled")
                link = entry.get("link", "")
                summary = entry.get("summary", "")
                # For RSS feeds, try to use the fetched article content
                content = bodies.get(link, "")
                if not content:
                    content = strip_html(summary)[:800] if summary else ""

                if content or title:
                    all_articles.append({
                        "source": feed_info["name"],
                        "title": title,
                        "link": link,
                        "summary": content[:1000],
                    })
                    count += 1
            SOURCE_HEALTH[name] = {"count": count, "failed": raw == 0}
        except Exception as e:
            print(f"  Failed to fetch {name}: {e}")
            SOURCE_HEALTH[name] = {"count": count, "failed": True}
        if count:
            print(f"  {feed_info['name']}: {count} articles")

    return all_articles


def parse_tldr_page(text):
    articles = []
    current_section = ""
    section_pattern = r'<h3 class="text-center font-bold">(.*?)</h3>'
    article_pattern = r'<a[^>]*href="([^"]+)"[^>]*><h3>(.*?)</h3></a>\s*<div class="newsletter-html">(.*?)</div>'

    parts = re.split(r'(<h3 class="text-center font-bold">.*?</h3>)', text)
    for part in parts:
        sec = re.search(section_pattern, part)
        if sec:
            current_section = re.sub(r'<[^>]+>', '', sec.group(1)).strip()
            current_section = current_section.replace('&amp;', '&')
            continue

        for match in re.finditer(article_pattern, part, re.DOTALL):
            raw_url, title, body = match.groups()
            title = re.sub(r'<[^>]+>', '', title).strip()
            title = title.replace('&#x27;', "'").replace('&amp;', '&')
            body = re.sub(r'<[^>]+>', '', body).strip()
            body = ' '.join(body.split())[:400]
            if 'sponsor' not in title.lower() and 'utm_source=tldr' in raw_url:
                clean_url = raw_url.split('?')[0]
                articles.append({
                    "source": f"TLDR — {current_section}",
                    "title": title,
                    "link": clean_url,
                    "summary": body,
                })
    return articles


def tldr_spread(articles, n):
    # Take items round-robin across TLDR's sections (Big Tech, Science, Programming...)
    # so trimming to n keeps the variety instead of just the first section.
    by_sec = {}
    for a in articles:
        by_sec.setdefault(a["source"], []).append(a)
    out = []
    while len(out) < n and any(by_sec.values()):
        for sec in list(by_sec):
            if by_sec[sec] and len(out) < n:
                out.append(by_sec[sec].pop(0))
    return out


def fetch_tldr_articles():
    # TLDR publishes weekday mornings US-Eastern (~10:00 UTC); the digest generates
    # early in the UTC day (see digest_already_current()), before that day's edition
    # exists, so walk back to the latest published one.
    today = datetime.date.today()
    for back in range(4):
        day = (today - datetime.timedelta(days=back)).isoformat()
        url = f"https://tldr.tech/tech/{day}"
        try:
            r = SESSION.get(url, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f"  TLDR {day}: fetch failed: {e}")
            continue
        articles = tldr_spread(parse_tldr_page(r.text), 8)
        if articles:
            print(f"  TLDR: scraped {len(articles)} articles from {day}")
            SOURCE_HEALTH["TLDR"] = {"count": len(articles), "failed": False}
            return articles
    print("  TLDR: no published edition found in last 4 days")
    SOURCE_HEALTH["TLDR"] = {"count": 0, "failed": True}
    return []


def fetch_articles():
    SOURCE_HEALTH.clear()
    print("--- Fetching newsletters (deep content) ---")
    newsletters = fetch_newsletter_content()
    print("--- Fetching RSS articles ---")
    rss_articles = fetch_rss_articles()
    print("--- Fetching TLDR ---")
    tldr_articles = fetch_tldr_articles()
    return newsletters, rss_articles, tldr_articles


def sanitize_gemini_html(text):
    # The model's output is embedded verbatim into digest.html, and newsletter content
    # is attacker-controllable — strip anything that can execute script (stored XSS
    # from our own Pages domain). Regex-only, no new deps.
    # (name kept as-is post-migration off Gemini — renaming risks missing a call site)
    text = re.sub(r'<(script|iframe|object|embed)[^>]*>.*?</\1>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<(script|iframe|object|embed)[^>]*/?>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+on\w+="[^"]*"', '', text, flags=re.IGNORECASE)
    text = re.sub(r"\s+on\w+='[^']*'", '', text, flags=re.IGNORECASE)
    return text


def clean_digest(text):
    # The model sometimes prefixes commentary ("This confirms the expected output
    # format... <h2>-delimited sections...") or answers in markdown ("## Heading").
    # Embedded verbatim, a literal <h2> in that commentary opens a bogus heading
    # (seen 2026-09-30). Keep only what starts at the first well-formed <h2>Title</h2>,
    # drop code fences, and reject output with no HTML sections so the caller retries.
    text = re.sub(r'^\s*```(?:html)?\s*|\s*```\s*$', '', text.strip())
    m = re.search(r'<h2>[^<\n]{3,100}</h2>', text)
    if not m:
        raise ValueError("no well-formed <h2> section in model output (preamble/markdown only?)")
    return text[m.start():]


def generate_with_retry(prompt):
    # The model API 5xx killed the digest outright on 2026-08-04 (504 DEADLINE_EXCEEDED)
    # and 2026-08-05 (503 "high demand"). There was no retry, so one bad minute
    # lost the whole day — and because the workflow step is continue-on-error,
    # the run still went green and the previous day's digest was re-served. The
    # failure was invisible for two days. Retry transient errors only; anything
    # else (bad/missing auth, malformed request) raises immediately.
    #
    # Runs the Claude Code CLI in print mode instead of a raw HTTP call — auth is
    # CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), which bills against the
    # user's Claude subscription, not a metered API key.
    delay = 20
    for attempt in range(1, 5):
        try:
            r = subprocess.run(
                ["claude", "-p", "--model", CLAUDE_MODEL],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,  # 300s — long prompt + long output can take minutes; 90s timed out before
            )
            if r.returncode != 0:
                raise RuntimeError(f"claude CLI exit {r.returncode} (prompt {len(prompt)} chars): stderr={r.stderr.strip()[:400]!r} stdout={r.stdout.strip()[:400]!r}")
            text = r.stdout.strip()
            if not text:
                raise RuntimeError(f"empty output from claude CLI: stderr={r.stderr.strip()[:400]}")
            return clean_digest(text)
        except subprocess.TimeoutExpired:
            if attempt == 4:
                raise
            print(f"  claude CLI timeout on attempt {attempt}/4, retrying in {delay}s")
            time.sleep(delay)
            delay *= 2
        except Exception as e:
            msg = str(e)
            transient = isinstance(e, ValueError) or any(t in msg.lower() for t in ("429", "500", "502", "503", "504",
                                                         "overloaded", "rate limit", "unavailable",
                                                         "timeout"))
            if attempt == 4 or not transient:
                raise
            print(f"  claude CLI {type(e).__name__} on attempt {attempt}/4, retrying in {delay}s: {msg[:120]}")
            time.sleep(delay)
            delay *= 2


def summarize(newsletters, rss_articles, tldr_articles, previous_text=""):
    if not newsletters and not rss_articles and not tldr_articles:
        return "<p>No new articles in the last 28 hours.</p>"

    # Build newsletter section — full text for the AI to read through
    newsletter_text = ""
    for n in newsletters:
        newsletter_text += f"\n{'='*60}\nNEWSLETTER: {n['source']} — {n['title']}\n{'='*60}\n"
        newsletter_text += n['content'] + "\n"
        if n['links']:
            newsletter_text += n['links'] + "\n"

    # Build RSS articles section
    article_text = ""
    for a in rss_articles:
        article_text += f"Source: {a['source']}\nTitle: {a['title']}\nLink: {a['link']}\nContent: {a['summary']}\n\n"

    # Build TLDR section
    tldr_text = ""
    for a in tldr_articles:
        tldr_text += f"Source: {a['source']}\nTitle: {a['title']}\nLink: {a['link']}\nSummary: {a['summary']}\n\n"

    response = generate_with_retry(f"""You are creating a personal daily news digest. You have been given:
- {len(newsletters)} full newsletters (Guardian, NYTimes, The Wire, The Hindu, Scroll) with their complete editorial content
- {len(rss_articles)} individual RSS articles with fetched content
- {len(tldr_articles)} tech articles scraped from TLDR newsletter

READ THROUGH EACH NEWSLETTER CAREFULLY. They contain curated editorial picks, story summaries, and context written by editors. Extract every significant story from them.

Create a well-organized HTML digest with these sections in order:

0. **Deadlines in the News** — ONLY when the news contains an official deadline, rule change or civic window that an ordinary individual living in Tamil Nadu must act on (income tax filing or extensions, PAN/Aadhaar/KYC rules, voter-roll registration or revision, two-wheeler insurance/PUC/traffic rules, health insurance rules, bank or UPI rule changes). At most 5 items as a <ul>; each <li>: <strong>the date</strong> — what to do in one plain sentence, linked to the source when there is one. Skip the section entirely when nothing qualifies; never include general news, company or business deadlines, or anything that does not ask the reader to do something.

1. **Today's Briefing** — Some days this is the ONLY section that gets read, so it must work as a complete, standalone overview of the day. Start with a single summary paragraph (3-5 sentences) that ties together the day's most important throughline(s) across India, the world, AND tech — what's the big picture, what connects, what a busy person needs to walk away knowing. Then 8-12 STORY items covering the most essential things happening today across all three areas, ordered by importance rather than by category. These replace a separate "Top Stories" section: the one-line summary is the quick read, the hidden part is the deep read, so the hidden part must add real detail and context, not restate the line.

2. **India** — 3-5 further Indian STORY items (The Wire, The Hindu, The Indian Express, Scroll, The News Minute, Mint, The Caravan, Coimbatore news) not already in the Briefing. Skip if nothing notable.

3. **World** — 3-5 further international STORY items (Guardian, NYTimes, Al Jazeera, Democracy Now) not already in the Briefing. Skip if nothing notable.

4. **Tech** — 2-4 further technology STORY items (Simon Willison, Rest of World, TLDR) not already in the Briefing. Skip if nothing notable.

5. **Boris Cherny** — If there are any articles from "Boris Cherny", give them their own section as STORY items. Skip if none.

6. **TLDR Highlights** — The 5-8 most interesting TLDR articles not used above, as a plain <ul>: linked headline, 1-2 sentence summary, TLDR sub-section in parentheses. Skip if none.

7. **Also Worth Reading** — Everything else as a compact list grouped by source. Format: source name as a bold label, then bullet points with titles (linked if URL available).

STORY item format — use exactly this HTML for every story in sections 1-5, one after another (not inside <ul>):
<details class="story"><summary><span class="tag tag-new">New</span> <strong>Lead-in of 3-6 words</strong> — one sentence with the single key fact.</summary><div class="more"><p>3-4 sentences: what happened, the context, and why it matters.</p><p class="src"><a href="SOURCE_URL">Read the full story</a> <span class="source">(Source name)</span></p></div></details>
- The tag is either <span class="tag tag-new">New</span> or <span class="tag tag-update">Update</span>. Use Update ONLY when the same ongoing story appears in YESTERDAY'S DIGEST below; then begin the hidden part with <p><strong>What's changed:</strong> one sentence on what is new since yesterday.</p> before the context. Everything else is New.
- Put the source link in the hidden part; if no URL is available, drop the <a> and keep the source name.
- The one-line summary must make sense on its own — a reader who opens nothing should still know what happened.

Rules:
- Make titles clickable <a> links when a URL is available. If no URL, just use plain text.
- Wrap source attributions in <span class="source">, e.g. <span class="source">(The Hindu)</span>
- Use clean semantic HTML (h2, h3, p, ul, li, a, strong, span tags)
- Do NOT include html/head/body/doctype tags — just the inner content
- Output ONLY the HTML, starting directly with the first <h2> tag. No preamble, no commentary about the format, no markdown (no ## headings, no code fences)
- Do NOT repeat the same story across sections
- No single outlet supplies more than 3 of the Briefing's stories, or more than 3 items in any other section; spread every section across outlets.
- Every newsletter that carries significant news must contribute at least one story somewhere in the digest. The Guardian and NYTimes newsletters are the main Western world sources: World must draw on them as well as Al Jazeera.
- The reader values The Wire most: when The Wire covers a story, cite The Wire as its source, and make sure The Wire's strongest stories of the day appear (within the cap above).
- BALANCE: the Briefing gives India, the wider world and tech each fair space, chosen by importance, not by which source sent the most text. For world news, reach beyond the US and Europe: when Al Jazeera or others cover the Middle East, Africa, Asia or Latin America, include the significant stories. For Indian news, draw on several outlets rather than one. Mint's economy stories count as India news.
- Do NOT summarise YESTERDAY'S DIGEST itself — it is only there so you can tell New from Update
- Prioritize stories with real-world impact over celebrity/entertainment
- If a section would have zero items, skip it entirely
- IMPORTANT: The newsletters contain MANY stories — extract all significant ones, don't just pick 2-3 from each

Clarity rules (most important):
- Write in plain, everyday language — as if explaining to an intelligent friend who doesn't follow the news closely. No jargon, no insider shorthand, no editorializing.
- Start each summary with the single most important fact in one clear sentence, then add context. Lead with the news, not the setup.
- Explain names, acronyms, and references on first use ("the RBI (India's central bank)" not just "the RBI").
- State cause and effect explicitly: what happened, who did it, why it matters.
- Use short sentences and concrete numbers/dates when available.
- Never assume the reader already knows the story — each summary must stand on its own.

=== FULL NEWSLETTERS ===
{newsletter_text}

=== RSS ARTICLES ===
{article_text}

=== TLDR TECH ===
{tldr_text}

=== YESTERDAY'S DIGEST (for New/Update tagging only) ===
{previous_text or "(not available)"}""")
    text = response
    if not text:
        # Empty response — either a safety block or the output hit max_tokens.
        # Don't crash split_sections on it.
        print("  Model returned no text (safety block or max_tokens cut) — digest body will be empty")
        text = ""
    return sanitize_gemini_html(text)


def render_health():
    parts = []
    for name, h in SOURCE_HEALTH.items():
        if h["failed"]:
            parts.append(f'<span class="hz-fail">{name} ✗</span>')
        elif h["count"]:
            parts.append(f'<span class="hz-ok">{name} {h["count"]}</span>')
        else:
            parts.append(f'<span class="hz-quiet">{name} 0</span>')
    return " · ".join(parts)


def slugify(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower().replace("'", "")).strip("-")


def split_sections(digest_content):
    """Split Gemini's <h2>-delimited HTML into (title, slug, body, item_count) tuples."""
    parts = re.split(r'(<h2>.*?</h2>)', digest_content, flags=re.DOTALL)
    sections = []
    i = 1
    while i < len(parts) - 1:
        title = re.sub(r'</?h2>', '', parts[i]).strip()
        body = parts[i + 1]
        count = len(re.findall(r'<details class="story"', body)) or len(re.findall(r'<li>', body))
        sections.append((title, slugify(title), body, count))
        i += 2
    return sections


def read_minutes(html_text):
    # ~220 words a minute; never show "0 min"
    return max(1, round(len(strip_html(html_text).split()) / 220))


def quick_part(body):
    # What you read without opening anything: everything except the hidden story bodies.
    return re.sub(r'<div class="more">.*?</div>\s*</details>', '</details>', body, flags=re.DOTALL)


def render_sections(sections):
    # Only the Briefing opens by default; quick days stop there, deep days open the rest.
    parts = []
    for idx, (title, slug, body, count) in enumerate(sections):
        # Your Dates stays folded (his choice); the Briefing and any news deadlines open.
        open_attr = " open" if slug in ("todays-briefing", "deadlines-in-the-news") else ""
        quick, full = read_minutes(quick_part(body)), read_minutes(body)
        when = f"{quick} min quick · {full} min in full" if 'class="story"' in body and full > quick else f"{full} min"
        badge = f'<span class="badge">{count} · {when}</span>' if count else f'<span class="badge">{when}</span>'
        parts.append(f"""<details class="sec" id="{slug}"{open_attr}>
            <summary><h2>{title}{badge}</h2></summary>
            <div class="section-body">{body}</div>
        </details>""")
    return "\n".join(parts)


def render_nav_pills(sections):
    pills = []
    for title, slug, body, count in sections:
        label = f"{title} ({count})" if count else title
        pills.append(f'<a href="#{slug}">{label}</a>')
    pills.append('<button type="button" id="expand-all">Expand all</button>')
    return "\n".join(pills)


def personal_dates():
    # PERSONAL_DATES secret: one "YYYY-MM-DD|item|notes" per line, synced from his private
    # dotfiles memory note by deadlines.sh. Only ever rendered into the encrypted page.
    rows = []
    for line in os.environ.get("PERSONAL_DATES", "").splitlines():
        parts = [x.strip() for x in line.split("|")]
        try:
            when = datetime.date.fromisoformat(parts[0])
        except (ValueError, IndexError):
            continue
        rows.append((when, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))
    return rows


def render_your_dates(include_personal):
    today = datetime.date.today()
    items = []
    for m, d, lead, text in FIXED_DEADLINES:
        when = datetime.date(today.year, m, d)
        if when < today:
            when = datetime.date(today.year + 1, m, d)
        items.append((when, text, "Every year"))
    if include_personal:
        items += personal_dates()
    if not items:
        return ""
    lis = []
    for when, item, note in sorted(items):
        days = (when - today).days
        cls = "overdue" if days < 0 else "soon" if days <= 30 else "later"
        left = f"{-days} days overdue" if days < 0 else "today" if days == 0 else f"in {days} days"
        note_html = f'<span class="dnote">{html_mod.escape(note)}</span>' if note else ""
        lis.append(f'<li class="{cls}"><span class="dwhen">{when:%d %b %Y} · {left}</span>'
                   f'<span class="ditem">{html_mod.escape(item)}</span>{note_html}</li>')
    return ("<h2>Your Dates</h2><ul class=\"dates\">" + "".join(lis) + "</ul>")


# Fixed salt so the key a device remembers keeps opening each new day's page.
LOCK_SALT = hashlib.sha256(b"indie-feeds digest lock v1").digest()[:16]
LOCK_ITER = 600_000


def lock_key(password):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), LOCK_SALT, LOCK_ITER, 32)


def encrypt_page(page, password, title):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    ct = AESGCM(lock_key(password)).encrypt(iv, page.encode("utf-8"), None)
    b64 = lambda x: base64.b64encode(x).decode()
    return LOCK_TEMPLATE.replace("{TITLE}", html_mod.escape(title)).replace("{SALT}", b64(LOCK_SALT)) \
        .replace("{ITER}", str(LOCK_ITER)).replace("{IV}", b64(iv)).replace("{CT}", b64(ct))


def decrypt_page(shell, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = re.search(r'data-iv="([^"]+)"', shell); ct = re.search(r'data-ct="([^"]+)"', shell)
    if not (iv and ct and password):
        return ""
    try:
        return AESGCM(lock_key(password)).decrypt(base64.b64decode(iv.group(1)), base64.b64decode(ct.group(1)), None).decode("utf-8")
    except Exception:
        return ""


LOCK_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{TITLE}</title><meta name="robots" content="noindex">
<style>
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #000; color: #e0e0e0;
       font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; }
form { display: grid; gap: 0.75rem; width: min(320px, 88vw); }
h1 { font-size: 1.4rem; margin: 0; color: #f0f0f0; } p { margin: 0; color: #888; font-size: 0.9rem; }
input[type=password] { padding: 0.7rem; border-radius: 8px; border: 1px solid #333; background: #111; color: #eee; font-size: 1rem; }
button { padding: 0.7rem; border-radius: 8px; border: 0; background: #e8734a; color: #000; font-weight: 600; font-size: 1rem; }
label { color: #999; font-size: 0.85rem; } #err { color: #d1603a; min-height: 1.2em; }
</style></head><body>
<form id="lock" data-salt="{SALT}" data-iter="{ITER}" data-iv="{IV}" data-ct="{CT}">
  <h1>Daily Digest</h1><p>Private page. Enter your password to read today's digest.</p>
  <input id="pw" type="password" autocomplete="current-password" placeholder="Password" required>
  <label><input id="remember" type="checkbox" checked> Remember on this device</label>
  <button type="submit">Open</button><div id="err"></div>
</form>
<script>
const f = document.getElementById('lock');
const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
async function open(raw) {
  const key = await crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['decrypt']);
  const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: b64(f.dataset.iv) }, key, b64(f.dataset.ct));
  document.open(); document.write(new TextDecoder().decode(pt)); document.close();
}
async function derive(pw) {
  const base = await crypto.subtle.importKey('raw', new TextEncoder().encode(pw), 'PBKDF2', false, ['deriveBits']);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt: b64(f.dataset.salt),
    iterations: +f.dataset.iter }, base, 256));
}
(async () => {
  let saved = null; try { saved = localStorage.getItem('digest-key'); } catch (e) {}
  if (saved) { try { await open(b64(saved)); return; } catch (e) { try { localStorage.removeItem('digest-key'); } catch (e2) {} } }
})();
f.addEventListener('submit', async e => {
  e.preventDefault(); document.getElementById('err').textContent = 'Opening…';
  try {
    const raw = await derive(document.getElementById('pw').value);
    if (document.getElementById('remember').checked) {
      try { localStorage.setItem('digest-key', btoa(String.fromCharCode(...raw))); } catch (e) {}
    }
    await open(raw);
  } catch (err) { document.getElementById('err').textContent = 'Wrong password. Try again.'; }
});
</script></body></html>"""


def build_html(digest_content, newsletters, rss_articles, tldr_articles, your_dates=""):
    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime("%B %d, %Y")
    ist = now + datetime.timedelta(hours=5, minutes=30)
    ist_str = ist.strftime("%I:%M %p IST")

    sources = set()
    for n in newsletters:
        sources.add(n['source'])
    for a in rss_articles:
        sources.add(a['source'])
    if tldr_articles:
        sources.add("TLDR")
    total = len(newsletters) + len(rss_articles) + len(tldr_articles)
    health_line = render_health()

    sections = split_sections(your_dates + digest_content)
    nav_html = render_nav_pills(sections)
    sections_html = render_sections(sections) if sections else digest_content

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Daily Digest \u2014 {date_str}</title>
    <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%23000'/%3E%3Cpath d='M4 3a1 1 0 0 1 1 1v8a1 1 0 0 1-2 0V4a1 1 0 0 1 1-1zm7 1.5c0-1-1.5-1.5-2.5-1.5a1 1 0 1 0 0 2c.5 0 1.5.5 1.5 1.5v3c0 1-1 1.5-1.5 1.5a1 1 0 1 0 0 2c1 0 2.5-.5 2.5-1.5v-7z' fill='%23e8734a'/%3E%3C/svg%3E">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
            line-height: 1.7;
            color: #e0e0e0;
            background: #000;
            max-width: 680px;
            margin: 0 auto;
            padding: 2rem 1.5rem;
        }}
        header {{
            border-bottom: 2px solid #e8734a;
            padding-bottom: 1rem;
            margin-bottom: 1.25rem;
        }}
        header h1 {{
            font-size: 1.75rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            color: #f0f0f0;
        }}
        header .date {{
            color: #888;
            font-size: 0.9rem;
            margin-top: 0.25rem;
        }}
        header .stats {{
            color: #666;
            font-size: 0.8rem;
            margin-top: 0.2rem;
        }}
        nav {{
            position: sticky;
            top: 0;
            z-index: 10;
            display: flex;
            flex-wrap: wrap;
            gap: 0.4rem;
            background: #000;
            padding: 0.75rem 0;
            margin-bottom: 1rem;
            border-bottom: 1px solid #2a2a2a;
        }}
        nav a {{
            display: inline-block;
            padding: 0.3rem 0.7rem;
            border: 1px solid #333;
            border-radius: 999px;
            font-size: 0.78rem;
            color: #bbb;
            text-decoration: none;
            white-space: nowrap;
        }}
        nav a:hover {{ border-color: #e8734a; color: #e8734a; }}
        details.sec {{
            border-bottom: 1px solid #1a1a1a;
            margin-bottom: 0.25rem;
        }}
        summary {{
            cursor: pointer;
            list-style: none;
            padding: 0.5rem 0;
        }}
        summary::-webkit-details-marker {{ display: none; }}
        summary::before {{
            content: "\u25b8";
            display: inline-block;
            color: #e8734a;
            margin-right: 0.4rem;
            transition: transform 0.15s;
        }}
        details[open] summary::before {{ transform: rotate(90deg); }}
        summary h2 {{ display: inline; }}
        .badge {{
            display: inline-block;
            margin-left: 0.5rem;
            padding: 0.05rem 0.5rem;
            border-radius: 999px;
            background: #1a1a1a;
            color: #888;
            font-size: 0.72rem;
            font-weight: 400;
            vertical-align: middle;
        }}
        .section-body {{ padding: 0.25rem 0 1rem 1.3rem; }}
        #your-dates, #deadlines-in-the-news {{ border: 1px solid #3a2a10; border-radius: 8px; padding: 0 0.75rem; margin-bottom: 0.75rem; }}
        #your-dates h2, #deadlines-in-the-news h2 {{ color: #f0b25a; }}
        ul.dates {{ list-style: none; padding-left: 0; }}
        ul.dates li {{ display: grid; gap: 0.1rem; padding: 0.45rem 0; border-bottom: 1px solid #161616; }}
        .dwhen {{ font-size: 0.78rem; color: #777; font-variant-numeric: tabular-nums; }}
        .ditem {{ color: #ddd; }}
        .dnote {{ font-size: 0.82rem; color: #777; }}
        li.soon .dwhen {{ color: #f0b25a; font-weight: 600; }}
        li.overdue .dwhen {{ color: #d1603a; font-weight: 600; }}
        li.later .ditem {{ color: #aaa; }}
        details.story {{ margin: 0.15rem 0; border-left: 2px solid #1f1f1f; padding-left: 0.6rem; }}
        details.story[open] {{ border-left-color: #e8734a; }}
        details.story > summary {{ padding: 0.35rem 0; font-size: 0.95rem; color: #ccc; line-height: 1.55; }}
        details.story > summary::before {{ font-size: 0.8em; }}
        .more {{ padding: 0 0 0.5rem 1rem; }}
        .more p {{ margin: 0.35rem 0; }}
        .src {{ font-size: 0.85rem; }}
        .tag {{ display: inline-block; font-size: 0.66rem; font-weight: 600; letter-spacing: 0.03em;
                text-transform: uppercase; padding: 0.05rem 0.4rem; border-radius: 4px; margin-right: 0.3rem;
                vertical-align: 0.1em; }}
        .tag-new {{ background: #16301f; color: #7fc79a; }}
        .tag-update {{ background: #3a2a10; color: #f0b25a; }}
        nav button {{ padding: 0.3rem 0.7rem; border: 1px solid #333; border-radius: 999px; font-size: 0.78rem;
                      color: #bbb; background: #000; cursor: pointer; font-family: inherit; }}
        nav button:hover {{ border-color: #e8734a; color: #e8734a; }}
        h2 {{
            font-size: 1.15rem;
            font-weight: 600;
            color: #e8734a;
        }}
        h3 {{ font-size: 1.05rem; font-weight: 600; margin: 1.25rem 0 0.25rem; color: #f0f0f0; }}
        p {{ margin: 0.5rem 0; color: #bbb; font-size: 0.95rem; }}
        a {{ color: #e8734a; text-decoration-color: #555; }}
        a:hover {{ text-decoration-color: #e8734a; }}
        ul {{ padding-left: 1.25rem; margin: 0.5rem 0; }}
        li {{ margin: 0.4rem 0; font-size: 0.95rem; color: #bbb; }}
        strong {{ color: #e0e0e0; }}
        .source {{ color: #5a9a7a; font-size: 0.85em; }}
        footer {{
            margin-top: 2rem;
            padding-top: 1rem;
            border-top: 1px solid #2a2a2a;
            color: #555;
            font-size: 0.8rem;
        }}
        .health {{ margin-bottom: 0.6rem; line-height: 1.9; }}
        .health .hz-ok {{ color: #5a9a7a; }}
        .health .hz-quiet {{ color: #555; }}
        .health .hz-fail {{ color: #d1603a; font-weight: 600; }}
        @media (max-width: 480px) {{
            body {{ padding: 1.25rem 1rem; }}
            header h1 {{ font-size: 1.5rem; }}
            nav {{ gap: 0.3rem; }}
            nav a {{ font-size: 0.72rem; padding: 0.25rem 0.55rem; }}
        }}
    </style>
</head>
<body>
    <header>
        <h1>Daily Digest</h1>
        <div class="date">{date_str} \u00b7 Generated at {ist_str}</div>
        <div class="stats">{len(newsletters)} newsletters + {len(rss_articles)} articles + {len(tldr_articles)} TLDR items from {len(sources)} sources</div>
    </header>
    <nav>
        {nav_html}
    </nav>
    <main>
        {sections_html}
    </main>
    <script>
        // Expand all: opens every section and story; tap again to fold back to the quick view.
        const b = document.getElementById('expand-all');
        if (b) b.addEventListener('click', () => {{
            const open = b.textContent === 'Expand all';
            document.querySelectorAll('details').forEach((d, i) => {{ d.open = open || ['todays-briefing', 'deadlines-in-the-news'].includes(d.id); }});
            b.textContent = open ? 'Collapse all' : 'Expand all';
        }});
    </script>
    <footer>
        <div class="health">{health_line}</div>
        Auto-generated from priority RSS feeds using Claude Sonnet 5
    </footer>
</body>
</html>"""


def digest_already_current():
    # public/digest.html was just curled down from the live site by the
    # "Preserve existing digest" workflow step. Every hourly run reaches this
    # script now (see generate-feed.yml), so skip the expensive Claude pass
    # once today's UTC-dated digest already exists - only the first run after
    # midnight UTC should actually regenerate.
    path = os.path.join(OUT_DIR, "digest.html")
    if not os.path.exists(path):
        return False
    today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%B %d, %Y")
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(2000)
    except OSError:
        return False
    return f"Daily Digest — {today_str}" in head


def previous_digest_text():
    # The workflow curls the live (yesterday's) digest.html into public/ before this runs;
    # its story headlines let the model tag today's stories New or Update.
    path = os.path.join(OUT_DIR, "digest.html")
    try:
        with open(path, encoding="utf-8") as f:
            page = f.read()
    except OSError:
        return ""
    if 'id="lock"' in page:
        page = decrypt_page(page, os.environ.get("DIGEST_PASSWORD", ""))
    m = re.search(r"<main>(.*?)</main>", page, re.DOTALL)
    body = quick_part(m.group(1)) if m else ""
    return strip_html(body)[:8000]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    if digest_already_current() and not os.environ.get("FORCE_DIGEST"):
        print("digest.html already current for today (UTC) - skipping regeneration")
        return

    print("Fetching all sources...")
    newsletters, rss_articles, tldr_articles = fetch_articles()
    total = len(newsletters) + len(rss_articles) + len(tldr_articles)
    print(f"\nTotal: {len(newsletters)} newsletters, {len(rss_articles)} articles, {len(tldr_articles)} TLDR items")

    if total == 0:
        print("Nothing found, skipping digest generation")
        return

    print("\nGenerating AI summary...")
    digest_content = summarize(newsletters, rss_articles, tldr_articles, previous_digest_text())

    password = os.environ.get("DIGEST_PASSWORD", "")
    # Personal dates only ever go into an encrypted page; without a password the page
    # is published as before, with the yearly dates only.
    page = build_html(digest_content, newsletters, rss_articles, tldr_articles, render_your_dates(bool(password)))
    if password:
        title = re.search(r"<title>(.*?)</title>", page).group(1)
        page = encrypt_page(page, password, title)
    output_path = os.path.join(OUT_DIR, "digest.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(page)
    print("Wrote digest.html")


if __name__ == "__main__":
    main()
