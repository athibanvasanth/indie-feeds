import datetime
import html as html_mod
import os
import re
import urllib.parse

import feedparser
import requests
from google import genai

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

# Newsletters via kill-the-newsletter: full email HTML with embedded articles
NEWSLETTER_FEEDS = [
    {"name": "Guardian", "url": "https://kill-the-newsletter.com/feeds/defau1t6a8hk8q2ifvax.xml"},
    {"name": "The Wire Daily", "url": "https://kill-the-newsletter.com/feeds/wfvis6inrr7c7as08gq4.xml"},
    {"name": "NYTimes", "url": "https://kill-the-newsletter.com/feeds/975gttn5j5rzvrylr84x.xml"},
    {"name": "The Hindu Newsletter", "url": "https://kill-the-newsletter.com/feeds/5zujdzatzifucxo5t954.xml"},
]

# Standard RSS feeds with individual article entries
RSS_FEEDS = [
    {"name": "The Hindu - Coimbatore", "url": "https://www.thehindu.com/news/cities/Coimbatore/feeder/default.rss"},
    {"name": "Boris Cherny", "url": "https://nitter.net/bcherny/rss"},
    {"name": "Democracy Now!", "url": "https://www.democracynow.org/democracynow.rss"},
    {"name": "Scroll Newsletter", "url": "https://athibanvasanth.github.io/indie-feeds/scroll.xml"},
    {"name": "Simon Willison", "url": "https://simonwillison.net/atom/everything/"},
]

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
    links = []
    for match in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html_content, re.DOTALL):
        url, anchor = match.groups()
        url = html_mod.unescape(url)
        anchor = strip_html(anchor).strip()
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


def fetch_newsletter_content():
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=28)
    all_newsletters = []

    for feed_info in NEWSLETTER_FEEDS:
        name = feed_info["name"]
        count = 0
        try:
            feed = feedparser.parse(feed_info["url"])
            raw = len(feed.entries)
            for entry in feed.entries[:2]:
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

                # Extract full newsletter text
                full_text = strip_html(raw_html)[:4000]
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
            SOURCE_HEALTH[name] = {"count": count, "failed": raw == 0}
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
            feed = feedparser.parse(feed_info["url"])
            raw = len(feed.entries)
            for entry in feed.entries[:10]:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime.datetime(*entry.published_parsed[:6], tzinfo=datetime.timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime.datetime(*entry.updated_parsed[:6], tzinfo=datetime.timezone.utc)

                if published is not None and published < cutoff:
                    continue

                title = entry.get("title", "Untitled")
                link = entry.get("link", "")
                summary = entry.get("summary", "")
                # For RSS feeds, try to fetch actual article content
                content = ""
                if link and feed_info["name"] not in ("Boris Cherny",):
                    content = fetch_article_content(link)
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


def fetch_tldr_articles():
    # TLDR publishes weekday mornings US-Eastern (~10:00 UTC); the digest cron runs at
    # 01:15 UTC before that day's edition exists, so walk back to the latest published one.
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
        articles = parse_tldr_page(r.text)
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


def summarize(newsletters, rss_articles, tldr_articles):
    if not newsletters and not rss_articles and not tldr_articles:
        return "<p>No new articles in the last 24 hours.</p>"

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

    total = len(newsletters) + len(rss_articles) + len(tldr_articles)

    client = genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=genai.types.HttpOptions(timeout=90000),  # 90s — was unbounded, caused a stuck CI run
    )
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=f"""You are creating a personal daily news digest. You have been given:
- {len(newsletters)} full newsletters (Guardian, NYTimes, The Wire, The Hindu) with their complete editorial content
- {len(rss_articles)} individual RSS articles with fetched content
- {len(tldr_articles)} tech articles scraped from TLDR newsletter

READ THROUGH EACH NEWSLETTER CAREFULLY. They contain curated editorial picks, story summaries, and context written by editors. Extract every significant story from them.

Create a well-organized HTML digest with these sections in order:

1. **Today's Briefing** — A single paragraph (4-5 sentences) summarizing the most significant things happening today across India and the world. Write it like a friend catching you up over coffee. No bullet points, just flowing prose.

2. **Top Stories** — Pick the 5-7 most important stories from ALL sources. For each one:
   - Headline (linked if URL is available)
   - 3-4 sentence summary explaining what happened AND why it matters
   - Source attribution

3. **India** — Key Indian news stories (3-5 items) from The Hindu, The Wire, Scroll, etc. Each with headline, 2-3 sentence summary, and source attribution. Skip if nothing notable.

4. **World** — Key international stories (3-5 items) from Guardian, NYTimes, Democracy Now. Same format. Skip if nothing notable.

5. **Tech** — Technology highlights (2-4 items) from Simon Willison, TLDR, etc. Same format. Skip if nothing notable.

6. **Boris Cherny** — If there are any articles from "Boris Cherny", give them their own section. Skip if none.

7. **TLDR Highlights** — Pick the 5-8 most interesting articles from TLDR sources. For each: linked headline, 1-2 sentence summary, TLDR sub-section in parentheses. Skip if none.

8. **Also Worth Reading** — Everything else as a compact list grouped by source. Format: source name as a bold label, then bullet points with titles (linked if URL available).

Rules:
- Make titles clickable <a> links when a URL is available. If no URL, just use plain text.
- Wrap source attributions in <span class="source">, e.g. <span class="source">(The Hindu)</span>
- Use clean semantic HTML (h2, h3, p, ul, li, a, strong, span tags)
- Do NOT include html/head/body/doctype tags — just the inner content
- Do NOT repeat the same story across sections
- Prioritize stories with real-world impact over celebrity/entertainment
- If a section would have zero items, skip it entirely
- IMPORTANT: The newsletters contain MANY stories — extract all significant ones, don't just pick 2-3 from each

=== FULL NEWSLETTERS ===
{newsletter_text}

=== RSS ARTICLES ===
{article_text}

=== TLDR TECH ===
{tldr_text}"""
    )
    return response.text


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
        count = len(re.findall(r'<li>|<h3>', body))
        sections.append((title, slugify(title), body, count))
        i += 2
    return sections


