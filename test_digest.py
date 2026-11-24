import datetime
import unittest
from unittest import mock

import feedparser

import generate_digest
from generate_digest import clean_digest, fetch_feed_any, rss2json_to_rss


class CleanDigest(unittest.TestCase):
    def test_keeps_clean_html(self):
        html = "<h2>World</h2><p>Story.</p>"
        self.assertEqual(clean_digest(html), html)

    def test_drops_model_preamble(self):
        out = clean_digest("This confirms the expected output format.\n\n<h2>World</h2><p>Story.</p>")
        self.assertEqual(out, "<h2>World</h2><p>Story.</p>")

    def test_ignores_bare_h2_in_preamble(self):
        out = clean_digest("I will use <h2>-delimited sections.\n<h2>India</h2><p>Story.</p>")
        self.assertTrue(out.startswith("<h2>India</h2>"))

    def test_strips_code_fences(self):
        self.assertEqual(clean_digest("```html\n<h2>World</h2><p>x</p>\n```"), "<h2>World</h2><p>x</p>")

    def test_rejects_markdown_only(self):
        with self.assertRaises(ValueError):
            clean_digest("## World\n\nStory.")


class Rss2Json(unittest.TestCase):
    def payload(self, **over):
        p = {
            "status": "ok",
            "feed": {"title": "Scroll & Co"},
            "items": [{"title": "A < B", "link": "https://example.com/a?x=1&y=2",
                       "pubDate": "2026-10-07 02:03:04", "content": "<p>Body</p>"}],
        }
        p.update(over)
        return p

    def test_entries_survive_with_dates(self):
        feed = feedparser.parse(rss2json_to_rss(self.payload()))
        self.assertEqual(feed.feed.title, "Scroll & Co")
        entry = feed.entries[0]
        self.assertEqual(entry.title, "A < B")
        self.assertEqual(entry.link, "https://example.com/a?x=1&y=2")
        self.assertEqual(tuple(entry.published_parsed[:6]), (2026, 10, 7, 2, 3, 4))

    def test_upstream_error_raises(self):
        with self.assertRaises(RuntimeError):
            rss2json_to_rss({"status": "error", "message": "Cannot download this RSS feed"})

    def test_bad_date_does_not_crash(self):
        items = [{"title": "T", "link": "https://example.com", "pubDate": "not a date"}]
        feed = feedparser.parse(rss2json_to_rss(self.payload(items=items)))
        self.assertEqual(len(feed.entries), 1)


class FeedFallback(unittest.TestCase):
    info = {"name": "X", "url": "https://a.test/f", "fallbacks": ["https://b.test/f"]}
    cutoff = datetime.datetime(2026, 11, 23, tzinfo=datetime.timezone.utc)
    old = "Sun, 01 Nov 2026 00:00:00 GMT"
    new = "Tue, 24 Nov 2026 00:00:00 GMT"

    def run_with(self, routes, cutoff):
        def fake(url):
            r = routes[url.split("/")[2]]
            if isinstance(r, Exception):
                raise r
            items = "".join(f"<item><title>{d}</title><pubDate>{d}</pubDate></item>" for d in r)
            return feedparser.parse(f'<rss version="2.0"><channel>{items}</channel></rss>')
        with mock.patch.object(generate_digest, "fetch_feed", fake):
            return [e.title for e in fetch_feed_any(self.info, cutoff).entries]

    def test_stale_route_falls_through(self):
        self.assertEqual(self.run_with({"a.test": [self.old], "b.test": [self.new]}, self.cutoff), [self.new])

    def test_all_stale_returns_first_not_error(self):
        self.assertEqual(self.run_with({"a.test": [self.old], "b.test": RuntimeError("403")}, self.cutoff), [self.old])

    def test_no_cutoff_keeps_first_answer(self):
        self.assertEqual(self.run_with({"a.test": [self.old], "b.test": [self.new]}, None), [self.old])

    def test_all_down_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_with({"a.test": RuntimeError("503"), "b.test": RuntimeError("403")}, self.cutoff)


if __name__ == "__main__":
    unittest.main()
