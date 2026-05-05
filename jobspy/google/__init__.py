"""Google Jobs scraper — Playwright + CDP-first.

## Why this is built the way it is

Google in 2026 hardened anti-bot in a way that defeats every standard
browser-automation framework from a fresh request:

  - Vanilla Playwright (headless or headed)        → /sorry/ CAPTCHA
  - Real Chrome via channel="chrome"               → /sorry/ CAPTCHA
  - undetected-chromedriver / nodriver / patchright → /sorry/ CAPTCHA
  - All of the above + MCP's exact launch flags    → /sorry/ CAPTCHA

The single thing that *does* work is connecting (via CDP) to a Chrome
instance that has already accumulated session trust — i.e. a browser
that's been used a few times to view normal Google pages, has cookies,
and has run for more than a few seconds. That's why the long-running
@playwright/mcp browser works while a fresh launch fails — it's profile
warmth, not flags or stealth.

So this scraper:
  1. **CDP-connect** to a running Chrome instance. Two discovery paths:
     a. `CHROME_CDP_URL` env var — point at any Chrome started with
        `--remote-debugging-port=N` (the standard pattern).
     b. Auto-discover a running @playwright/mcp Chromium by scanning for
        a `--remote-debugging-port=` arg in the process list.
  2. If neither is available, log a clear "no warm browser found"
     message and return zero jobs. We don't try to launch our own —
     it'll just CAPTCHA.

The original HTTP scraper is preserved at `__init__.py.http-backup` for
reference / future use if Google ever drops the JS gate.

## Running a warm browser

The cheapest path is Chrome itself:

    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
        --remote-debugging-port=9222 --user-data-dir=$HOME/.cache/jobspy-chrome

Then export `CHROME_CDP_URL=http://127.0.0.1:9222` and run.
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote_plus
from urllib.request import urlopen

from jobspy.google.util import log
from jobspy.model import (
    Country,
    JobPost,
    JobResponse,
    JobType,
    Location,
    Scraper,
    ScraperInput,
    Site,
)


_GOOGLE_JOBS_URL = "https://www.google.com/search?q={query}&udm=8"
_NAV_TIMEOUT_MS = 30_000
_RENDER_TIMEOUT_MS = 12_000


class Google(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.GOOGLE, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self.country: Country | None = None
        # user_agent retained for API compat; ignored when CDP-connecting
        # since we attach to a context that already has its own UA.
        self.user_agent = user_agent

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        query = self._build_query(scraper_input)
        url = _GOOGLE_JOBS_URL.format(query=quote_plus(query))
        log.info(f"google: query={query!r}")

        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error(
                "google: playwright is required. "
                "Install with: pip install playwright && playwright install chromium"
            )
            return JobResponse(jobs=[])

        cdp_url = _discover_cdp_url()
        if not cdp_url:
            log.error(
                "google: no warm Chrome found. Google's anti-bot blocks fresh "
                "browser launches. Start a long-running Chrome with:\n"
                "  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome "
                "--remote-debugging-port=9222 "
                "--user-data-dir=$HOME/.cache/jobspy-chrome\n"
                "Then export CHROME_CDP_URL=http://127.0.0.1:9222 and retry."
            )
            return JobResponse(jobs=[])

        log.info(f"google: connecting to CDP at {cdp_url}")
        raw_cards: list[dict[str, Any]] = []

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                log.error(f"google: failed to connect to CDP {cdp_url}: {e}")
                return JobResponse(jobs=[])

            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                if "/sorry/" in page.url:
                    log.error(
                        f"google: even the warm browser hit /sorry/ CAPTCHA at "
                        f"{page.url[:120]}. The session may need more warmth — "
                        f"open the browser, search Google a few times, accept "
                        f"any consent dialogs, then retry."
                    )
                    return JobResponse(jobs=[])
                try:
                    page.wait_for_selector(
                        '[role="button"][aria-label*="to saves list"]',
                        timeout=_RENDER_TIMEOUT_MS,
                    )
                except PlaywrightTimeout:
                    log.warning(
                        "google: no job-card buttons rendered within timeout — "
                        "Google may be showing 'no results' for this query"
                    )
                    return JobResponse(jobs=[])

                # Light scroll to nudge lazy-loaded cards.
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(800)

                raw_cards = page.evaluate(_EXTRACT_JS)
            finally:
                page.close()

        log.info(f"google: extracted {len(raw_cards)} raw cards")

        jobs: list[JobPost] = []
        seen_keys: set[str] = set()
        wanted = getattr(scraper_input, "results_wanted", 25) or 25
        for card in raw_cards:
            post = _build_job_post(card)
            if post is None:
                continue
            dedupe_key = post.id or f"{post.title}|{post.company_name}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            jobs.append(post)
            if len(jobs) >= wanted:
                break

        log.info(f"google: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)

    def _build_query(self, si: ScraperInput) -> str:
        if si.google_search_term:
            return si.google_search_term
        parts: list[str] = []
        if si.search_term:
            parts.append(si.search_term)
        parts.append("jobs")
        job_type_mapping = {
            JobType.FULL_TIME: "Full time",
            JobType.PART_TIME: "Part time",
            JobType.INTERNSHIP: "Internship",
            JobType.CONTRACT: "Contract",
        }
        if si.job_type in job_type_mapping:
            parts.append(job_type_mapping[si.job_type])
        if si.location:
            parts.append(f"near {si.location}")
        hours_old = getattr(si, "hours_old", None)
        if hours_old:
            if hours_old <= 24:
                parts.append("since yesterday")
            elif hours_old <= 72:
                parts.append("in the last 3 days")
            elif hours_old <= 168:
                parts.append("in the last week")
            else:
                parts.append("in the last month")
        if si.is_remote:
            parts.append("remote")
        return " ".join(parts)


# -----------------------------------------------------------------------------
# CDP discovery — find a running Chrome we can attach to
# -----------------------------------------------------------------------------


def _discover_cdp_url() -> str | None:
    """Return a CDP base URL we can `connect_over_cdp` to, or None.

    Checks in order:
      1. $CHROME_CDP_URL  (e.g. "http://127.0.0.1:9222")
      2. $JOBSPY_CHROME_CDP_PORT (just the port, host = 127.0.0.1)
      3. Common port 9222 (the standard Chrome remote-debug port)
      4. Auto-detect: any running playwright-mcp Chromium with
         `--remote-debugging-port=<N>` on the command line.
    """
    explicit = os.environ.get("CHROME_CDP_URL", "").strip()
    if explicit and _cdp_alive(explicit):
        return explicit

    port_env = os.environ.get("JOBSPY_CHROME_CDP_PORT", "").strip()
    if port_env.isdigit():
        candidate = f"http://127.0.0.1:{port_env}"
        if _cdp_alive(candidate):
            return candidate

    standard = "http://127.0.0.1:9222"
    if _cdp_alive(standard):
        return standard

    autodetected = _autodetect_running_pw_mcp_port()
    if autodetected:
        candidate = f"http://127.0.0.1:{autodetected}"
        if _cdp_alive(candidate):
            return candidate
    return None


def _cdp_alive(base_url: str, timeout: float = 1.5) -> bool:
    try:
        with urlopen(f"{base_url.rstrip('/')}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _autodetect_running_pw_mcp_port() -> int | None:
    """Scan running processes for a Chromium with --remote-debugging-port=N.

    macOS-only path right now (uses `ps -ax`). On Linux this still works.
    Windows would need a different approach (tasklist/wmic) — left as TODO
    if needed.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-axo", "command="], text=True, stderr=subprocess.DEVNULL, timeout=2
        )
    except Exception:
        return None
    for line in out.splitlines():
        if "chromium" not in line.lower() and "chrome" not in line.lower():
            continue
        if "playwright" not in line.lower() and "mcp" not in line.lower() and "ms-playwright" not in line.lower():
            continue
        m = re.search(r"--remote-debugging-port=(\d+)", line)
        if m:
            return int(m.group(1))
    return None


