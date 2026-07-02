"""Handshake scraper — early-talent / student-grad jobs (public listings only).

Handshake is the dominant US student-and-recent-grad job platform. The
in-app feed (employer-vetted, university-gated) is auth-walled, but
Handshake publishes a public *city* listing page for every major US
metro and a public *role* listing for every job-role taxonomy. Each
listing surfaces 15-30 jobs as ``/public/jobs/<id>`` URLs and the
detail pages are server-rendered with a complete Schema.org
``JobPosting`` JSON-LD block.

URL contract:

  https://joinhandshake.com/find-jobs/<city>-<state>     (city listings)
  https://joinhandshake.com/find-jobs/role/<role>       (role listings)
  https://app.joinhandshake.com/public/jobs/<id>        (detail page)

City slug format matches "<city>-<state>" lowercased, hyphenated. We
normalize ``Atlanta, GA`` → ``atlanta-ga``. When no location is
provided we fall back to scraping multiple top-metro pages in parallel.

## Caveats

  - Public surface is a small slice of total Handshake postings — most
    listings require a verified student account at a partner school.
  - City pages return ~15-30 jobs; pagination isn't part of the public
    surface, so volume is naturally capped.
  - Keyword filter happens after detail-page fetch (city pages don't
    accept ``?q=`` params).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any

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

log = create_logger("Handshake")

_BASE = "https://joinhandshake.com"
_CITY_URL_TMPL = f"{_BASE}/find-jobs/{{slug}}"
_DETAIL_URL_TMPL = "https://app.joinhandshake.com/public/jobs/{job_id}"
_TIMEOUT_S = 20
_DETAIL_WORKERS = 6
_DEFAULT_MAX_AGE_DAYS = 90

# Top metros to scan when no location is provided. Matches Handshake's
# own SEO landing pages.
_FALLBACK_METROS = [
    "new-york-ny", "san-francisco-ca", "los-angeles-ca", "chicago-il",
    "boston-ma", "atlanta-ga", "seattle-wa", "austin-tx", "washington-dc",
    "philadelphia-pa", "denver-co", "houston-tx", "dallas-tx", "miami-fl",
]

_CARD_RE = re.compile(
    r'<a\s+href="(https://app\.joinhandshake\.com/public/jobs/(\d+))"'
    r'[^>]*aria-label="Apply to ([^"]+)"',
    re.DOTALL,
)
_LDJSON_RE = re.compile(
    r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": JobType.FULL_TIME,
    "PART_TIME": JobType.PART_TIME,
    "CONTRACTOR": JobType.CONTRACT,
    "CONTRACT": JobType.CONTRACT,
    "TEMPORARY": JobType.TEMPORARY,
    "INTERN": JobType.INTERNSHIP,
    "INTERNSHIP": JobType.INTERNSHIP,
}


class Handshake(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.HANDSHAKE, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted or 15
        start_offset = max(scraper_input.offset or 0, 0)

        sess = cc_requests.Session(impersonate="chrome120")
        slugs = _location_slugs(scraper_input.location)
        if not slugs:
            slugs = _FALLBACK_METROS
            log.info(f"Handshake: no location, scanning {len(slugs)} top metros")

        candidate_urls = _collect_card_urls(sess, slugs)
        if not candidate_urls:
            log.info("Handshake: 0 candidate jobs from city listings")
            return JobResponse(jobs=[])

        log.info(f"Handshake: {len(candidate_urls)} candidate jobs from city pages")
        # Cap detail-page fetches
        fetch_target = min(
            len(candidate_urls), max(wanted * 3, wanted + 20)
        )
        url_subset = list(candidate_urls)[:fetch_target]

        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=_DETAIL_WORKERS) as ex:
            futures = {ex.submit(_fetch_detail, sess, u): u for u in url_subset}
            for fut in as_completed(futures):
                url = futures[fut]
                data = fut.result()
                if data:
                    results[url] = data
        log.info(f"Handshake: enriched {len(results)}/{fetch_target} detail pages")

        cutoff = _resolve_cutoff(scraper_input)
        now = datetime.now(timezone.utc)
        kw_lower = (scraper_input.search_term or "").lower().strip()

        seen_ids: set[str] = set()
        jobs: list[JobPost] = []
        for url in url_subset:
            if url not in results:
                continue
            data = results[url]
            valid_through = _parse_iso_dt(data.get("validThrough"))
            if valid_through is not None and valid_through < now:
                continue
            posted = _parse_iso_dt(data.get("datePosted"))
            if cutoff and posted and posted < cutoff:
                continue

            title = (data.get("title") or "").strip()
            # Handshake JSON-LD often has " | Company | Handshake" suffix in
            # title — strip everything past the first " | ".
            title_clean = title.split(" | ")[0].strip() if title else title
            if kw_lower and kw_lower not in title_clean.lower():
                continue

            loc = _build_location(data.get("jobLocation"))
            post = _build_jobpost(url, data, title_clean, posted, loc)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)

        jobs = jobs[start_offset : start_offset + wanted]
        log.info(f"Handshake: returning {len(jobs)} jobs (offset={start_offset})")
        return JobResponse(jobs=jobs)


def _location_slugs(location: str | None) -> list[str]:
    if not location:
        return []
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if not parts:
        return []
    city = re.sub(r"[^a-z0-9\s]", "", parts[0].lower())
    city = re.sub(r"\s+", "-", city).strip("-")
    if len(parts) > 1:
        state = re.sub(r"[^a-z0-9]", "", parts[1].lower())
        if state:
            return [f"{city}-{state}"]
    return [city]


def _collect_card_urls(
    sess: cc_requests.Session, slugs: list[str]
) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for slug in slugs:
        url = _CITY_URL_TMPL.format(slug=slug)
        try:
            r = sess.get(url, timeout=_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            log.debug(f"Handshake: city {slug} fetch failed: {e!r}")
            continue
        if not r.ok:
            log.debug(f"Handshake: city {slug} returned {r.status_code}")
            continue
        for href, _jid, _label in _CARD_RE.findall(r.text):
            if href not in seen:
                seen.add(href)
                ordered.append(href)
    return ordered


def _fetch_detail(sess: cc_requests.Session, url: str) -> dict | None:
    try:
        r = sess.get(url, timeout=_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        log.debug(f"Handshake: detail {url} failed: {e!r}")
        return None
    if not r.ok:
        return None
    for blob in _LDJSON_RE.findall(r.text):
        try:
            data = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
    return None


def _resolve_cutoff(si: ScraperInput) -> datetime | None:
    hours = getattr(si, "hours_old", None)
    if hours and hours > 0:
        return datetime.now(timezone.utc) - timedelta(hours=hours)
    return datetime.now(timezone.utc) - timedelta(days=_DEFAULT_MAX_AGE_DAYS)


def _parse_iso_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        iso = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _build_location(value: Any) -> Location | None:
    if not value:
        return None
    locs = value if isinstance(value, list) else [value]
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        addr = loc.get("address") or {}
        if not isinstance(addr, dict):
            continue
        city = (addr.get("addressLocality") or "").strip() or None
        state = (addr.get("addressRegion") or "").strip() or None
        country_raw = addr.get("addressCountry") or None
        country: str | Country | None
        if isinstance(country_raw, dict):
            country = country_raw.get("name") or None
        elif isinstance(country_raw, str):
            country = country_raw.strip() or None
        else:
            country = None
        if country in ("US", "USA", "United States"):
            country = Country.USA
        if city or state or country:
            return Location(city=city, state=state, country=country)
    return None


def _build_compensation(raw: Any) -> Compensation | None:
    if not isinstance(raw, dict):
        return None
    val = raw.get("value")
    if not isinstance(val, dict):
        return None
    min_a = val.get("minValue")
    max_a = val.get("maxValue")
    unit = (val.get("unitText") or "YEAR").upper()
    interval_map = {
        "YEAR": CompensationInterval.YEARLY,
        "MONTH": CompensationInterval.MONTHLY,
        "WEEK": CompensationInterval.WEEKLY,
        "DAY": CompensationInterval.DAILY,
        "HOUR": CompensationInterval.HOURLY,
    }
    interval = interval_map.get(unit, CompensationInterval.YEARLY)
    if min_a is None and max_a is None:
        return None
    try:
        return Compensation(
            interval=interval,
            min_amount=float(min_a) if min_a is not None else None,
            max_amount=float(max_a) if max_a is not None else None,
            currency=raw.get("currency") or "USD",
        )
    except (ValueError, TypeError):
        return None


def _build_jobpost(
    url: str,
    data: dict,
    title_clean: str,
    posted: datetime | None,
    location: Location | None,
) -> JobPost | None:
    if not title_clean:
        return None
    m = re.search(r"/public/jobs/(\d+)", url)
    pid = m.group(1) if m else url.rsplit("/", 1)[-1]

    org = data.get("hiringOrganization") or {}
    company = ((org.get("name") if isinstance(org, dict) else None) or "").strip() or None
    company_logo = org.get("logo") if isinstance(org, dict) else None

    employment = data.get("employmentType")
    job_type = None
    if isinstance(employment, str):
        job_type = _EMPLOYMENT_TYPE_MAP.get(employment.upper())
    elif isinstance(employment, list) and employment:
        for e in employment:
            if isinstance(e, str):
                job_type = _EMPLOYMENT_TYPE_MAP.get(e.upper())
                if job_type:
                    break

    compensation = _build_compensation(data.get("baseSalary"))
    industry = data.get("industry")
    if isinstance(industry, list) and industry:
        industry = industry[0]
    if not isinstance(industry, str):
        industry = None

    description_html = data.get("description") or ""
    description = re.sub(r"<[^>]+>", " ", description_html)
    description = re.sub(r"\s+", " ", description).strip() or None

    return JobPost(
        id=f"handshake-{pid}",
        title=title_clean,
        company_name=company,
        job_url=url,
        location=location,
        date_posted=posted.date() if posted else None,
        description=description,
        compensation=compensation,
        job_type=[job_type] if job_type else None,
        company_industry=industry,
        company_logo=company_logo,
    )
