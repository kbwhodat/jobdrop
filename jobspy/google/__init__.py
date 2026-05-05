"""Google Jobs scraper — headless via selenium-driverless.

## Why selenium-driverless

Google's 2026 anti-bot CAPTCHAs every fresh browser launch from
playwright (chromium/chrome/firefox), undetected-chromedriver, nodriver,
patchright — every standard automation framework. Headed and headless
both fail. The only browsers Google trusts are long-running instances
that have accumulated session history.

`selenium-driverless` works around this by talking to Chrome over CDP
without any of the Selenium/WebDriver fingerprint surface. Cold-start,
headless, fresh profile — verified against three different Google
queries returning real jobs without /sorry/ redirects.

## End-to-end behavior

  - Launches Chrome headless (`--headless=new`)
  - Navigates Google Jobs SERP (`udm=8`)
  - Extracts cards using DOM walk (find aria "Add ... to saves list"
    button, walk up to the ancestor containing the metadata block)
  - Returns a JobResponse with title/company/location/date_posted/url

## Date capture

Google only decorates job cards with "Posted X ago" tags when the
query includes a temporal phrase like "in the last week" / "in the
last month". The scraper appends one based on `hours_old` and falls
back to "in the last month" when no `hours_old` is set, so dates
populate by default.

The original HTTP scraper is preserved at `__init__.py.http-backup`.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote_plus

from jobspy.google.util import log
from jobspy.model import (
    Compensation,
    CompensationInterval,
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
_NAV_TIMEOUT_S = 30
_RENDER_SLEEP_S = 4.0


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
        self.user_agent = user_agent  # accepted for API compat; unused

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        query = self._build_query(scraper_input)
        url = _GOOGLE_JOBS_URL.format(query=quote_plus(query))
        log.info(f"google: query={query!r}")

        try:
            from selenium_driverless import webdriver  # noqa: F401
        except ImportError:
            log.error(
                "google: selenium-driverless is required. "
                "Install with: pip install selenium-driverless"
            )
            return JobResponse(jobs=[])

        try:
            raw_cards = asyncio.run(self._scrape_async(url))
        except RuntimeError as e:
            # Caller is already inside an event loop. Fall back to a
            # nested-loop runner via a fresh thread.
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                log.info("google: detected running loop; running on dedicated thread")
                raw_cards = _run_on_thread(self._scrape_async(url))
            else:
                raise

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

    async def _scrape_async(self, url: str) -> list[dict[str, Any]]:
        from selenium_driverless import webdriver

        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--window-size=1280,900")

        async with webdriver.Chrome(options=options) as driver:
            await driver.get(url, wait_load=True, timeout=_NAV_TIMEOUT_S)
            await asyncio.sleep(_RENDER_SLEEP_S)

            current_url = await driver.current_url
            if "/sorry/" in current_url:
                log.error(
                    f"google: hit /sorry/ CAPTCHA at {current_url[:120]}. "
                    f"selenium-driverless usually bypasses this — possible IP "
                    f"reputation issue or anti-bot upgrade."
                )
                return []

            cards = await driver.execute_script(_EXTRACT_JS, serialization="json")
            return cards or []

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

        # Always include a temporal phrase. Google only decorates cards
        # with "Posted X ago" lines when one is present in the query.
        # Default: last month — wide enough not to filter usefully but
        # enough to make dates appear on cards.
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
        else:
            parts.append("in the last month")

        if si.is_remote:
            parts.append("remote")

        return " ".join(parts)


# -----------------------------------------------------------------------------
# Async helpers
# -----------------------------------------------------------------------------


def _run_on_thread(coro):
    """Run an awaitable to completion from sync code that's already inside
    a running event loop. Spins up a dedicated thread + new loop for the
    one call. Returns the result; re-raises exceptions from the coro."""
    import threading

    result_box: dict[str, Any] = {}

    def runner():
        try:
            result_box["ok"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            result_box["err"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "err" in result_box:
        raise result_box["err"]
    return result_box.get("ok")


# -----------------------------------------------------------------------------
# Page-side extraction
# -----------------------------------------------------------------------------

# Each job card's only stable anchor is a `[role="button"]` with aria-label
# "Add <title> to saves list". Its visible content lives several DOM levels
# up — heading-only at depth ~2, full card (heading + "Posted X ago" +
# employment type) at depth ~8. Walk too far and sibling cards merge.
#
# Heuristic: walk up to the FIRST ancestor that
#   - has multi-line text content
#   - matches an age phrase or employment-type keyword (the metadata block)
#   - still contains the card's title exactly once (single-card boundary)
# Fall back to the first multi-line ancestor (heading-only — date_posted
# will be None) if no metadata-bearing ancestor exists within 14 levels.
_EXTRACT_JS = r"""
const buttons = document.querySelectorAll('[role="button"]');
const cards = [];
const seen = new Set();
const META_RE = /(\d+\s*(?:day|hour|week|month|minute)s?\s*ago)|(Full-time|Part-time|Contractor|Contract|Internship|Temporary)/i;

