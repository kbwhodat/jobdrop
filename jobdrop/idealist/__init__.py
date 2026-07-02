"""Idealist scraper — nonprofit / mission-driven job board.

Idealist is the largest US listing source for nonprofit, NGO, foundation,
advocacy, and mission-driven roles. Unique coverage versus the
mainstream aggregators — most Idealist postings never reach LinkedIn /
Indeed because nonprofit employers tend to post once on Idealist and not
elsewhere.

## How this works (no auth, no JS, no Google scraping required)

Idealist publishes a public sitemap of every currently-live job
posting at:

    https://www.idealist.org/sitemap-jobs-en-1.xml

This sitemap is XML, returns ~2,500-3,000 live URLs in a single fetch,
and is updated nightly. Each URL is of the form:

    https://www.idealist.org/en/<category>-job/<32-hex-hash>-<slug>

where ``<slug>`` ends with a kebab-cased city name. We pre-filter by
city slug before fetching detail pages — huge cost savings.

Each detail page embeds a clean Schema.org ``JobPosting`` JSON-LD block
with title, hiringOrganization, jobLocation, baseSalary, datePosted,
validThrough, employmentType, and description (HTML).

## Caveats

  - Sitemap reflects "currently live" only — no historical listings.
  - Location filter operates on URL slug. Hyphenation differences
    (St. Louis → st-louis) handled. Searches without a location pass
    every URL through the keyword filter.
  - Salary is JSON-LD-structured but only present on ~30-40% of postings.
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

log = create_logger("Idealist")

_SITEMAP_URL = "https://www.idealist.org/sitemap-jobs-en-1.xml"
_TIMEOUT_S = 25
_DETAIL_WORKERS = 6
_DEFAULT_MAX_AGE_DAYS = 90
# Detail page JSON-LD — first script with type=JobPosting wins.
_LDJSON_RE = re.compile(
    r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
# Job URL has the form /en/<category>-job/<hash>-<title-org-city-slug>
_JOB_URL_RE = re.compile(
    r'https?://www\.idealist\.org/en/[a-z]+-job/[a-f0-9]{32}-[a-z0-9\-]+'
)

_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": JobType.FULL_TIME,
    "PART_TIME": JobType.PART_TIME,
    "CONTRACTOR": JobType.CONTRACT,
    "CONTRACT": JobType.CONTRACT,
    "TEMPORARY": JobType.TEMPORARY,
    "INTERN": JobType.INTERNSHIP,
    "VOLUNTEER": JobType.VOLUNTEER,
}


class Idealist(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.IDEALIST, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted or 15
        start_offset = max(scraper_input.offset or 0, 0)

        urls = _fetch_sitemap_urls()
        if not urls:
            log.error("Idealist: sitemap fetch returned 0 URLs")
            return JobResponse(jobs=[])
        log.info(f"Idealist: sitemap has {len(urls)} live job URLs")

        # URL-level pre-filter by city + keyword.
        city_token = _city_slug(scraper_input.location)
        kw_tokens = _kw_tokens(scraper_input.search_term)
        candidates = _filter_urls(urls, city_token, kw_tokens)
        if not candidates:
            log.info(
                f"Idealist: 0 URLs matched filters "
                f"(city={city_token!r}, kw={kw_tokens!r}). "
                "Falling back to keyword-only filter."
            )
            # No city match — try without it, agencies often headquarter
            # elsewhere from where they hire.
            candidates = _filter_urls(urls, None, kw_tokens)
        if not candidates:
            log.info("Idealist: still 0 candidates after fallback")
            return JobResponse(jobs=[])

        # Cap how many detail pages we fetch — slug pre-filter is loose so
        # we expand to a multiple of wanted.
        fetch_target = min(len(candidates), max(wanted * 3, wanted + 20))
        url_subset = candidates[: fetch_target]
        log.info(f"Idealist: fetching {len(url_subset)} candidate detail pages")

        sess = cc_requests.Session(impersonate="chrome120")
        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=_DETAIL_WORKERS) as ex:
            futures = {ex.submit(_fetch_detail, sess, u): u for u in url_subset}
            for fut in as_completed(futures):
                url = futures[fut]
                data = fut.result()
                if data:
                    results[url] = data

        cutoff = _resolve_cutoff(scraper_input)
        now = datetime.now(timezone.utc)
        kw_lower = (scraper_input.search_term or "").lower().strip()

        jobs: list[JobPost] = []
        seen_ids: set[str] = set()
        dropped_old = 0
        dropped_expired = 0
        for url in url_subset:
            if url not in results:
                continue
            data = results[url]
            valid_through = _parse_iso_dt(data.get("validThrough"))
            if valid_through is not None and valid_through < now:
                dropped_expired += 1
                continue
            posted = _parse_iso_dt(data.get("datePosted"))
            if cutoff and posted and posted < cutoff:
                dropped_old += 1
                continue

            title = (data.get("title") or "").strip()
            if kw_lower and kw_lower not in title.lower():
                # Title hint failed — accept if the URL slug had the keyword
                # (some queries are too loose for a strict title match).
                slug_match = all(t in url.lower() for t in kw_tokens)
                if not slug_match:
                    continue

            location_obj = _build_location(data.get("jobLocation"))
            post = _build_jobpost(url, data, posted, location_obj)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)
            if len(jobs) >= start_offset + wanted:
                break

        if dropped_old or dropped_expired:
            log.info(
                f"Idealist: filtered {dropped_old} stale + "
                f"{dropped_expired} past-deadline postings"
            )
        jobs = jobs[start_offset : start_offset + wanted]
        log.info(f"Idealist: returning {len(jobs)} jobs (offset={start_offset})")
        return JobResponse(jobs=jobs)


def _fetch_sitemap_urls() -> list[str]:
    try:
        r = cc_requests.get(
            _SITEMAP_URL,
            impersonate="chrome120",
            timeout=_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001
        log.error(f"Idealist: sitemap fetch failed: {e}")
        return []
    if not r.ok:
        log.error(f"Idealist: sitemap returned status {r.status_code}")
        return []
    return _JOB_URL_RE.findall(r.text)


def _city_slug(loc: str | None) -> str | None:
    if not loc:
        return None
    city = loc.split(",")[0].strip().lower()
    if not city:
        return None
    # "St. Louis" → "st-louis" — match Idealist's slug format
    city = re.sub(r"[^a-z0-9\s]", "", city)
    city = re.sub(r"\s+", "-", city).strip("-")
    return city or None


def _kw_tokens(search_term: str | None) -> list[str]:
    if not search_term:
        return []
    return [t for t in re.split(r"\W+", search_term.lower()) if len(t) >= 3]


def _filter_urls(
    urls: list[str], city_token: str | None, kw_tokens: list[str]
) -> list[str]:
    """Slug-level URL filter — cheaper than fetching detail pages."""
    out: list[str] = []
    for url in urls:
        slug = url.rsplit("/", 1)[-1].lower()
        if city_token and city_token not in slug:
            continue
        if kw_tokens:
            if not any(tok in slug for tok in kw_tokens):
                continue
        out.append(url)
    return out


def _fetch_detail(sess: cc_requests.Session, url: str) -> dict | None:
    try:
        r = sess.get(url, timeout=_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        log.debug(f"Idealist: detail fetch {url[:80]} failed: {e!r}")
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
        if country == "US":
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
    url: str, data: dict, posted: datetime | None, location: Location | None,
) -> JobPost | None:
    title = (data.get("title") or "").strip()
    if not title:
        return None
    identifier = data.get("identifier") or {}
    pid = (
        identifier.get("value") if isinstance(identifier, dict) else None
    ) or url.rsplit("/", 1)[-1].split("-", 1)[0]

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

    description_html = data.get("description") or ""
    description = re.sub(r"<[^>]+>", " ", description_html)
    description = re.sub(r"\s+", " ", description).strip() or None

    return JobPost(
        id=f"idealist-{pid}",
        title=title,
        company_name=company,
        job_url=url,
        location=location,
        date_posted=posted.date() if posted else None,
        description=description,
        compensation=compensation,
        job_type=[job_type] if job_type else None,
        company_logo=company_logo,
    )
