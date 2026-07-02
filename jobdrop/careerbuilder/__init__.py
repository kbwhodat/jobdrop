"""CareerBuilder scraper — broad US/CA general-purpose job board.

CareerBuilder's search results pages are gated by a Cloudflare WAF that
403s every Chrome/Edge TLS fingerprint we tried. Empirically the
``safari17_2_ios`` impersonation profile in ``curl_cffi`` slips
through cleanly. With that profile, the SSR'd HTML embeds a complete
``jobResults`` JSON array (50 jobs per page) with full structured
fields — title, url, datePosted, employmentType, jobLocation (with
address + coordinates), baseSalary (min/max/unit), hiringOrganization
(name + logo + description). No browser, no Google dorking, no
detail-page enrichment required.

URL contract:

  https://www.careerbuilder.com/jobs-<keyword-slug>-in-<city>,<state>
  https://www.careerbuilder.com/jobs-in-<city>,<state>                 (no kw)

Pagination:

  ``?page=<n>&sid=<session_uuid>``

The ``sid`` is established on page 1 and embedded in every "next page"
``<a rel="next" href="...">`` link. Subsequent page fetches need both
the session cookies *and* the ``sid`` query param — without ``sid``
each request returns page-1 results regardless of ``?page=``.

## Caveats

  - Keyword + city must form a valid SEO slug. Multi-word keywords use
    hyphens ("python-developer"). State must be a 2-letter code lowercased.
  - Salary present on ~40-50% of postings (when employer set it).
  - Date filter happens client-side after parse (no server-side cutoff).
"""
from __future__ import annotations

import json
import re
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

log = create_logger("CareerBuilder")

_BASE = "https://www.careerbuilder.com"
_TIMEOUT_S = 25
_PAGE_SIZE = 50
_DEFAULT_MAX_AGE_DAYS = 90
# Empirically: safari17_2_ios is the only impersonation profile that
# bypasses CareerBuilder's Cloudflare WAF reliably. chrome120 / edge101
# return 403. chrome116 also works but is older — Safari iOS is the
# safer long-term pick.
_IMPERSONATE = "safari17_2_ios"

_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": JobType.FULL_TIME,
    "PART_TIME": JobType.PART_TIME,
    "CONTRACTOR": JobType.CONTRACT,
    "CONTRACT": JobType.CONTRACT,
    "TEMPORARY": JobType.TEMPORARY,
    "INTERN": JobType.INTERNSHIP,
    "INTERNSHIP": JobType.INTERNSHIP,
}


