# indie-feeds

RSS feed generators for sites that don't publish one, plus a daily digest. Deployed to GitHub Pages by GitHub Actions.

## Traps — read before changing the workflow

**Both crons live in ONE workflow file.** `generate-feed.yml` (named "Generate RSS Feed") runs hourly (`0 * * * *`) *and* carries the daily digest as a **conditional step**, not a separate workflow:

```yaml
if: github.event.schedule == '15 1 * * *' || workflow_dispatch
```

So `gh workflow list` correctly shows only one workflow. **Don't "fix" a missing daily-digest workflow — there isn't one to find.** The hourly runs re-serve the existing digest via a "Preserve existing digest" curl step so it never 404s between regenerations.

**This repo is under a GitHub abuse flag: Actions WRITE tokens are blocked.** A workflow requesting `contents: write` fails at Set-up-job with "Repository access blocked". Read-only runs (Pages deploy: `pages:write` + `id-token`, `contents:read`) work fine.

So: no `contents: write`, no auto-commits, no `keepalive-workflow` — don't re-add them. GitHub's 60-day scheduled-workflow auto-disable therefore can't be solved by a keepalive; reset it with a **user push** (those aren't blocked) or via Manage workflow → Enable.

## Digest sources

`generate_digest.py` pulls 5 newsletters + 4 RSS feeds + a TLDR scrape, then makes one Gemini pass into static HTML. The footer prints a per-source health line (`hz-ok` / `hz-quiet` / `hz-fail`) — a workflow run goes green even when a source silently returns zero articles, so that line is the only real signal.

**Accepted failure:** `Boris Cherny ✗`. His posts are X-only and every free X→RSS route is dead or datacenter-IP-blocked. Not a bug — don't re-investigate each time.

**Real failure:** `Scroll Newsletter ✗`. Since `7c1f1ba` it routes through rss2json (Substack 403s GitHub Actions' IP range directly, though it's clean from a residential IP). A `✗` here means rss2json is down, rate-limiting, or changed shape — **that is a genuine regression, flag it.**

If a *third* source starts failing, escalate regardless.

## Gotchas

- `fetch_feed()` detects a JSON body and rebuilds RSS from it via `rss2json_to_rss()`. Dates must be converted to RFC822 — feedparser leaves `published_parsed` empty otherwise, and the 28-hour freshness cutoff then silently drops every entry.
- Scroll sits in `NEWSLETTER_FEEDS`, not `RSS_FEEDS`: its Daily Brief is multi-story and needs the full-content handler. Under `RSS_FEEDS` it gets capped at 800 chars *and* tries to fetch the article body from Substack — a second 403.
