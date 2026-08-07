import datetime
import json
import os
import re

import requests

CARAVAN_URL = "https://caravanmagazine.in"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CaravanRSS/1.0"})
OUT_DIR = os.path.join(os.path.dirname(__file__), "public")

SKIP_PREFIXES = ("/pages/", "/magazine/", "/sponsored-feature/", "/archives")


def escape_xml(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def fetch_article_urls():
    resp = SESSION.get(CARAVAN_URL, timeout=30)
    resp.raise_for_status()
    pattern = r'<a[^>]*href="(/[a-z][^"]+)"[^>]*>.*?<h[1-6][^>]*>(.*?)</h[1-6]>'
    matches = re.findall(pattern, resp.text, re.DOTALL)
    seen = set()
    urls = []
    for url, _ in matches:
        if url in seen:
            continue
        if any(url.startswith(p) for p in SKIP_PREFIXES):
            continue
        # skip bare category links like /politics with no slug after
        parts = url.strip("/").split("/")
        if len(parts) < 2:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def is_article_node(data):
    node_type = data.get("@type")
    return node_type == "Article" or (
        isinstance(node_type, list) and "Article" in node_type
    )


def fetch_article_meta(path):
    url = f"{CARAVAN_URL}{path}"
    try:
        resp = SESSION.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"    Error fetching {path}: {e}")
        return None

    # Extract JSON-LD
    ld_match = re.search(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        resp.text,
        re.DOTALL,
    )
    if not ld_match:
        print(f"    Dropping {path}: no JSON-LD block found")
        return None

    try:
        data = json.loads(ld_match.group(1))
    except json.JSONDecodeError:
        print(f"    Dropping {path}: JSON-LD failed to parse")
        return None

    # JSON-LD sometimes wraps its nodes in a top-level @graph array
    if isinstance(data.get("@graph"), list):
        data = next(
            (n for n in data["@graph"] if isinstance(n, dict) and is_article_node(n)),
            None,
        )
        if data is None:
            print(f"    Dropping {path}: no Article node in JSON-LD @graph")
            return None

    if not is_article_node(data):
        print(f"    Dropping {path}: JSON-LD @type is {data.get('@type')!r}, not Article")
        return None

    # Extract og:image as fallback (JSON-LD image sometimes missing protocol)
    og_match = re.search(
        r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', resp.text
    )
    image = data.get("image", "")
    if og_match:
        image = og_match.group(1)
    if image.startswith("//"):
        image = "https:" + image

    authors = data.get("author", [])
    if isinstance(authors, dict):
        authors = [authors]
    author_name = authors[0].get("name", "The Caravan") if authors else "The Caravan"

    return {
        "title": data.get("headline", "").strip(),
        "url": url,
        "description": data.get("description", ""),
        "author": author_name,
        "date": data.get("datePublished", ""),
        "image": image,
    }


def format_rfc822(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except (ValueError, AttributeError):
        return ""


def build_rss(articles, feed_url):
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    items = []
    for a in articles:
        title = escape_xml(a["title"])
        link = escape_xml(a["url"])
        desc = escape_xml(a["description"])
        author = escape_xml(a["author"])
        pub_date = format_rfc822(a["date"])
        guid = link

        thumbnail_xml = ""
        if a.get("image"):
            img = escape_xml(a["image"])
            thumbnail_xml = (
                f'      <media:content url="{img}" medium="image" type="image/jpeg"/>\n'
                f'      <media:thumbnail url="{img}"/>\n'
                f'      <enclosure url="{img}" type="image/jpeg" length="0"/>\n'
            )

        # Extract category from URL path
        category_xml = ""
        path = a["url"].replace(CARAVAN_URL, "").strip("/")
        parts = path.split("/")
        if parts:
            cat = parts[0].replace("-", " ").title()
            category_xml = f"      <category>{escape_xml(cat)}</category>\n"

        items.append(
            f"""    <item>
      <title>{title}</title>
      <link>{link}</link>
      <guid isPermaLink="true">{guid}</guid>
      <pubDate>{pub_date}</pubDate>
      <dc:creator>{author}</dc:creator>
      <description>{desc}</description>
{thumbnail_xml}{category_xml}    </item>"""
        )

    items_xml = "\n".join(items)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:atom="http://www.w3.org/2005/Atom"
  xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>The Caravan</title>
    <link>{escape_xml(CARAVAN_URL)}</link>
    <description>The Caravan - A journal of politics and culture from India</description>
    <language>en</language>
    <lastBuildDate>{now}</lastBuildDate>
    <atom:link href="{escape_xml(feed_url)}" rel="self" type="application/rss+xml"/>
{items_xml}
  </channel>
</rss>"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    base_url = os.environ.get("BASE_URL", "").rstrip("/")

    print("Fetching Caravan homepage...")
    try:
        urls = fetch_article_urls()
    except Exception as e:
        print(f"  Failed to fetch Caravan homepage: {e}")
        print("  Skipping Caravan feed generation")
        return
    print(f"  Found {len(urls)} article URLs")

    articles = []
    for path in urls:
        print(f"  Fetching metadata: {path}")
        meta = fetch_article_meta(path)
        if meta:
            articles.append(meta)

    # Sort by date descending
    articles.sort(key=lambda a: a.get("date", ""), reverse=True)

    feed_url = f"{base_url}/caravan.xml" if base_url else "caravan.xml"
    rss = build_rss(articles, feed_url)
    with open(os.path.join(OUT_DIR, "caravan.xml"), "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"Wrote caravan.xml ({len(articles)} articles)")


if __name__ == "__main__":
    main()
