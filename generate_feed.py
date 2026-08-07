import datetime
import html
import os
import re
import shutil

import requests

WP_API = "https://cms.thewire.in/wp-json/wp/v2/posts"
WP_CATEGORIES_API = "https://cms.thewire.in/wp-json/wp/v2/categories"
SITE_URL = "https://thewire.in"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "TheWireRSS/1.0"})
FEED_TITLE = "The Wire"
FEED_DESCRIPTION = (
    "The Wire - Independent journalism from India covering politics, "
    "economy, science, law, society, culture, and more."
)
OUT_DIR = os.path.join(os.path.dirname(__file__), "public")


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text).strip()


def extract_first_image(html_content):
    """Extract the first img src from HTML content as a fallback thumbnail."""
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_content)
    if match:
        return match.group(1)
    return None


def clean_content(html_content):
    """Clean WordPress content for proper RSS display."""
    # Remove script tags and their content
    html_content = re.sub(r"<script[^>]*>.*?</script>", "", html_content, flags=re.DOTALL)
    # Remove noscript tags
    html_content = re.sub(r"<noscript[^>]*>.*?</noscript>", "", html_content, flags=re.DOTALL)
    # Remove inline style tags
    html_content = re.sub(r"<style[^>]*>.*?</style>", "", html_content, flags=re.DOTALL)
    # Remove data-* attributes (WP clutter)
    html_content = re.sub(r'\s+data-\w+="[^"]*"', "", html_content)
    # Remove loading="lazy" and decoding="async" (not needed in feeds)
    html_content = re.sub(r'\s+(?:loading|decoding)="[^"]*"', "", html_content)
    # Remove srcset and sizes attributes (causes clutter, src is enough)
    html_content = re.sub(r'\s+srcset="[^"]*"', "", html_content)
    html_content = re.sub(r'\s+sizes="[^"]*"', "", html_content)
    # Clean up excessive whitespace
    html_content = re.sub(r"\n{3,}", "\n\n", html_content)
    return html_content.strip()


