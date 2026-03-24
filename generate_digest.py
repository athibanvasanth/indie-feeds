import datetime
import os

import feedparser
import google.generativeai as genai

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

    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-2.0-flash")

    response = model.generate_content(
        f"""You are creating a daily news digest. Below are articles from the last 24 hours across multiple sources.

Create a well-organized HTML digest with these sections:
1. **Top Stories** \u2014 3-5 most important stories with brief summaries (2-3 sentences each). Include the source name.
2. **India** \u2014 Key Indian news stories
3. **World** \u2014 Key international stories
4. **Tech** \u2014 Technology highlights
5. **Quick Links** \u2014 Other noteworthy articles as a bullet list with source attribution

Rules:
- For each article, make the title a clickable <a> link using the provided URL
- Use clean semantic HTML (h2, h3, p, ul, li, a tags)
- Do NOT include html/head/body/doctype tags \u2014 just the inner content
- Keep summaries concise and informative (2-3 sentences max)
- If a section has no relevant articles, skip it entirely
- Add source attribution in parentheses after each item

Articles:
{article_text}"""
    )
    return response.text


def build_html(digest_content):
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
            color: #1a1a1a;
            background: #fafaf9;
            max-width: 680px;
            margin: 0 auto;
            padding: 2rem 1.5rem;
        }}
        header {{
            border-bottom: 2px solid #1a1a1a;
            padding-bottom: 1rem;
            margin-bottom: 2rem;
        }}
        header h1 {{
            font-size: 1.75rem;
            font-weight: 700;
            letter-spacing: -0.02em;
        }}
        header .date {{
            color: #666;
            font-size: 0.9rem;
            margin-top: 0.25rem;
        }}
        h2 {{
            font-size: 1.25rem;
            font-weight: 600;
            margin: 2rem 0 1rem;
            padding-bottom: 0.5rem;
            border-bottom: 1px solid #e5e5e5;
        }}
        h3 {{ font-size: 1.05rem; font-weight: 600; margin: 1.25rem 0 0.25rem; }}
        p {{ margin: 0.5rem 0; color: #333; font-size: 0.95rem; }}
        a {{ color: #1a1a1a; text-decoration-color: #999; }}
        a:hover {{ text-decoration-color: #1a1a1a; }}
        ul {{ padding-left: 1.25rem; margin: 0.5rem 0; }}
        li {{ margin: 0.4rem 0; font-size: 0.95rem; }}
        footer {{
            margin-top: 3rem;
            padding-top: 1rem;
            border-top: 1px solid #e5e5e5;
            color: #999;
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

    page = build_html(digest_content)
    output_path = os.path.join(OUT_DIR, "digest.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(page)
    print("Wrote digest.html")


if __name__ == "__main__":
    main()