def render_sections(sections):
    # first two sections (Briefing, Top Stories) open by default; rest start collapsed
    parts = []
    for idx, (title, slug, body, count) in enumerate(sections):
        open_attr = " open" if idx < 2 else ""
        badge = f'<span class="badge">{count}</span>' if count else ""
        parts.append(f"""<details id="{slug}"{open_attr}>
            <summary><h2>{title}{badge}</h2></summary>
            <div class="section-body">{body}</div>
        </details>""")
    return "\n".join(parts)


def render_nav_pills(sections):
    pills = []
    for title, slug, body, count in sections:
        label = f"{title} ({count})" if count else title
        pills.append(f'<a href="#{slug}">{label}</a>')
    return "\n".join(pills)


def build_html(digest_content, newsletters, rss_articles, tldr_articles):
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

    sections = split_sections(digest_content)
    nav_html = render_nav_pills(sections)
    sections_html = render_sections(sections) if sections else digest_content

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Daily Digest \u2014 {date_str}</title>
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
        details {{
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
    <footer>
        <div class="health">{health_line}</div>
        Auto-generated from priority RSS feeds using Gemini AI
    </footer>
</body>
</html>"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    print("Fetching all sources...")
    newsletters, rss_articles, tldr_articles = fetch_articles()
    total = len(newsletters) + len(rss_articles) + len(tldr_articles)
    print(f"\nTotal: {len(newsletters)} newsletters, {len(rss_articles)} articles, {len(tldr_articles)} TLDR items")

    if total == 0:
        print("Nothing found, skipping digest generation")
        return

    print("\nGenerating AI summary...")
    digest_content = summarize(newsletters, rss_articles, tldr_articles)

    page = build_html(digest_content, newsletters, rss_articles, tldr_articles)
    output_path = os.path.join(OUT_DIR, "digest.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(page)
    print("Wrote digest.html")


if __name__ == "__main__":
    main()
