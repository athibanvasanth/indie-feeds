import datetime
import os

import feedparser
from google import genai

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

FEEDS = [
    {"name": "The Hindu - Coimbatore", "url": "https://www.thehindu.com/news/cities/Coimbatore/feeder/default.rss"},
    {"name": "Guardian", "url": "https://kill-the-newsletter.com/feeds/defau1t6a8hk8q2ifvax.xml"},
    {"name": "Boris Cherny", "url": "https://xcancel.com/bcherny/rss"},
    {"name": "TLDR Newsletter", "url": "https://www.tldrnewsletter.com/rss"},
    {"name": "Democracy Now!", "url": "https://www.democracynow.org/democracynow.rss"},
    {"name": "Scroll Newsletter", "url": "https://athibanvasanth.github.io/indie-feeds/scroll.xml"},
    {"name": "The Wire Daily", "url": "https://kill-the-newsletter.com/feeds/wfvis6inrr7c7as08gq4.xml"},
    {"name": "NYTimes", "url": "https://kill-the-newsletter.com/feeds/975gttn5j5rzvrylr84x.xml"},
    {"name": "Simon Willison", "url": "https://simonwillison.net/atom/everything/"},
    {"name": "The Hindu", "url": "https://kill-the-newsletter.com/feeds/5zujdzatzifucxo5t954.xml"},
]


def fetch_articles():
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    all_articles = []

    for feed_info in FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:10]:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime.datetime(*entry.published_parsed[:6], tzinfo=datetime.timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime.datetime(*entry.updated_parsed[:6], tzinfo=datetime.timezone.utc)

                if published is None or published > cutoff:
                    title = entry.get("title", "Untitled")
                    link = entry.get("link", "")
                    summary = entry.get("summary", "")
                    if len(summary) > 300:
                        summary = summary[:300] + "..."
                    all_articles.append({
                        "source": feed_info["name"],
                        "title": title,
                        "link": link,
                        "summary": summary,
                    })
        except Exception as e:
            print(f"  Failed to fetch {feed_info['name']}: {e}")

    return all_articles


def summarize(articles):
    if not articles:
        return "<p>No new articles in the last 24 hours.</p>"

    article_text = ""
    for a in articles:
        article_text += f"Source: {a['source']}\nTitle: {a['title']}\nLink: {a['link']}\nSummary: {a['summary']}\n\n"

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"""You are creating a personal daily news digest from {len(articles)} articles across multiple sources.

Create a well-organized HTML digest with these sections in order:

1. **Today's Briefing** — A single paragraph (3-4 sentences) summarizing the most significant things happening today. Write it like a friend catching you up over coffee. No bullet points, just flowing prose.

2. **Top Stories** — Pick the 4-6 most important stories. For each one:
   - Linked headline
   - 3-4 sentence summary explaining what happened AND why it matters
   - Source name in parentheses

3. **India** — Key Indian news stories (2-4 items). Each with a linked headline, 2-3 sentence summary, and source attribution. Skip if nothing notable.

4. **World** — Key international stories (2-4 items). Same format as India. Skip if nothing notable.

5. **Tech** — Technology highlights (2-3 items). Same format. Skip if nothing notable.

6. **Also Worth Reading** — Everything else as a compact list grouped by source. Format: source name as a bold label, then bullet points with linked titles only (no summaries).

Rules:
- Make every article title a clickable <a> link using the provided URL
- Use clean semantic HTML (h2, h3, p, ul, li, a, strong tags)
- Do NOT include html/head/body/doctype tags — just the inner content
- Do NOT repeat the same story across sections
- Prioritize stories with real-world impact over celebrity/entertainment news
- If a section would have zero items, skip it entirely

Articles:
{article_text}"""
    )
    return response.text


def build_html(digest_content, articles):
    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime("%B %d, %Y")
    ist = now + datetime.timedelta(hours=5, minutes=30)
    ist_str = ist.strftime("%I:%M %p IST")

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
            margin-bottom: 2rem;
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
        h2 {{
            font-size: 1.25rem;
            font-weight: 600;
            color: #e8734a;
            margin: 2rem 0 1rem;
            padding-bottom: 0.5rem;
            border-bottom: 1px solid #2a2a2a;
        }}
        h3 {{ font-size: 1.05rem; font-weight: 600; margin: 1.25rem 0 0.25rem; color: #f0f0f0; }}
        p {{ margin: 0.5rem 0; color: #bbb; font-size: 0.95rem; }}
        a {{ color: #e8734a; text-decoration-color: #555; }}
        a:hover {{ text-decoration-color: #e8734a; }}
        ul {{ padding-left: 1.25rem; margin: 0.5rem 0; }}
        li {{ margin: 0.4rem 0; font-size: 0.95rem; color: #bbb; }}
        strong {{ color: #e0e0e0; }}
        footer {{
            margin-top: 3rem;
            padding-top: 1rem;
            border-top: 1px solid #2a2a2a;
            color: #555;
            font-size: 0.8rem;
        }}
        @media (max-width: 480px) {{
            body {{ padding: 1.25rem 1rem; }}
            header h1 {{ font-size: 1.5rem; }}
        }}
    </style>
</head>
<body>
    <header>
        <h1>Daily Digest</h1>
        <div class="date">{date_str} \u00b7 Generated at {ist_str}</div>
        <div class="stats">{len(articles)} articles scanned from {len(set(a['source'] for a in articles))} sources</div>
    </header>
    <main>
        {digest_content}
    </main>
    <footer>
        Auto-generated from priority RSS feeds using Gemini AI
    </footer>
</body>
</html>"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    print("Fetching priority feed articles...")
    articles = fetch_articles()
    print(f"Found {len(articles)} articles from the last 24 hours")

    if not articles:
        print("No articles found, skipping digest generation")
        return

    print("Generating AI summary...")
    digest_content = summarize(articles)

    page = build_html(digest_content, articles)
    output_path = os.path.join(OUT_DIR, "digest.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(page)
    print("Wrote digest.html")


if __name__ == "__main__":
    main()
