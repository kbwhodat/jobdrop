"""Dice scraper — tech/IT job board, SSR HTML via Googlebot UA.

Dice.com is a long-running US tech/IT specialty board. The public API
(`job-search-api.svc.dhigroupinc.com`) is locked behind WAF — vanilla
HTTPS requests return 403. However, Dice's Next.js frontend
server-side-renders the full search results page for crawlers, and
sending Googlebot's User-Agent over a Chrome TLS fingerprint
(curl_cffi `chrome120`) returns the same rendered HTML that Google
indexes.

URL contract:

    https://www.dice.com/jobs?q=<keywords>&location=<City, ST>
        &radius=<miles>&filters.workplaceTypes=<...>

The SSR'd HTML embeds each result card as a ``<div data-testid="job-card"
data-id="<hash>" data-job-guid="<uuid>">``. The card contains:
  - aria-label on the inner anchor → title
  - href → ``/job-detail/<uuid>`` (canonical URL)
  - company name in a <p> near the company-profile link
  - location, posted-relative, job_type, description preview, salary all
    as plain <p> tags in document order

No auth, no JS execution required — just the right UA + TLS profile.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup
from curl_cffi import requests as cc_requests

from jobdrop.model import (
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
from jobdrop.util import create_logger

log = create_logger("Dice")

_BASE = "https://www.dice.com"
_SEARCH = f"{_BASE}/jobs"
_TIMEOUT_S = 25
_PAGE_SIZE = 20  # Dice's SSR page returns ~20 cards
# Googlebot UA over chrome120 TLS — empirically the only combo that gets
# the SSR'd job list. Real browsers + non-Googlebot UAs also work but UAs
# without "Googlebot" hit a JS-only shell that has no job data inline.
_USER_AGENT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

_JOB_TYPE_MAP = {
    "full-time": JobType.FULL_TIME,
    "fulltime": JobType.FULL_TIME,
    "part-time": JobType.PART_TIME,
    "parttime": JobType.PART_TIME,
    "contract": JobType.CONTRACT,
    "contract-to-hire": JobType.CONTRACT,
    "third party": JobType.CONTRACT,
    "internship": JobType.INTERNSHIP,
    "temporary": JobType.TEMPORARY,
}

# "$150000 - $180000" or "$60 - $75" (hourly) or "$150K - $180K"
_SAL_RANGE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([kKmM])?\s*-\s*"
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([kKmM])?"
)
# "$150000" or "$150K" — single value
_SAL_SINGLE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([kKmM])?"
)
_TIME_RELATIVE_RE = re.compile(
    r"(\d+)\s*(minute|hour|day|week|month|year)s?\s*ago",
    re.IGNORECASE,
)


class Dice(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.DICE, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self._ua = user_agent or _USER_AGENT

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        wanted = scraper_input.results_wanted
        jobs: list[JobPost] = []
        seen_ids: set[str] = set()

        page = 1 + (scraper_input.offset // _PAGE_SIZE)
        while len(jobs) < wanted:
            params: dict[str, Any] = {
                "q": scraper_input.search_term or "",
                "location": scraper_input.location or "",
                "page": page,
                "pageSize": _PAGE_SIZE,
                "radius": scraper_input.distance or 30,
                "radiusUnit": "mi",
            }
            if scraper_input.is_remote:
                params["filters.workplaceTypes"] = "Remote"

            try:
                r = cc_requests.get(
                    _SEARCH,
                    params=params,
                    headers={
                        "User-Agent": self._ua,
                        "Accept": "text/html",
                    },
                    impersonate="chrome120",
                    timeout=_TIMEOUT_S,
                )
            except Exception as e:  # noqa: BLE001
                log.error(f"Dice: request failed page={page}: {e}")
                break
            if not r.ok:
                log.error(f"Dice: status {r.status_code} on page {page}")
                break

            page_jobs = _parse_search_page(r.text, scraper_input.country)
            if not page_jobs:
                break

            new_this_page = 0
            for post in page_jobs:
                if post.id in seen_ids:
                    continue
                seen_ids.add(post.id)
                jobs.append(post)
                new_this_page += 1
                if len(jobs) >= wanted:
                    break

            log.info(f"Dice: page {page} → {new_this_page} new (total {len(jobs)})")
            if new_this_page == 0:
                break
            if page > 20:
                break
            page += 1

        log.info(f"Dice: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


def _parse_search_page(html: str, country: Country | None) -> list[JobPost]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[JobPost] = []
    for card in soup.select('div[data-testid="job-card"]'):
        post = _parse_card(card, country)
        if post is not None:
            jobs.append(post)
    return jobs


def _parse_card(card, country: Country | None) -> JobPost | None:
    try:
        guid = card.get("data-job-guid")
        if not guid:
            return None

        link = card.select_one('a[data-testid="job-search-job-card-link"]')
        if link is None:
            return None
        title_raw = link.get("aria-label") or ""
        # Strip trailing " (hash)" — they put the data-id in parens
        title = re.sub(r"^View Details for\s+", "", title_raw)
        title = re.sub(r"\s*\([a-f0-9]+\)\s*$", "", title).strip()
        if not title:
            return None
        title = " ".join(title.split())
        job_url = link.get("href") or f"{_BASE}/job-detail/{guid}"

        # Company name lives in a <p> inside an <a> with /company-profile/
        company_p = card.select_one('a[href*="/company-profile/"] p')
        company = company_p.get_text(strip=True) if company_p else None

        # All other fields are inline <p> tags in document order. The exact
        # order varies card to card, so we classify each <p>'s text content
        # against pattern bags rather than positional indexing.
        location_text: str | None = None
        date_text: str | None = None
        description_text: str | None = None
        salary_text: str | None = None
        job_type_text: str | None = None
        for p in card.find_all("p"):
            text = " ".join(p.get_text(" ", strip=True).split())
            if not text or text == "•":
                continue
            if company and text == company:
                continue
            tl = text.lower()
            # Salary
            if salary_text is None and "$" in text and _SAL_SINGLE_RE.search(text):
                salary_text = text
                continue
            # Job type
            if job_type_text is None and tl in _JOB_TYPE_MAP:
                job_type_text = text
                continue
            # Date — explicit relative or "Today"/"Yesterday"
            if date_text is None and (
                tl == "today"
                or tl == "yesterday"
                or _TIME_RELATIVE_RE.search(tl)
            ):
                date_text = text
                continue
            # Location — text containing a state code or "Remote"/"Hybrid"
            if location_text is None and _looks_like_location(text):
                location_text = text
                continue
            # Description preview (longest remaining)
            if description_text is None and len(text) > 60:
                description_text = text
                continue
        location_obj = _build_location(location_text, country)
        compensation = _build_compensation(salary_text)
        job_types = _map_job_type(job_type_text)
        is_remote = bool(location_text and "remote" in location_text.lower())
        date_posted = _parse_relative_date(date_text)

        return JobPost(
            id=f"dice-{guid}",
            title=title,
            company_name=company,
            location=location_obj,
            description=description_text,
            date_posted=date_posted,
            job_url=job_url,
            compensation=compensation,
            job_type=job_types,
            is_remote=is_remote,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"Dice: skipping malformed card: {e}")
        return None


# Matches "Atlanta, GA", "Smyrna, Georgia", "Remote", "Hybrid Atlanta, GA"
_LOC_RE = re.compile(
    r"^(?:hybrid\s+)?[A-Za-z][A-Za-z\.\-\s]+,\s*[A-Za-z]{2,}$"
)


def _looks_like_location(text: str) -> bool:
    if not text:
        return False
    tl = text.lower()
    if tl in {"remote", "hybrid", "on-site", "onsite"}:
        return True
    if _LOC_RE.match(text):
        return True
    return False


def _build_location(text: str | None, country: Country | None) -> Location | None:
    if not text:
        return None
    cleaned = re.sub(r"^hybrid\s+", "", text, flags=re.IGNORECASE).strip()
    if cleaned.lower() == "remote":
        return Location(country=country or Country.USA)
    parts = [p.strip() for p in cleaned.split(",")]
    city = parts[0] if parts else None
    state = parts[1] if len(parts) > 1 else None
    if not city and not state:
        return None
    return Location(city=city, state=state, country=country or Country.USA)


def _map_job_type(text: str | None) -> list[JobType] | None:
    if not text:
        return None
    mapped = _JOB_TYPE_MAP.get(text.strip().lower().replace(" ", "-"))
    return [mapped] if mapped else None


def _build_compensation(text: str | None) -> Compensation | None:
    if not text:
        return None
    m = _SAL_RANGE_RE.search(text)
    if m:
        lo = _to_number(m.group(1), m.group(2))
        hi = _to_number(m.group(3), m.group(4))
    else:
        single = _SAL_SINGLE_RE.search(text)
        if not single:
            return None
        lo = hi = _to_number(single.group(1), single.group(2))
    if lo is None or hi is None:
        return None
    # Drop "$0 - $0" placeholder rows that some Dice cards emit
    if (lo == 0 and hi == 0) or (max(lo, hi) <= 0):
        return None
    # Hourly heuristic: any number under $1,000 → hourly (Dice contracts
    # quote "$60 - $75"); $1,000+ → yearly ("$150000 - $180000").
    interval = (
        CompensationInterval.HOURLY if max(lo, hi) < 1000
        else CompensationInterval.YEARLY
    )
    return Compensation(interval=interval, min_amount=lo, max_amount=hi, currency="USD")


def _to_number(s: str, suffix: str | None) -> float | None:
    try:
        n = float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None
    if suffix and suffix.lower() == "k":
        n *= 1_000
    elif suffix and suffix.lower() == "m":
        n *= 1_000_000
    return n


def _parse_relative_date(text: str | None) -> date | None:
    if not text:
        return None
    tl = text.strip().lower()
    today = datetime.utcnow().date()
    if tl == "today":
        return today
    if tl == "yesterday":
        return today - timedelta(days=1)
    m = _TIME_RELATIVE_RE.search(tl)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit.startswith("minute") or unit.startswith("hour"):
        return today
    if unit.startswith("day"):
        return today - timedelta(days=n)
    if unit.startswith("week"):
        return today - timedelta(weeks=n)
    if unit.startswith("month"):
        return today - timedelta(days=n * 30)
    if unit.startswith("year"):
        return today - timedelta(days=n * 365)
    return None
