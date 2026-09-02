# indie-feeds

RSS feed generators for sites that don't publish one, plus a daily digest. Deployed to GitHub Pages by GitHub Actions; base URL `athibanvasanth.github.io/indie-feeds/`.

Each generator uses whatever hook that site actually exposes: **The Wire** via its WordPress REST API, **Scroll Newsletter** via rss2json (Substack 403s GitHub Actions' IP range directly), **The Caravan** from JSON-LD.

## Traps — read before changing the workflow

**The daily digest lives inside the hourly workflow, not a separate one.** `generate-feed.yml` (named "Generate RSS Feed") runs hourly (`0 * * * *`) and *every* run attempts the digest step — `gh workflow list` correctly shows only one workflow, don't go looking for a missing daily-digest one.

**Digest generation used to be gated to one exact cron slot (`15 1 * * *`) and GitHub's scheduler silently dropped that slot on ~40% of days (found 2026-09-02, no visible failure — the run just never happened).** Fixed by removing the exact-time gate: every hourly run now reaches `generate_digest.py`, which checks the just-curled live `digest.html`'s title date against today's (UTC) and returns immediately if it's already current — so only the first successful run after UTC midnight does the real (expensive) work. Don't re-add a fixed-time-only gate; it will silently break the same way.

The hourly runs re-serve the existing digest via a "Preserve existing digest" curl step so it never 404s between regenerations.

**This repo is under a GitHub abuse flag: Actions WRITE tokens are blocked.** A workflow requesting `contents: write` fails at Set-up-job with "Repository access blocked". Read-only runs (Pages deploy: `pages:write` + `id-token`, `contents:read`) work fine.

So: no `contents: write`, no auto-commits, no `keepalive-workflow` — don't re-add them. GitHub's 60-day scheduled-workflow auto-disable therefore can't be solved by a keepalive; reset it with a **user push** (those aren't blocked) or via Manage workflow → Enable.

## Digest sources

`generate_digest.py` pulls 5 newsletters + 4 RSS feeds + a TLDR scrape, then makes one Claude Sonnet 5 pass into static HTML — via the Claude Code CLI in print mode (`claude -p --model claude-sonnet-5`), authenticated with `CLAUDE_CODE_OAUTH_TOKEN` so it bills against the subscription, not a metered API key. (Was opencode Zen's free Muse Spark 1.2 before 2026-09-01; Gemini before that.) The footer prints a per-source health line (`hz-ok` / `hz-quiet` / `hz-fail`) — a workflow run goes green even when a source silently returns zero articles, so that line is the only real signal.

**Accepted failure:** `Boris Cherny ✗`. His posts are X-only and every free X→RSS route is dead or datacenter-IP-blocked — Nitter instances gated/bot-challenged/403, xcancel serves a fake "not whitelisted" entry, openrss and public RSSHub disabled, the syndication API needs a signed token. Currently on `nitter.net/bcherny/rss`, the last flagship instance serving real tweets, though it's flaky from GitHub's datacenter IPs. Not a bug — don't re-investigate each run.

If it dies for good, the fallback ladder is: (1) another live Nitter instance, (2) `borischerny.com/feed.xml` — his blog, rock-stable but roughly yearly, so the digest just stays quiet, (3) self-host RSSHub on the Actual Budget GCP VM with a burner X `auth_token` cookie — the only durable free route to his daily posts, but it needs setup and cookie upkeep.

**Real failure:** `Scroll Newsletter ✗`. Since `7c1f1ba` it routes through rss2json (Substack 403s GitHub Actions' IP range directly, though it's clean from a residential IP). A `✗` here means rss2json is down, rate-limiting, or changed shape — **that is a genuine regression, flag it.**

If a *third* source starts failing, escalate regardless.

## Gotchas

- `fetch_feed()` detects a JSON body and rebuilds RSS from it via `rss2json_to_rss()`. Dates must be converted to RFC822 — feedparser leaves `published_parsed` empty otherwise, and the 28-hour freshness cutoff then silently drops every entry.
- Scroll sits in `NEWSLETTER_FEEDS`, not `RSS_FEEDS`: its Daily Brief is multi-story and needs the full-content handler. Under `RSS_FEEDS` it gets capped at 800 chars *and* tries to fetch the article body from Substack — a second 403.
- The kill-the-newsletter feed tokens in `generate_digest.py`'s `NEWSLETTER_FEEDS` (lines 16-19) are inherently public — public repo, public Pages site. That's a deliberate design choice, not a leak: they only gate inbox delivery, nothing sensitive rides on them.
