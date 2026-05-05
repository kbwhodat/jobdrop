"""ZipRecruiter scraper — curl_cffi + safari17_2_ios on the web endpoint.

## Why this is rewritten

The original implementation talked to ZipRecruiter's iOS-app API at
`api.ziprecruiter.com/jobs-app/jobs` with a hardcoded Basic-auth UUID
and iOS UA. As of 2026-05, **that endpoint is dead** for any external
caller — Cloudflare returns 403 `forbidden cf-waf` / `forbidden aa`
regardless of TLS impersonation profile (tested chrome, safari17_0,
safari17_2_ios, safari18_0, chrome131; all 403). The hardcoded
client-id UUID is also almost certainly invalidated upstream.

What still works: the public web search page at
`www.ziprecruiter.com/jobs-search`. **Counterintuitively**, only the
iOS Safari TLS profile passes Cloudflare cleanly — Chrome impersonation
trips a "Just a moment" interstitial. iOS Safari sails right through.

So this rewrite:
  - Uses curl_cffi with `impersonate="safari17_2_ios"`.
  - Hits the public web endpoint, not the API.
  - Parses HTML cards (`article[id^="job-card-"]`) for title / company /
    location / salary; constructs a job URL via the legacy
    `/jobs//j?lvk=<key>` redirector.
  - Dedupes on listing key (cards appear twice in the rendered DOM —
    likely mobile + desktop variants).

## Limitations vs. the original

  - **No absolute date_posted.** Cards only show a "New" badge for
    fresh jobs; no "X days ago" text. We set `date_posted = today` when
    the badge is present, else None.
  - **No description.** Would require a detail-page fetch per job.
    `description` is None in v1.
  - **No job_type / employment_type.** Not surfaced on the web cards.

The original API-based implementation is preserved at
`__init__.py.api-backup` for reference.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from jobspy.model import (
    Compensation,
    CompensationInterval,
    Country,
    JobPost,
    JobResponse,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.util import create_logger

log = create_logger("ZipRecruiter")

_SEARCH_URL = "https://www.ziprecruiter.com/jobs-search"
_BASE_URL = "https://www.ziprecruiter.com"
_IMPERSONATE_PROFILE = "safari17_2_ios"
_TIMEOUT_S = 25
_JOBS_PER_PAGE = 40  # observed page size from www.ziprecruiter.com
_MAX_RETRIES = 3
_RETRY_BACKOFF_S = (1.5, 3.5)  # increasing sleep between retries


class ZipRecruiter(Scraper):
    base_url = _BASE_URL

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.ZIP_RECRUITER, proxies=proxies)
        self.scraper_input: ScraperInput | None = None
        self.seen_keys: set[str] = set()

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        self.seen_keys = set()

        try:
            from curl_cffi import requests as cffi_requests  # noqa: F401
        except ImportError:
            log.error(
                "ZipRecruiter: curl_cffi is required. "
                "Install with: pip install curl_cffi"
            )
            return JobResponse(jobs=[])

        wanted = scraper_input.results_wanted
        max_pages = max(1, math.ceil(wanted / _JOBS_PER_PAGE))
        all_posts: list[JobPost] = []

        for page in range(1, max_pages + 1):
            log.info(f"ZipRecruiter: page {page}/{max_pages}")
            try:
                posts = self._fetch_page(page)
            except Exception as e:
                log.error(f"ZipRecruiter: page {page} fetch failed: {e}")
                break
            if not posts:
                break
            all_posts.extend(posts)
            if len(all_posts) >= wanted:
                break

        return JobResponse(jobs=all_posts[:wanted])

    def _fetch_page(self, page_num: int) -> list[JobPost]:
        import time as _time

        from curl_cffi import requests as cffi_requests

        params = self._build_params(page_num)
        url = f"{_SEARCH_URL}?{urlencode(params)}"

        last_status: int | None = None
        for attempt in range(_MAX_RETRIES):
            r = cffi_requests.get(url, impersonate=_IMPERSONATE_PROFILE, timeout=_TIMEOUT_S)
            last_status = r.status_code
            if r.status_code == 200:
                return self._parse_html(r.text)
            # ZipRecruiter occasionally throws transient 403s on bursts;
            # the same query usually succeeds within 1-2 retries.
            if r.status_code in (403, 429, 503) and attempt < _MAX_RETRIES - 1:
                sleep_s = _RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)]
                log.info(
                    f"ZipRecruiter: status {r.status_code} on attempt {attempt + 1}; "
                    f"retrying in {sleep_s}s"
                )
                _time.sleep(sleep_s)
                continue
            break
        log.error(f"ZipRecruiter: status {last_status} (after retries) for {url[:120]}")
        return []

    def _build_params(self, page_num: int) -> dict[str, Any]:
        si = self.scraper_input
        params: dict[str, Any] = {
            "search": si.search_term or "",
            "location": si.location or "",
        }
        if page_num > 1:
            params["page"] = page_num
        if si.is_remote:
            params["refine_by_location_type"] = "only_remote"
        if si.distance:
            params["radius"] = si.distance
        # Map ZipRecruiter's hours_old → days filter via "days" param
        if getattr(si, "hours_old", None):
            params["days"] = max(si.hours_old // 24, 1)
        return params

    def _parse_html(self, html: str) -> list[JobPost]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select('article[id^="job-card-"]')

        out: list[JobPost] = []
        for card in cards:
            post = self._card_to_jobpost(card)
            if post is None:
                continue
            if post.id in self.seen_keys:
                continue
            self.seen_keys.add(post.id)
            out.append(post)
        return out

    def _card_to_jobpost(self, card) -> JobPost | None:
        cid = card.get("id", "")
        m = re.match(r"^job-card-(.+)$", cid)
        if not m:
            return None
        listing_key = m.group(1)
        job_id = f"zr-{listing_key}"

        title_el = card.select_one("h2")
        title = title_el.get_text(" ", strip=True) if title_el else None
        if not title:
            return None

        company_el = card.select_one('[data-testid="job-card-company"]')
        company = company_el.get_text(" ", strip=True) if company_el else None

        location_el = card.select_one('[data-testid="job-card-location"]')
        location_str = location_el.get_text(" ", strip=True) if location_el else None
        location_obj = _parse_location(location_str)

        compensation = _extract_card_salary(card)

        # "New" badge → posted within ~24h. Cards don't expose explicit
        # "X days ago" text on the search-results layout.
        is_new = bool(card.find(string=lambda s: s and s.strip() == "New"))
        date_posted = datetime.now().date() if is_new else None

        job_url = f"{_BASE_URL}/jobs//j?lvk={listing_key}"

        return JobPost(
            id=job_id,
            title=title,
            company_name=company,
            job_url=job_url,
            location=location_obj,
            compensation=compensation,
            date_posted=date_posted,
            description=None,
        )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _parse_location(loc_str: str | None) -> Location | None:
    if not loc_str:
        return None
    if loc_str.lower() in {"remote", "remote, us"}:
        return Location(country=Country.USA)
    parts = [p.strip() for p in loc_str.split(",")]
    city = parts[0] if parts else None
    state = parts[1] if len(parts) > 1 else None
    return Location(city=city, state=state, country=Country.USA)


# Salary patterns observed on ZipRecruiter cards:
#   "$20 - $30/hr"
#   "$77.60K - $176K/yr"
#   "$45,000 - $60,000/yr"
#   "$50K/yr"  (single value)
_SALARY_RANGE_RE = re.compile(
    r"\$\s*([\d,.]+)\s*([KkMm]?)\s*-\s*\$\s*([\d,.]+)\s*([KkMm]?)\s*/\s*(yr|hr|mo|wk|day)",
    re.I,
)
_SALARY_SINGLE_RE = re.compile(
    r"\$\s*([\d,.]+)\s*([KkMm]?)\s*/\s*(yr|hr|mo|wk|day)",
    re.I,
)
_INTERVAL_MAP = {
    "yr": CompensationInterval.YEARLY,
    "hr": CompensationInterval.HOURLY,
    "mo": CompensationInterval.MONTHLY,
    "wk": CompensationInterval.WEEKLY,
    "day": CompensationInterval.DAILY,
}


def _amount(num_str: str, mult: str | None) -> float:
    val = float(num_str.replace(",", ""))
    if mult and mult.lower() == "k":
        val *= 1000.0
    elif mult and mult.lower() == "m":
        val *= 1_000_000.0
    return val


def _extract_card_salary(card) -> Compensation | None:
    text = card.get_text(" ", strip=True)
    m = _SALARY_RANGE_RE.search(text)
    if m:
        return Compensation(
            interval=_INTERVAL_MAP.get(m.group(5).lower()),
            min_amount=_amount(m.group(1), m.group(2)),
            max_amount=_amount(m.group(3), m.group(4)),
            currency="USD",
        )
    m = _SALARY_SINGLE_RE.search(text)
    if m:
        amt = _amount(m.group(1), m.group(2))
        return Compensation(
            interval=_INTERVAL_MAP.get(m.group(3).lower()),
            min_amount=amt,
            max_amount=amt,
            currency="USD",
        )
    return None
