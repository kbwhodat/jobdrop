"""Naukri scraper — HTML search page via zendriver.

Naukri's JSON API (``naukri.com/jobapi/v3/search``) now returns
``HTTP 406 — recaptcha required`` to non-browser clients, even with
TLS impersonation (``curl_cffi`` + Chrome). The hardcoded ``Nkparam``
token in the legacy headers no longer satisfies the challenge.

The fix: drive the public HTML search page with zendriver (CDP-direct,
no WebDriver fingerprint). The same approach used for greenhouse and
google. Naukri renders 20 job cards per page server-side, with stable
class hooks (``.srp-jobtuple-wrapper``, ``data-job-id``) that survive
the React/Next hydration.

URL patterns:
  https://www.naukri.com/{kw-slug}-jobs                       (page 1)
  https://www.naukri.com/{kw-slug}-jobs-{N}                   (page N)
  https://www.naukri.com/{kw-slug}-jobs-in-{loc-slug}         (page 1 + city)
  https://www.naukri.com/{kw-slug}-jobs-in-{loc-slug}-{N}     (page N + city)

DOM extraction returns a JSON-safe array of dicts with title,
company, location, experience, salary text, date_posted text, and
job_url. Salary is parsed from Indian formats (Lacs P.A., Cr) into
INR ranges. Experience and date_posted strings are normalized to
the standard JobPost fields.
"""
from __future__ import annotations

import asyncio
import re as builtin_re
import threading
from datetime import date, datetime, timedelta
from typing import Any, Optional

import regex as re