# -----------------------------------------------------------------------------
# Page-side extraction
# -----------------------------------------------------------------------------

# Each job is a card whose only stable anchor is a "[role="button"]" with
# aria-label "Add <title> to saves list". The card's *visible* content
# lives several levels up the parent chain — Google nests it ~8 deep.
# At parent-depth 2 you get just title/company/location (3 lines). Walk
# further up and you pick up "Posted X ago", "Full-time", "Salary $...",
# etc. Walk too far and multiple cards merge into one container.
#
# We pick the LARGEST ancestor that:
#   - contains the title from aria-label exactly once (still one card)
#   - AND mentions an age phrase (X days/hours ago) OR an employment-type
#     keyword (Full-time/Part-time/Contract/Internship) — i.e. has the
#     metadata block, not just the heading block
# If no such ancestor is found within 14 levels, fall back to the first
# multi-line container (heading-only — works but date_posted will be None).
_EXTRACT_JS = r"""
() => {
  const buttons = document.querySelectorAll('[role="button"]');
  const cards = [];
  const seen = new Set();
  const META_RE = /(\d+\s*(?:day|hour|week|month|minute)s?\s*ago)|(Full-time|Part-time|Contractor|Contract|Internship|Temporary)/i;

  for (const btn of buttons) {
    const aria = btn.getAttribute('aria-label') || '';
    const m = aria.match(/^Add (.+) to saves list$/);
    if (!m) continue;
    const title = m[1];
    const titleEsc = title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const titleRe = new RegExp(titleEsc.slice(0, Math.min(40, titleEsc.length)), 'g');

    let el = btn.parentElement;
    let metaContainer = null;       // FIRST ancestor with metadata block — one card
    let fallbackContainer = null;   // first multi-line ancestor (heading only, no date)

    for (let depth = 0; depth < 14 && el; depth++) {
      const txt = (el.innerText || '').trim();
      if (txt.length > 30 && txt.includes('\n')) {
        if (!fallbackContainer) fallbackContainer = el;
        // Stop at the first ancestor that contains a date OR employment-type tag
        // AND still has the title only once (i.e. hasn't merged with siblings yet).
        // Once we walk past this, sibling cards will glue on and we lose the
        // single-card boundary.
        if (META_RE.test(txt)) {
          const titleHits = (txt.match(titleRe) || []).length;
          if (titleHits === 1) {
            metaContainer = el;
            break;
          }
        }
      }
      el = el.parentElement;
    }

    const container = metaContainer || fallbackContainer;
    if (!container) continue;
    if (seen.has(container)) continue;
    seen.add(container);

    const lines = (container.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    let href = null;
    for (const a of container.querySelectorAll('a[href]')) {
      const h = a.href;
      if (h && !h.endsWith('#') && !h.includes('#/')) { href = h; break; }
    }
    cards.push({ title_from_aria: title, lines: lines, href: href });
  }
  return cards;
}
"""