for (const btn of buttons) {
  const aria = btn.getAttribute('aria-label') || '';
  const m = aria.match(/^Add (.+) to saves list$/);
  if (!m) continue;
  const title = m[1];
  // Escape regex metacharacters in the title. Don't slice — slicing the
  // escaped string risks cutting mid-`\x` and leaving a dangling backslash,
  // which produces "Invalid regular expression" at runtime.
  const titleEsc = title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const titleRe = new RegExp(titleEsc, 'g');

  let el = btn.parentElement;
  let metaContainer = null;
  let fallbackContainer = null;

  for (let depth = 0; depth < 14 && el; depth++) {
    const txt = (el.innerText || '').trim();
    if (txt.length > 30 && txt.includes('\n')) {
      if (!fallbackContainer) fallbackContainer = el;
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
"""


# -----------------------------------------------------------------------------
# Card → JobPost
# -----------------------------------------------------------------------------

_AGE_RE = re.compile(r"(\d+)\s*(day|hour|week|month|minute)s?\s*ago", re.I)

# Google's card uses hyphenated forms ("Full-time", "Part-time") that don't
# match the upstream JobType.FULL_TIME alias list (which expects "fulltime").
# Explicit mapping from card text → JobType enum.
_JOB_TYPE_TEXT_MAP = {
    "full-time": JobType.FULL_TIME,
    "fulltime": JobType.FULL_TIME,
    "part-time": JobType.PART_TIME,
    "parttime": JobType.PART_TIME,
    "contract": JobType.CONTRACT,
    "contractor": JobType.CONTRACT,
    "internship": JobType.INTERNSHIP,
    "intern": JobType.INTERNSHIP,
    "temporary": JobType.TEMPORARY,
    "temp": JobType.TEMPORARY,
    "per diem": JobType.PER_DIEM,
}
_JOB_TYPE_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _JOB_TYPE_TEXT_MAP.keys()) + r")\b",
    re.I,
)


# Salary parsing — Google's card lines show forms like:
#   "$60K–$80K a year"            (k-suffix range, yearly)
#   "$20.00 - $34.00 Per Hour"    (decimal range, hourly)
#   "$100,920–$162,600 a year"    (comma-thousand range, yearly)
#   "$26.27–$30.07 an hour"       (decimal range, hourly)
#   "$45K a year"                 (single value, yearly)
# Optional leading "Salary " label, varying dash kinds (-, –, —, "to").
_SALARY_RANGE_RE = re.compile(
    r"""
    \$?\s*([\d,]+(?:\.\d+)?)\s*([KkMm])?    # min amount + optional multiplier
    \s*(?:[-–—]+|\sto\s)\s*                  # separator: any dash or " to "
    \$?\s*([\d,]+(?:\.\d+)?)\s*([KkMm])?    # max amount + optional multiplier
    \s*(?:a\s|an\s|per\s|/)?                # interval connector
    (year|yr|annual|hour|hr|month|mo|week|wk|day|daily)? # interval
    """,
    re.I | re.X,
)
_SALARY_SINGLE_RE = re.compile(
    r"""
    \$?\s*([\d,]+(?:\.\d+)?)\s*([KkMm])?
    \s*(?:a\s|an\s|per\s|/)
    (year|yr|annual|hour|hr|month|mo|week|wk|day|daily)
    """,
    re.I | re.X,
)
_INTERVAL_MAP = {
    "year": CompensationInterval.YEARLY,
    "yr": CompensationInterval.YEARLY,
    "annual": CompensationInterval.YEARLY,
    "hour": CompensationInterval.HOURLY,
    "hr": CompensationInterval.HOURLY,
    "month": CompensationInterval.MONTHLY,
    "mo": CompensationInterval.MONTHLY,
    "week": CompensationInterval.WEEKLY,
    "wk": CompensationInterval.WEEKLY,
    "day": CompensationInterval.DAILY,
    "daily": CompensationInterval.DAILY,
}


def _parse_amount(num_str: str, mult_char: str | None) -> float:
    val = float(num_str.replace(",", ""))
    if mult_char:
        c = mult_char.lower()
        if c == "k":
            val *= 1000.0
        elif c == "m":
            val *= 1_000_000.0
    return val


def _extract_compensation(lines: list[str]) -> Compensation | None:
    """Scan card lines for a salary range or single value, return Compensation
    or None. Tries range first; falls back to single value with explicit
    interval (e.g. '$50K a year').

    Guard against false matches on incidental numeric ranges (employee
    counts, version numbers, etc.) by requiring at least one strong
    salary marker: $ prefix, K/M multiplier, or an interval word.
    """
    for ln in lines:
        m = _SALARY_RANGE_RE.search(ln)
        if not m:
            continue
        has_dollar = "$" in ln
        has_mult = bool(m.group(2) or m.group(4))
        has_interval = bool(m.group(5))
        if not (has_dollar or has_mult or has_interval):
            continue
        min_amt = _parse_amount(m.group(1), m.group(2))
        max_amt = _parse_amount(m.group(3), m.group(4))
        interval_word = (m.group(5) or "").lower()
        interval = _INTERVAL_MAP.get(interval_word)
        return Compensation(
            interval=interval,
            min_amount=min_amt,
            max_amount=max_amt,
            currency="USD",
        )
    for ln in lines:
        m = _SALARY_SINGLE_RE.search(ln)
        if not m:
            continue
        # Single-RE requires explicit interval, so it's harder to false-match.
        amt = _parse_amount(m.group(1), m.group(2))
        interval = _INTERVAL_MAP.get(m.group(3).lower())
        return Compensation(
            interval=interval,
            min_amount=amt,
            max_amount=amt,
            currency="USD",
        )
    return None


def _build_job_post(card: dict[str, Any]) -> JobPost | None:
    title_from_aria: str = (card.get("title_from_aria") or "").strip()
    lines: list[str] = card.get("lines") or []
    href: str | None = card.get("href")

    if not lines:
        return None

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

    job_types: list[JobType] = []
    seen_types: set[JobType] = set()
    for ln in lines:
        for m in _JOB_TYPE_RE.finditer(ln):
            t = _JOB_TYPE_TEXT_MAP[m.group(1).lower()]
            if t not in seen_types:
                seen_types.add(t)
                job_types.append(t)

    compensation = _extract_compensation(lines)

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
        job_type=job_types or None,
        compensation=compensation,
        description=None,
    )