class CareerBuilder(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.CAREERBUILDER, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted or 15

        # Two-tier path strategy:
        #   1) keyword+location slug (precise SEO page, server-side filtered)
        #   2) location-only slug + client-side title filter (when (1) 404s,
        #      which happens for keywords that don't have a pre-built SEO
        #      page — e.g. "developer", "manager", "engineer").
        primary_path = _build_search_path(
            scraper_input.search_term, scraper_input.location
        )
        fallback_path = (
            _build_search_path(None, scraper_input.location)
            if scraper_input.search_term and scraper_input.location
            else None
        )
        if not primary_path:
            log.error(
                "CareerBuilder: search requires either a keyword or a "
                "location to build a valid URL slug"
            )
            return JobResponse(jobs=[])

        sess = cc_requests.Session(impersonate=_IMPERSONATE)

        jobs = self._walk_pages(
            sess,
            primary_path,
            scraper_input,
            wanted,
            client_side_filter=False,
        )
        if not jobs and fallback_path and fallback_path != primary_path:
            log.info(
                "CareerBuilder: primary slug returned 0; "
                f"falling back to {fallback_path!r} + client-side keyword filter"
            )
            jobs = self._walk_pages(
                sess,
                fallback_path,
                scraper_input,
                wanted,
                client_side_filter=True,
            )

        log.info(f"CareerBuilder: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)

    def _walk_pages(
        self,
        sess: cc_requests.Session,
        slug_path: str,
        scraper_input: ScraperInput,
        wanted: int,
        *,
        client_side_filter: bool,
    ) -> list[JobPost]:
        jobs: list[JobPost] = []
        seen_ids: set[str] = set()
        cutoff = _resolve_cutoff(scraper_input)
        sid: str | None = None
        page = 1 + (scraper_input.offset // _PAGE_SIZE)
        kw_lower = (scraper_input.search_term or "").lower().strip()
        kw_tokens = [t for t in re.split(r"\W+", kw_lower) if len(t) >= 3]

        for _ in range(20):
            url = f"{_BASE}{slug_path}"
            params: dict[str, Any] = {}
            if page > 1:
                params["page"] = page
                if sid:
                    params["sid"] = sid
            try:
                r = sess.get(url, params=params, timeout=_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                log.error(f"CareerBuilder: page {page} fetch failed: {e}")
                break
            if not r.ok:
                log.error(
                    f"CareerBuilder: page {page} status {r.status_code} for {slug_path}"
                )
                break

            results = _extract_job_results(r.text)
            if not results:
                break

            new_this_page = 0
            for item in results:
                post = _build_jobpost(item, scraper_input.country, cutoff)
                if post is None or post.id in seen_ids:
                    continue
                if client_side_filter and kw_tokens:
                    title_lc = post.title.lower()
                    if not any(t in title_lc for t in kw_tokens):
                        continue
                seen_ids.add(post.id)
                jobs.append(post)
                new_this_page += 1
                if len(jobs) >= wanted:
                    break

            log.info(
                f"CareerBuilder: page {page} → {new_this_page} new (total {len(jobs)})"
            )
            if len(jobs) >= wanted:
                break
            if sid is None:
                sid_m = re.search(r"sid=([a-f0-9\-]+)", r.text)
                if sid_m:
                    sid = sid_m.group(1)
            if not re.search(r'rel="next"[^>]*href="[^"]+"', r.text):
                break
            page += 1
        return jobs


def _build_search_path(keyword: str | None, location: str | None) -> str | None:
    kw_slug = _slugify(keyword)
    if location:
        parts = [p.strip() for p in location.split(",") if p.strip()]
        city_slug = _slugify(parts[0]) if parts else None
        state_slug = (
            re.sub(r"[^a-z]", "", parts[1].lower()) if len(parts) > 1 else None
        )
        # Only emit /jobs-{kw}-in-{city},{state} when state is a 2-letter US code.
        # Anything else falls back to keyword-only or location-only paths.
        if city_slug and state_slug and len(state_slug) == 2:
            if kw_slug:
                return f"/jobs-{kw_slug}-in-{city_slug},{state_slug}"
            return f"/jobs-in-{city_slug},{state_slug}"
    if kw_slug:
        # Keyword-only search (national)
        return f"/jobs-{kw_slug}"
    return None


def _slugify(text: str | None) -> str | None:
    if not text:
        return None
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9\s\-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or None


def _extract_job_results(html: str) -> list[dict]:
    """Pull the inline ``"jobResults":[...]`` array out of the SSR'd HTML.

    Bracket-balances through the array so we don't depend on a precise
    closing-token regex. Returns [] when not found.
    """
    m = re.search(r'"jobResults"\s*:\s*\[', html)
    if not m:
        return []
    start = m.end() - 1  # position of opening [
    depth = 0
    i = start
    in_str = False
    esc = False
    while i < len(html):
        c = html[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    break
        i += 1
    if depth != 0:
        return []
    try:
        return json.loads(html[start : i + 1])
    except (ValueError, json.JSONDecodeError):
        return []


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


def _build_location(value: Any, country: Country | None) -> Location | None:
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
        resolved_country: str | Country | None
        if isinstance(country_raw, dict):
            resolved_country = country_raw.get("name") or None
        elif isinstance(country_raw, str):
            resolved_country = country_raw.strip() or None
        else:
            resolved_country = None
        if resolved_country in ("US", "USA", "United States"):
            resolved_country = Country.USA
        if not resolved_country and country is not None:
            resolved_country = country
        if city or state or resolved_country:
            return Location(city=city, state=state, country=resolved_country)
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
    item: dict, country: Country | None, cutoff: datetime | None
) -> JobPost | None:
    try:
        if item.get("status") and item["status"] != "ACTIVE":
            return None
        job_id = item.get("jobId")
        posting = item.get("jobPosting") or {}
        if not job_id or not isinstance(posting, dict):
            return None
        title = (posting.get("title") or "").strip()
        if not title:
            return None
        title = " ".join(title.split())

        posted = _parse_iso_dt(posting.get("datePosted"))
        if cutoff and posted and posted < cutoff:
            return None

        org = posting.get("hiringOrganization") or {}
        company = (
            (org.get("name") if isinstance(org, dict) else None) or ""
        ).strip() or None
        company_logo = org.get("logo") if isinstance(org, dict) else None

        employment = posting.get("employmentType")
        job_types: list[JobType] = []
        if isinstance(employment, str):
            mapped = _EMPLOYMENT_TYPE_MAP.get(employment.upper())
            if mapped:
                job_types.append(mapped)
        elif isinstance(employment, list):
            for e in employment:
                if isinstance(e, str):
                    mapped = _EMPLOYMENT_TYPE_MAP.get(e.upper())
                    if mapped and mapped not in job_types:
                        job_types.append(mapped)

        compensation = _build_compensation(posting.get("baseSalary"))
        location_obj = _build_location(posting.get("jobLocation"), country)

        # CareerBuilder canonicalUrl includes utm params we don't need —
        # prefer the cleaner jobPosting.url, fall back to canonicalUrl.
        job_url = (posting.get("url") or item.get("canonicalUrl") or "").strip()
        # Strip the marketing query string for stable comparison
        job_url = re.sub(r"\?mstr_dist=true.*$", "", job_url)

        return JobPost(
            id=f"careerbuilder-{job_id}",
            title=title,
            company_name=company,
            location=location_obj,
            job_url=job_url,
            date_posted=posted.date() if posted else None,
            compensation=compensation,
            job_type=job_types or None,
            company_logo=company_logo,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"CareerBuilder: skipping malformed item: {e}")
        return None