# -----------------------------------------------------------------------------
# Card → JobPost
# -----------------------------------------------------------------------------

_AGE_RE = re.compile(r"(\d+)\s*(day|hour|week|month|minute)s?\s*ago", re.I)


def _build_job_post(card: dict[str, Any]) -> JobPost | None:
    title_from_aria: str = (card.get("title_from_aria") or "").strip()
    lines: list[str] = card.get("lines") or []
    href: str | None = card.get("href")

    if not lines:
        return None

    # Layout in practice (Google Jobs, May 2026):
    #   [0] title
    #   [1] company
    #   [2] location, often with " • via <syndicator>" suffix
    #   [3...] tags: "Posted X days ago", "Salary $...", "Employment Type ...",
    #          "Qualification ...", optional benefit chips
    title = lines[0].strip() if len(lines) > 0 else None
    if not title:
        title = title_from_aria
    if not title:
        return None

    company = lines[1].strip() if len(lines) > 1 else None
    raw_location = lines[2].strip() if len(lines) > 2 else None

    location_str = raw_location
    if location_str and "•" in location_str:
        location_str = location_str.split("•", 1)[0].strip()

    location_obj: Location | None = None
    if location_str:
        city = state = None
        if "," in location_str:
            parts = [p.strip() for p in location_str.split(",")]
            city = parts[0] or None
            state = parts[1] if len(parts) > 1 else None
        else:
            city = location_str
        location_obj = Location(city=city, state=state, country=Country.USA)

    date_posted = None
    for ln in lines:
        m = _AGE_RE.search(ln)
        if m:
            n = int(m.group(1))
            unit = m.group(2).lower()
            now = datetime.now()
            delta = {
                "minute": timedelta(minutes=n),
                "hour": timedelta(hours=n),
                "day": timedelta(days=n),
                "week": timedelta(weeks=n),
                "month": timedelta(days=30 * n),
            }.get(unit, timedelta(days=n))
            date_posted = (now - delta).date()
            break

    is_remote = (
        "remote" in (raw_location or "").lower()
        or "remote" in title.lower()
        or "wfh" in (raw_location or "").lower()
    )

    job_url = href
    if not job_url:
        q = " ".join(filter(None, [title, company])).strip()
        job_url = f"https://www.google.com/search?q={quote_plus(q + ' job')}&udm=8"

    id_seed = "|".join(filter(None, [title, company, raw_location]))
    job_id = "go-" + str(abs(hash(id_seed)))[:14]

    return JobPost(
        id=job_id,
        title=title,
        company_name=company,
        job_url=job_url,
        location=location_obj,
        date_posted=date_posted,
        is_remote=is_remote,
        description=None,
    )
