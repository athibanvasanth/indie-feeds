# indie-feeds

RSS feed generators for sites that don't publish one, plus a daily digest. Deployed to GitHub Pages by GitHub Actions; base URL `athibanvasanth.github.io/indie-feeds/`.

Each generator uses whatever hook that site actually exposes: **The Wire** via its WordPress REST API, **Scroll Newsletter** via feed2json.org (Substack 403s GitHub Actions' IP range directly), **The Caravan** from JSON-LD.

## Traps — read before changing the workflow

**The daily digest lives inside the hourly workflow, not a separate one.** `generate-feed.yml` (named "Generate RSS Feed") runs hourly (`0 * * * *`) and *every* run attempts the digest step — `gh workflow list` shows this workflow plus GitHub's own automatic `Dependency Graph`; there is no separate daily-digest workflow to look for.

**Digest generation used to be gated to one exact cron slot (`15 1 * * *`) and GitHub's scheduler silently dropped that slot on ~40% of days (found 2026-09-02, no visible failure — the run just never happened).** Fixed by removing the exact-time gate: every hourly run now reaches `generate_digest.py`, which checks the just-curled live `digest.html`'s title date against today's (UTC) and returns immediately if it's already current — so only the first successful run after UTC midnight does the real (expensive) work. Don't re-add a fixed-time-only gate; it will silently break the same way.

The hourly runs re-serve the existing digest via a "Preserve existing digest" curl step so it never 404s between regenerations.

**This repo is under a GitHub abuse flag: Actions WRITE tokens are blocked.** A workflow requesting `contents: write` fails at Set-up-job with "Repository access blocked". Read-only runs (Pages deploy: `pages:write` + `id-token`, `contents:read`) work fine.

So: no `contents: write`, no auto-commits, no `keepalive-workflow` — don't re-add them. GitHub's 60-day scheduled-workflow auto-disable therefore can't be solved by a keepalive; reset it with a **user push** (those aren't blocked) or via Manage workflow → Enable.

## Digest sources

`generate_digest.py` pulls 5 newsletters (Guardian, NYT, The Wire, The Hindu, Scroll) + 10 RSS feeds (Coimbatore, The News Minute, Indian Express, The Wire's own `feed.xml`, Mint, The Caravan's own `caravan.xml`, Al Jazeera, Democracy Now, Simon Willison, Rest of World, Boris Cherny's blog; busy ones capped by `max`) + 8 TLDR items, then makes one Claude Sonnet 5 pass into static HTML — via the Claude Code CLI in print mode (`claude -p --model claude-sonnet-5`), authenticated with `CLAUDE_CODE_OAUTH_TOKEN` so it bills against the subscription, not a metered API key. (Was opencode Zen's free Muse Spark 1.2 before 2026-09-01; Gemini before that.) **If the digest step fails with "organization has disabled Claude subscription access"**, the token's Claude account has lost its Pro subscription (happened 2026-10-01 after moving to a new account): while logged into the active account run `claude setup-token`, then `gh secret set CLAUDE_CODE_OAUTH_TOKEN -R athibanvasanth/indie-feeds`. The footer prints a per-source health line (`hz-ok` / `hz-quiet` / `hz-fail`) — a workflow run goes green even when a source silently returns zero articles, so that line is the only real signal.

**Boris Cherny is quiet by design.** His posts are X-only and every free X→RSS route is dead or datacenter-IP-blocked (Nitter instances gated/403, `nitter.net` answered **410 Gone** on 2026-10-21; xcancel fakes entries; openrss and public RSSHub disabled; the syndication API needs a signed token). Since 2026-10-21 the source is his blog, `borischerny.com/feed.xml` — rock-stable but roughly yearly, so the health line usually shows `Boris Cherny 0` (quiet), not ✗. A ✗ now means the blog itself failed. The only durable route to his daily posts would be self-hosting RSSHub on the Actual Budget GCP VM with a burner X `auth_token` cookie (setup + cookie upkeep).

**Real failure:** `Scroll Newsletter ✗`. Substack 403s GitHub Actions' IP range directly (clean from a residential IP), so Scroll goes through a converter that fetches from its own host. Route ladder, tried in order by `fetch_feed_any()`: **feed2json.org** (JSON Feed, since 2026-10-21) → rss2json (since `7c1f1ba`; from 2026-10-21 it answers every request with a Cloudflare "Just a moment" 403) → Substack direct. A `✗` means every route failed — **a genuine regression, flag it**; the run log shows which route failed and why.

If a *third* source starts failing, escalate regardless.

## Page layout (since 2026-10-21)

- **Dates That Affect You** (only when something qualifies): official deadlines from the news plus `FIXED_DEADLINES` (yearly tax dates, shown 30-45 days ahead). General dates only: the site is public, so his personal deadlines live in the private dotfiles memory (`reference_personal_deadlines.md`), never here.
- **Today's Briefing**: summary paragraph + 8-12 `<details class="story">` items: one line closed, deeper write-up open. Top Stories was merged into it. Each story is tagged New or Update against yesterday's digest (`previous_digest_text()` reads the curled live page before it's overwritten).
- India / World / Tech use the same story items; TLDR Highlights and Also Worth Reading stay lists. Badges show story count and reading time (quick vs in full); "Expand all" opens everything.
- Balance rules in the prompt: no outlet more than 3 items per section, every news-carrying newsletter contributes, The Wire is preferred as the cited source (his favourite), world news reaches beyond the US/Europe.

## Gotchas

- `fetch_feed()` detects a JSON body and rebuilds RSS from it via `jsonfeed_to_rss()` (feed2json) or `rss2json_to_rss()`. Dates must be converted to RFC822 — feedparser leaves `published_parsed` empty otherwise, and the 28-hour freshness cutoff then silently drops every entry.
- Scroll sits in `NEWSLETTER_FEEDS`, not `RSS_FEEDS`: its Daily Brief is multi-story and needs the full-content handler. Under `RSS_FEEDS` it gets capped at 800 chars *and* tries to fetch the article body from Substack — a second 403.
- Newsletter text is capped at 16,000 chars each (`fetch_newsletter_content`); it was 4,000 until 2026-10-21, which kept only a quarter to a third of the Guardian, NYT and Hindu editions.
- `generate_feed.py` fetches The Wire's categories 6 at a time (~80s → ~20s); the workflow sets `PYTHONUNBUFFERED=1` so log lines carry real timestamps.
- The kill-the-newsletter feed tokens in `generate_digest.py`'s `NEWSLETTER_FEEDS` are inherently public — public repo, public Pages site. That's a deliberate design choice, not a leak: they only gate inbox delivery, nothing sensitive rides on them.