from jobdrop.naukri.util import is_job_remote
from jobdrop.model import (
    Compensation,
    Country,
    JobPost,
    JobResponse,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobdrop.util import create_logger

log = create_logger("Naukri")

_BASE = "https://www.naukri.com"
_JOBS_PER_PAGE = 20
_NAV_TIMEOUT_S = 30
_RENDER_SLEEP_S = 6.0


# Page-side card extractor. Naukri renders job cards with a few
# different class hooks across A/B variants — we query the union and
# dedupe by data-job-id.
_EXTRACT_JS = r"""
const cards = document.querySelectorAll(
  '.srp-jobtuple-wrapper, .cust-job-tuple, [data-job-id]'
);
const seen = new Set();
const out = [];
for (const c of cards) {
  const jid = c.getAttribute('data-job-id');
  if (!jid || seen.has(jid)) continue;
  seen.add(jid);

  const titleEl = c.querySelector('.title, a.title, h2');
  const compEl = c.querySelector('.comp-name, .companyName, [class*="comp-name"]');
  const locEl = c.querySelector('[class*="loc"], .location, span.locWdth');
  const expEl = c.querySelector('[class*="exp"]');
  const salEl = c.querySelector('[class*="sal"]');
  const dateEl = c.querySelector('[class*="job-post-day"], .job-post-day');
  const linkEl = c.querySelector('a[href*="/job-listings"]');
  const skillsEls = c.querySelectorAll('.tags-gt li, .tag-li');
  const skills = [];
  for (const s of skillsEls) {
    const t = (s.innerText || '').trim();
    if (t) skills.push(t);
  }

  out.push({
    job_id: jid,
    title: titleEl ? titleEl.innerText.trim() : null,
    company: compEl ? compEl.innerText.trim() : null,
    location: locEl ? locEl.innerText.trim() : null,
    experience: expEl ? expEl.innerText.trim() : null,
    salary: salEl ? salEl.innerText.trim() : null,
    date_posted: dateEl ? dateEl.innerText.trim() : null,
    job_url: linkEl ? linkEl.href : null,
    skills: skills,
  });
}
return out;
"""


class Naukri(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.NAUKRI, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        # user_agent is accepted for compatibility but intentionally
        # not forwarded to zendriver — overriding the UA contradicts
        # zendriver's natural Linux Chrome fingerprint and trips
        # bot detection on multiple sites.
        self._user_agent = user_agent
        log.info("Naukri scraper initialized (zendriver mode)")

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted
        start_offset = max(scraper_input.offset or 0, 0)
        start_page = (start_offset // _JOBS_PER_PAGE) + 1

        try:
            import zendriver as zd  # noqa: F401
        except ImportError:
            log.error(
                "Naukri: zendriver is required. "
                "Install with: pip install zendriver"
            )
            return JobResponse(jobs=[])

        try:
            cards = _run_async(
                _scrape_pages(
                    search_term=scraper_input.search_term or "",
                    location=scraper_input.location,
                    wanted=wanted,
                    start_page=start_page,
                )
            )
        except RuntimeError as e:
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                cards = _run_on_thread(
                    _scrape_pages(
                        search_term=scraper_input.search_term or "",
                        location=scraper_input.location,
                        wanted=wanted,
                        start_page=start_page,
                    )
                )
            else:
                raise

        log.info(f"Naukri: extracted {len(cards)} raw cards")

        jobs: list[JobPost] = []
        for card in cards:
            post = _build_job_post(card)
            if post is not None:
                jobs.append(post)
            if len(jobs) >= wanted:
                break

        log.info(f"Naukri: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


# -----------------------------------------------------------------------------
# Async fetch helpers
# -----------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Naukri-style URL slug: lowercase, spaces → hyphens, strip non-word."""
    s = (text or "").lower().strip()
    s = builtin_re.sub(r"[^\w\s-]", "", s)
    s = builtin_re.sub(r"\s+", "-", s)
    s = builtin_re.sub(r"-+", "-", s)
    return s.strip("-")


def _build_search_url(search_term: str, location: str | None, page: int) -> str:
    kw_slug = _slugify(search_term) or "jobs"
    base_path = f"{kw_slug}-jobs"
    if location:
        # Take only the city portion before any comma — naukri's location
        # slugs are city-only, "Bangalore, India" → "bangalore".
        city = location.split(",")[0].strip()
        loc_slug = _slugify(city)
        if loc_slug:
            base_path = f"{kw_slug}-jobs-in-{loc_slug}"
    if page > 1:
        base_path = f"{base_path}-{page}"
    return f"{_BASE}/{base_path}"


async def _scrape_pages(
    search_term: str, location: str | None, wanted: int, start_page: int,
) -> list[dict[str, Any]]:
    """Walk naukri SERPs until we have ``wanted`` cards or pages run dry."""
    import zendriver as zd

    browser = await zd.start(
        headless=True, sandbox=False, browser_args=["--window-size=1280,900"],
    )
    seen_ids: set[str] = set()
    all_cards: list[dict[str, Any]] = []
    try:
        # Cap pagination at 5 pages (~100 cards) regardless of `wanted`
        # to bound runtime; callers can use offset for deeper paging.
        for offset in range(5):
            page_num = start_page + offset
            url = _build_search_url(search_term, location, page_num)
            log.info(f"Naukri: page {page_num} → {url}")
            try:
                tab = await browser.get(url)
            except Exception as e:
                log.error(f"Naukri: page {page_num} fetch failed: {e}")
                break
            await asyncio.sleep(_RENDER_SLEEP_S)

            try:
                cards = await tab.evaluate(
                    f"(() => {{ {_EXTRACT_JS} }})()", return_by_value=True,
                )
            except Exception as e:
                log.error(f"Naukri: page {page_num} extraction failed: {e}")
                break

            cards = cards or []
            new_count = 0
            for c in cards:
                jid = c.get("job_id")
                if not jid or jid in seen_ids:
                    continue
                seen_ids.add(jid)
                all_cards.append(c)
                new_count += 1

            log.info(
                f"Naukri: page {page_num} added {new_count} cards "
                f"(total {len(all_cards)} / wanted {wanted})"
            )
            if len(all_cards) >= wanted:
                break
            if new_count == 0:
                break
    finally:
        await browser.stop()

    return all_cards


def _run_async(coro):
    return asyncio.run(coro)


def _run_on_thread(coro):
    """Run an awaitable from sync code already inside a running loop."""
    box: dict[str, Any] = {}

    def runner():
        try:
            box["ok"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "err" in box:
        raise box["err"]
    return box.get("ok")


# -----------------------------------------------------------------------------
# Card → JobPost
# -----------------------------------------------------------------------------


_SALARY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(Lacs?|Lakh|Cr)\s*(P\.A\.?|PA)?",
    re.I,
)


def _parse_compensation(salary_text: str | None) -> Optional[Compensation]:
    """Parse Indian salary strings (e.g. '5-14 Lacs PA', '1-5 Cr')."""
    if not salary_text:
        return None
    text = salary_text.strip()
    if text.lower().startswith("not disclosed"):
        return None

    m = _SALARY_RE.search(text)
    if not m:
        return None
    min_v, max_v, unit = m.group(1), m.group(2), m.group(3)
    try:
        min_amt = float(min_v)
        max_amt = float(max_v)
    except ValueError:
        return None
    unit_l = unit.lower()
    if unit_l in ("lacs", "lac", "lakh"):
        min_amt *= 100_000
        max_amt *= 100_000
    elif unit_l == "cr":
        min_amt *= 10_000_000
        max_amt *= 10_000_000

    return Compensation(
        min_amount=int(min_amt), max_amount=int(max_amt), currency="INR",
    )


_DAYS_AGO_RE = re.compile(r"(\d+)\s*\+?\s*day", re.I)


def _parse_date(label: str | None) -> Optional[date]:
    if not label:
        return None
    s = label.strip().lower()
    today = datetime.now()
    if "today" in s or "just now" in s or "few hours" in s or "hour" in s:
        return today.date()
    if "yesterday" in s:
        return (today - timedelta(days=1)).date()
    m = _DAYS_AGO_RE.search(s)
    if m:
        try:
            days = int(m.group(1))
            return (today - timedelta(days=days)).date()
        except ValueError:
            return None
    return None


def _parse_location(loc_text: str | None) -> Location:
    """Naukri location strings: 'Bengaluru', 'Pune, Mumbai (All Areas)',
    'Hybrid - Bangalore', 'Remote'. Take the first comma-separated city."""
    if not loc_text:
        return Location(country=Country.INDIA)
    s = loc_text.strip()
    # Strip leading "Hybrid - " or similar prefixes
    s = builtin_re.sub(r"^(hybrid|remote|wfh|work from home)\s*-\s*", "", s, flags=builtin_re.I)
    parts = [p.strip() for p in s.split(",")]
    city = parts[0] if parts else None
    state = parts[1] if len(parts) > 1 else None
    # Strip parenthetical suffixes like "(All Areas)"
    if city:
        city = builtin_re.sub(r"\s*\([^)]*\)\s*$", "", city).strip() or None
    return Location(city=city, state=state, country=Country.INDIA)


def _build_job_post(card: dict[str, Any]) -> JobPost | None:
    job_id = card.get("job_id")
    title = (card.get("title") or "").strip()
    if not job_id or not title:
        return None

    company = (card.get("company") or "").strip() or None
    raw_location = card.get("location")
    location_obj = _parse_location(raw_location)
    compensation = _parse_compensation(card.get("salary"))
    date_posted = _parse_date(card.get("date_posted"))
    job_url = card.get("job_url") or f"{_BASE}/job-listings-{job_id}"

    skills = [s for s in (card.get("skills") or []) if s] or None
    experience_range = card.get("experience") or None

    is_remote = is_job_remote(title, "", location_obj) or (
        bool(raw_location) and "remote" in raw_location.lower()
    )

    return JobPost(
        id=f"nk-{job_id}",
        title=title,
        company_name=company,
        location=location_obj,
        is_remote=is_remote,
        date_posted=date_posted,
        job_url=job_url,
        compensation=compensation,
        skills=skills,
        experience_range=experience_range,
    )
