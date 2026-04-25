# indie-feeds

Custom RSS feed generators for independent media sites that don't offer RSS, plus an AI-summarized daily digest. Auto-generated via GitHub Actions and served through GitHub Pages.

**Live site:** [athibanvasanth.github.io/indie-feeds](https://athibanvasanth.github.io/indie-feeds/)

## Generated Feeds

| Site | Strategy | Feed |
|------|----------|------|
| The Wire | WordPress REST API (`/wp-json/wp/v2/`) | [feed.xml](https://athibanvasanth.github.io/indie-feeds/feed.xml) |
| Scroll Newsletter | Pinia state extraction from page source | [scroll.xml](https://athibanvasanth.github.io/indie-feeds/scroll.xml) |
| The Caravan | JSON-LD structured data | [caravan.xml](https://athibanvasanth.github.io/indie-feeds/caravan.xml) |

The Wire also generates ~50 per-category feeds (politics, rights, economy, etc.) — see the [live site](https://athibanvasanth.github.io/indie-feeds/) for the full list.

## Daily Digest

An AI-summarized digest of the day's news, generated once a day and published at [digest.html](https://athibanvasanth.github.io/indie-feeds/digest.html).

- **Newsletters** (full content via [kill-the-newsletter](https://kill-the-newsletter.com/)): Guardian, NYTimes, The Wire Daily, The Hindu
- **RSS articles**: The Hindu (Coimbatore), Democracy Now!, Scroll Newsletter, Simon Willison, Boris Cherny
- **Tech**: TLDR Tech (scraped daily)

`generate_digest.py` fetches everything from the last 28 hours, decodes tracking URLs (awstrack, ablink, piano.io), and feeds it to Gemini 2.5 Flash to produce a structured HTML digest with sections for Today's Briefing, Top Stories, India, World, Tech, and more.

## How It Works

Each generator script targets a different site using whatever structured data is available:

- `generate_feed.py` — The Wire (WordPress API, fetches categories dynamically)
- `generate_scroll_feed.py` — Scroll Newsletter (Pinia/Stck.me state extraction)
- `generate_caravan_feed.py` — The Caravan (JSON-LD structured data)
- `generate_digest.py` — Daily digest (newsletters + RSS + TLDR → Gemini summary)

All feeds are RSS 2.0 with media thumbnails, full HTML content, author info, and categories.

## Setup

```bash
pip install -r requirements.txt

export BASE_URL="https://athibanvasanth.github.io/indie-feeds"
python generate_feed.py
python generate_scroll_feed.py
python generate_caravan_feed.py

# digest also needs a Gemini API key
export GEMINI_API_KEY="..."
python generate_digest.py

# output lands in the public/ directory
```

## Deployment

GitHub Actions runs the feed generators every 30 minutes and the daily digest once a day at 01:15 UTC (≈06:45 IST), then deploys everything to GitHub Pages via `actions/deploy-pages`.