def escape_xml(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def format_rfc822(dt_str):
    dt = datetime.datetime.fromisoformat(dt_str)
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0530")


def fetch_posts(count=30, category_id=None):
    params = {
        "per_page": count,
        "_embed": "author,wp:term,wp:featuredmedia",
        "orderby": "date",
        "order": "desc",
    }
    if category_id:
        params["categories"] = category_id
    resp = SESSION.get(WP_API, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_categories():
    """Fetch top-level categories from The Wire."""
    params = {"per_page": 100, "orderby": "count", "order": "desc"}
    resp = SESSION.get(WP_CATEGORIES_API, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def build_rss(posts, feed_url, base_url="", title=FEED_TITLE, description=FEED_DESCRIPTION):
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    items = []
    for post in posts:
        post_title = escape_xml(html.unescape(post["title"]["rendered"]))
        link = escape_xml(post["link"])
        pub_date = format_rfc822(post["date"])
        post_description = escape_xml(
            strip_html(html.unescape(post["excerpt"]["rendered"]))
        )
        content = post["content"]["rendered"]
        guid = escape_xml(post["guid"]["rendered"])

        author = "The Wire"
        embedded = post.get("_embedded", {})
        authors = embedded.get("author", [])
        if authors and authors[0].get("name"):
            author = escape_xml(authors[0]["name"])

        # Extract featured image for thumbnail and hero image
        thumbnail_xml = ""
        hero_html = ""
        featured_media = embedded.get("wp:featuredmedia", [])
        if featured_media and featured_media[0].get("source_url"):
            fm = featured_media[0]
            img_url = fm["source_url"]
            mime_type = fm.get("mime_type", "image/jpeg")
            alt_text = fm.get("alt_text", "")
            caption_html = fm.get("caption", {}).get("rendered", "")
            caption_text = strip_html(caption_html) if caption_html else ""
            # Thumbnail for RSS reader list view (both tags for broad reader support)
            escaped_img = escape_xml(img_url)
            thumbnail_xml = (
                f'      <media:content url="{escaped_img}" medium="image" type="{mime_type}"/>\n'
                f'      <media:thumbnail url="{escaped_img}"/>\n'
                f'      <enclosure url="{escaped_img}" type="{mime_type}" length="0"/>\n'
            )
            # Hero image at top of content, matching The Wire's layout
            hero_html = f'<figure style="margin:0 0 1.5em 0;"><img src="{img_url}" alt="{alt_text}" style="max-width:100%;height:auto;display:block;"/>'
            if caption_text:
                hero_html += f'<figcaption style="font-size:0.85em;color:#666;margin-top:0.4em;">{caption_text}</figcaption>'
            hero_html += "</figure>\n"
        else:
            # Use The Wire logo as fallback thumbnail so RSS readers
            # don't render an empty image preview placeholder.
            placeholder_url = f"{base_url}/placeholder.png" if base_url else "placeholder.png"
            escaped_ph = escape_xml(placeholder_url)
            thumbnail_xml = (
                f'      <media:content url="{escaped_ph}" medium="image" type="image/png"/>\n'
                f'      <media:thumbnail url="{escaped_ph}"/>\n'
                f'      <enclosure url="{escaped_ph}" type="image/png" length="0"/>\n'
            )

        # Clean and prepare the article content
        content = clean_content(content)
        full_content = hero_html + content

        categories_xml = ""
        terms = embedded.get("wp:term", [])
        if terms:
            for term_group in terms:
                for term in term_group:
                    if term.get("taxonomy") == "category":
                        cat_name = escape_xml(html.unescape(term["name"]))
                        categories_xml += (
                            f"      <category>{cat_name}</category>\n"
                        )

        items.append(
            f"""    <item>
      <title>{post_title}</title>
      <link>{link}</link>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{pub_date}</pubDate>
      <dc:creator>{author}</dc:creator>
      <description>{post_description}</description>
      <content:encoded><![CDATA[{full_content}]]></content:encoded>
{thumbnail_xml}{categories_xml}    </item>"""
        )

    items_xml = "\n".join(items)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:atom="http://www.w3.org/2005/Atom"
  xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>{escape_xml(title)}</title>
    <link>{escape_xml(SITE_URL)}</link>
    <description>{escape_xml(description)}</description>
    <language>en</language>
    <lastBuildDate>{now}</lastBuildDate>
    <atom:link href="{escape_xml(feed_url)}" rel="self" type="application/rss+xml"/>
{items_xml}
  </channel>
</rss>"""


def build_index(base_url, category_feeds):
    cat_links = "\n".join(
        f'              <li><a href="{slug}.xml" onclick="copyFeed(event, \'{base_url}/{slug}.xml\')"><span class="rss-icon">&#9673;</span> {name}</a></li>'
        for slug, name in sorted(category_feeds, key=lambda x: x[1])
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>indie-feeds — RSS directory for independent media</title>
  <meta name="description" content="A curated collection of RSS feeds for independent journalism — custom-generated feeds for sites without RSS, plus a directory of native feeds from trusted sources worldwide.">
  <meta property="og:title" content="indie-feeds — RSS directory for independent media">
  <meta property="og:description" content="Custom-generated and curated RSS feeds for independent journalism. Subscribe in any RSS reader.">
  <meta property="og:type" content="website">
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      color: #e0e0e0;
      line-height: 1.6;
      background: #000;
    }}

    .hero {{
      background: #111;
      padding: 3rem 1.5rem 2.5rem;
      text-align: center;
    }}
    .hero h1 {{
      font-size: 2.2rem;
      font-weight: 800;
      letter-spacing: -0.03em;
      margin-bottom: 0.6rem;
    }}
    .hero h1 span {{
      color: #e8734a;
    }}
    .hero p {{
      max-width: 540px;
      margin: 0 auto;
      color: #a0a0a0;
      font-size: 1.05rem;
      line-height: 1.5;
    }}
    .hero .badge {{
      display: inline-block;
      margin-top: 1.2rem;
      padding: 0.35rem 0.9rem;
      background: rgba(232, 115, 74, 0.15);
      color: #e8734a;
      border-radius: 20px;
      font-size: 0.8rem;
      font-weight: 600;
      letter-spacing: 0.03em;
    }}

    .container {{
      max-width: 760px;
      margin: 0 auto;
      padding: 0 1.5rem;
    }}

    .section {{
      margin-top: 2.5rem;
    }}
    .section-header {{
      display: flex;
      align-items: center;
      gap: 0.6rem;
      margin-bottom: 1rem;
    }}
    .section-header h2 {{
      font-size: 1.15rem;
      font-weight: 700;
      color: #f0f0f0;
    }}
    .feed-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 0.5rem;
    }}
    .feed-card {{
      background: #111;
      border: 1px solid #2a2a2a;
      border-radius: 8px;
      padding: 0.9rem 1.1rem;
      transition: border-color 0.15s, box-shadow 0.15s;
    }}
    .feed-card:hover {{
      border-color: #444;
      box-shadow: 0 2px 8px rgba(255,255,255,0.04);
    }}
    .feed-card h3 {{
      font-size: 0.95rem;
      font-weight: 650;
      margin-bottom: 0.15rem;
    }}
    .feed-card .desc {{
      font-size: 0.82rem;
      color: #888;
      margin-bottom: 0.5rem;
    }}
    .feed-card .feed-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
    }}
    .feed-card .feed-links a {{
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      font-size: 0.78rem;
      padding: 0.25rem 0.6rem;
      background: #1a1a1a;
      border: 1px solid #333;
      border-radius: 5px;
      color: #ccc;
      text-decoration: none;
      font-family: 'SF Mono', 'Fira Code', monospace;
      transition: background 0.15s, border-color 0.15s;
      cursor: pointer;
    }}
    .feed-card .feed-links a:hover {{
      background: #222;
      border-color: #555;
    }}
    .feed-card .feed-links a .rss-icon {{
      color: #e8734a;
      font-size: 0.7rem;
    }}

    .wire-categories {{
      margin-top: 0.5rem;
    }}
    .wire-categories summary {{
      font-size: 0.8rem;
      color: #888;
      cursor: pointer;
      padding: 0.3rem 0;
    }}
    .wire-categories summary:hover {{
      color: #aaa;
    }}
    .wire-categories ul {{
      list-style: none;
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem;
      margin-top: 0.4rem;
    }}
    .wire-categories ul li a {{
      font-size: 0.75rem;
    }}

    .how-to {{
      margin-top: 2.5rem;
      padding: 1.5rem;
      background: #111;
      border: 1px solid #2a2a2a;
      border-radius: 8px;
    }}
    .how-to h2 {{
      font-size: 1rem;
      font-weight: 700;
      color: #f0f0f0;
      margin-bottom: 0.8rem;
    }}
    .how-to ol {{
      padding-left: 1.2rem;
    }}
    .how-to li {{
      font-size: 0.88rem;
      color: #aaa;
      margin-bottom: 0.4rem;
    }}
    .how-to code {{
      background: #1a1a1a;
      padding: 1px 5px;
      border-radius: 3px;
      font-size: 0.82em;
    }}

    footer {{
      margin-top: 2.5rem;
      padding: 1.5rem 0;
      border-top: 1px solid #2a2a2a;
      text-align: center;
      color: #666;
      font-size: 0.82rem;
    }}
    footer a {{
      color: #888;
      text-decoration: none;
    }}
    footer a:hover {{
      color: #e0e0e0;
    }}

    @media (max-width: 500px) {{
      .hero h1 {{ font-size: 1.7rem; }}
      .hero p {{ font-size: 0.95rem; }}
      .feed-card {{ padding: 0.75rem 0.9rem; }}
    }}
  </style>
</head>
<body>

  <div class="hero">
    <h1>indie<span>-</span>feeds</h1>
    <p>Custom-generated RSS feeds for independent media sites that don't offer RSS. Fresh every hour.</p>
    <div class="badge">Updated hourly</div>
  </div>

  <div class="container">

    <div class="section">
      <div class="section-header">
        <h2>Feeds</h2>
      </div>

      <div class="feed-grid">
        <div class="feed-card">
          <h3>The Wire</h3>
          <div class="desc">Independent news and opinion from India</div>
          <div class="feed-links">
            <a href="feed.xml" onclick="copyFeed(event, '{base_url}/feed.xml')"><span class="rss-icon">&#9673;</span> feed.xml</a>
          </div>
          <details class="wire-categories">
            <summary>Category feeds ({len(category_feeds)})</summary>
            <ul>
{cat_links}
            </ul>
          </details>
        </div>

        <div class="feed-card">
          <h3>The Caravan</h3>
          <div class="desc">Long-form journalism on politics and culture</div>
          <div class="feed-links">
            <a href="caravan.xml" onclick="copyFeed(event, '{base_url}/caravan.xml')"><span class="rss-icon">&#9673;</span> caravan.xml</a>
          </div>
        </div>
      </div>
    </div>


    <div class="how-to">
      <h2>How to use these feeds</h2>
      <ol>
        <li>Pick an RSS reader — <strong>Inoreader</strong>, <strong>Feedly</strong>, <strong>NetNewsWire</strong>, <strong>Newsblur</strong>, or any other</li>
        <li>Click a feed link or <em>Copy RSS</em> button above</li>
        <li>Paste the URL into your reader's "Add Feed" or "Subscribe" option</li>
      </ol>
      <p style="font-size:0.82rem;color:#888;margin-top:0.8rem;">New to RSS? It's the best way to follow news without algorithms deciding what you see.</p>
    </div>

    <footer>
      <p>Open source on <a href="https://github.com/athibanvasanth/indie-feeds">GitHub</a> &middot; Custom feeds update hourly via GitHub Actions</p>
      <p style="margin-top:0.3rem;">Built because independent journalism deserves open distribution.</p>
    </footer>

  </div>

  <script>
    function copyFeed(e, url) {{
      if (url) {{
        e.preventDefault();
        navigator.clipboard.writeText(url).then(function() {{
          var el = e.currentTarget;
          var orig = el.innerHTML;
          el.innerHTML = '<span class="rss-icon">&#10003;</span> Copied!';
          setTimeout(function() {{ el.innerHTML = orig; }}, 1500);
        }});
      }}
    }}
  </script>

</body>
</html>"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # Copy static assets
    script_dir = os.path.dirname(__file__)
    placeholder_src = os.path.join(script_dir, "placeholder.png")
    if os.path.exists(placeholder_src):
        shutil.copy2(placeholder_src, os.path.join(OUT_DIR, "placeholder.png"))

    # Determine base URL from environment or default
    base_url = os.environ.get("BASE_URL", "").rstrip("/")

    # Generate main feed
    print("Fetching main feed...")
    posts = fetch_posts(30)
    feed_url = f"{base_url}/feed.xml" if base_url else "feed.xml"
    rss = build_rss(posts, feed_url, base_url=base_url)
    with open(os.path.join(OUT_DIR, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"  Wrote feed.xml ({len(posts)} posts)")

    # Fetch categories and generate per-category feeds
    print("Fetching categories...")
    categories = fetch_categories()
    # Filter to categories with a reasonable number of posts
    categories = [c for c in categories if c.get("count", 0) > 10]
    print(f"  Found {len(categories)} categories")

    category_feeds = []
    for cat in categories:
        slug = cat["slug"]
        name = html.unescape(cat["name"])
        cat_id = cat["id"]
        print(f"  Fetching category: {name} ({slug})...")
        try:
            cat_posts = fetch_posts(30, category_id=cat_id)
        except Exception as e:
            print(f"    Error fetching {slug}: {e}")
            continue
        cat_feed_url = f"{base_url}/{slug}.xml" if base_url else f"{slug}.xml"
        cat_rss = build_rss(
            cat_posts,
            cat_feed_url,
            base_url=base_url,
            title=f"The Wire - {name}",
            description=f"Latest articles from The Wire in the {name} category.",
        )
        with open(os.path.join(OUT_DIR, f"{slug}.xml"), "w", encoding="utf-8") as f:
            f.write(cat_rss)
        category_feeds.append((slug, name))
        print(f"    Wrote {slug}.xml ({len(cat_posts)} posts)")

    # Generate index page
    index_html = build_index(base_url, category_feeds)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)
    print("Wrote index.html")
    print("Done!")


if __name__ == "__main__":
    main()
