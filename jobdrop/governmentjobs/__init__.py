"""GovernmentJobs (NEOGOV) scraper — Google-dorked discovery + JSON-LD enrichment.

GovernmentJobs.com is a NEOGOV-operated SPA aggregator for state, county,
and city public-sector jobs (the non-federal companion to USAJobs). Search
UI is JS-rendered with no public JSON API exposed to anonymous clients.
Detail pages, however, are server-rendered and embed a clean Schema.org
JobPosting JSON-LD block.

## Stage 1: Google discovery (zendriver)

  Query: ``site:governmentjobs.com "<keywords>" "<location>"``

URL captures:
  - ``https://www.governmentjobs.com/careers/{agency}/jobs/{id}``
  - ``https://www.governmentjobs.com/jobs/{id}/{slug}``

## Stage 2: JSON-LD enrichment

For each URL, GET via curl_cffi and parse Schema.org JobPosting object.
Two-layer freshness filter (matches Greenhouse pattern):
  - Drop if validThrough < now (deadline passed)
  - Drop if datePosted < cutoff (default 90 days, override via hours_old)
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus

from curl_cffi import requests as cc_requests

from jobdrop.governmentjobs.util import log
from jobdrop.model import (
    Compensation,
    CompensationInterval,
    JobPost,
    JobResponse,
    JobType,
    Location,
    Scraper,
    ScraperInput,
    Site,
)

_GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&start={start}"
_DETAIL_URL_RES = [
    re.compile(r"https?://www\.governmentjobs\.com/careers/[a-zA-Z0-9_-]+/jobs/(\d+)(?:[/?#]|$)"),
    re.compile(r"https?://www\.governmentjobs\.com/jobs/(\d+)/[a-zA-Z0-9_-]+(?:[/?#]|$)"),
]
_LDJSON_RE = re.compile(
    r'<script\s+type="application/ld\+json">([^<]+)</script>',
    re.IGNORECASE,
)

_RENDER_SLEEP_S = 3.0
_API_TIMEOUT_S = 20
_API_WORKERS = 8
_DEFAULT_MAX_AGE_DAYS = 90

_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": JobType.FULL_TIME,
    "PART_TIME": JobType.PART_TIME,
    "CONTRACTOR": JobType.CONTRACT,
    "TEMPORARY": JobType.TEMPORARY,
    "INTERN": JobType.INTERNSHIP,
}


class GovernmentJobs(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.GOVERNMENTJOBS, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self.user_agent = user_agent

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted or 15
        start_offset = max(scraper_input.offset or 0, 0)

        try:
            import zendriver as zd  # noqa: F401
        except ImportError:
            log.error(
                "GovernmentJobs: zendriver is required for Google discovery. "
                "Install with: pip install zendriver"
            )
            return JobResponse(jobs=[])

        query = _build_query(scraper_input)
        log.info(f"GovernmentJobs: Google query = {query!r}")

        try:
            urls = _run_async(_discover_via_google(query, wanted, start_offset))
        except RuntimeError as e:
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                urls = _run_on_thread(_discover_via_google(query, wanted, start_offset))
            else:
                raise
        log.info(f"GovernmentJobs: discovered {len(urls)} unique URLs")
        if not urls:
            return JobResponse(jobs=[])

        sess = cc_requests.Session(impersonate="safari17_2_ios")
        enriched: list[tuple[str, dict]] = []
        fetch_target = min(len(urls), max(wanted * 2, wanted + 10))
        with ThreadPoolExecutor(max_workers=_API_WORKERS) as ex:
            futures = {ex.submit(_fetch_detail, sess, u): u for u in urls[:fetch_target]}
            for fut in as_completed(futures):
                url = futures[fut]
                data = fut.result()
                if data:
                    enriched.append((url, data))
        log.info(f"GovernmentJobs: enriched {len(enriched)}/{fetch_target} detail pages")

        title_token = (scraper_input.search_term or "").lower().strip()
        cutoff = _resolve_cutoff(scraper_input)
        now = datetime.now(timezone.utc)

        seen_ids: set[str] = set()
        jobs: list[JobPost] = []
        dropped_old = 0
        dropped_expired = 0
        for url, data in enriched:
            valid_through = _parse_iso_dt(data.get("validThrough"))
            if valid_through is not None and valid_through < now:
                dropped_expired += 1
                continue
            posted = _parse_iso_dt(data.get("datePosted"))
            if cutoff and posted and posted < cutoff:
                dropped_old += 1
                continue

            title = (data.get("title") or "").strip()
            if title_token and title_token not in title.lower():
                continue
            # Location filter intentionally NOT re-applied here: the dork
            # SERP query already pre-filtered by location (Google returns
            # pages where the city name appears anywhere — JD body, agency
            # name, etc.). Re-checking the JSON-LD jobLocation would over-
            # filter, since public-sector jobs are tagged with the
            # agency's HQ address (e.g. "Lawrenceville, GA" for Gwinnett
            # County jobs that serve metro Atlanta).
            loc = _build_location(data.get("jobLocation"))

            post = _build_jobpost(url, data, posted, loc)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)

        if dropped_old or dropped_expired:
            log.info(
                f"GovernmentJobs: filtered {dropped_old} stale + "
                f"{dropped_expired} past-deadline postings"
            )
        jobs = jobs[start_offset : start_offset + wanted]
        log.info(f"GovernmentJobs: returning {len(jobs)} jobs (offset={start_offset})")
        return JobResponse(jobs=jobs)


def _build_query(si: ScraperInput) -> str:
    parts: list[str] = ["site:governmentjobs.com"]
    if si.search_term:
        parts.append(f'"{si.search_term}"')
    if si.location:
        city = si.location.split(",")[0].strip()
        if city:
            parts.append(f'"{city}"')
    return " ".join(parts)


async def _discover_via_google(
    query: str, wanted: int, start_offset: int = 0,
) -> list[str]:
    import zendriver as zd
    encoded = quote_plus(query)
    seen_urls: set[str] = set()
    ordered: list[str] = []
    browser = await zd.start(
        headless=True, sandbox=False, browser_args=["--window-size=1280,900"],
    )
    try:
        for page_idx in range(5):
            url = _GOOGLE_SEARCH_URL.format(query=encoded, start=start_offset + page_idx * 10)
            log.info(f"GovernmentJobs: SERP page {page_idx + 1} → {url[:120]}")
            try:
                tab = await browser.get(url)
            except Exception as e:
                log.error(f"GovernmentJobs: SERP fetch failed on page {page_idx + 1}: {e}")
                break
            await asyncio.sleep(_RENDER_SLEEP_S)
            try:
                current = await tab.evaluate("location.href")
            except Exception:
                current = url
            if "/sorry/" in str(current):
                log.error(
                    f"GovernmentJobs: hit Google /sorry/ on page {page_idx + 1}. "
                    "Returning what we have."
                )
                break
            try:
                html = await tab.get_content()
            except Exception:
                html = await tab.evaluate("document.documentElement.outerHTML") or ""

            new_count = 0
            for rx in _DETAIL_URL_RES:
                for m in rx.finditer(html or ""):
                    full = m.group(0).rstrip("/?#")
                    if full not in seen_urls:
                        seen_urls.add(full)
                        ordered.append(full)
                        new_count += 1

            log.info(
                f"GovernmentJobs: page {page_idx + 1} added {new_count} URLs "
                f"(total {len(ordered)} / wanted {wanted})"
            )
            if len(ordered) >= max(wanted, 20):
                break
            if new_count == 0:
                break
    finally:
        await browser.stop()
    return ordered


def _fetch_detail(sess: cc_requests.Session, url: str) -> dict | None:
    try:
        r = sess.get(url, timeout=_API_TIMEOUT_S)
    except Exception as e:
        log.debug(f"GovernmentJobs: fetch {url[:80]} failed: {e!r}")
        return None
    if not r.ok:
        return None
    text = r.text
    for blob in _LDJSON_RE.findall(text):
        try:
            data = json.loads(blob)
        except Exception:
            continue
        if isinstance(data, list):
            for d in data:
                if isinstance(d, dict) and d.get("@type") == "JobPosting":
                    return d
        elif isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
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
        country = addr.get("addressCountry") or None
        if isinstance(country, dict):
            country = country.get("name") or None
        if isinstance(country, str):
            country = country.strip() or None
        if city or state or country:
            return Location(city=city, state=state, country=country)
    return None


def _flatten_location(loc: Location) -> str:
    parts = []
    if loc.city: parts.append(loc.city)
    if loc.state: parts.append(loc.state)
    if isinstance(loc.country, str) and loc.country: parts.append(loc.country)
    return ", ".join(parts)


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
    m = re.search(r"/jobs/(\d+)", url)
    pid = m.group(1) if m else url.rsplit("/", 1)[-1]

    org = data.get("hiringOrganization") or {}
    company = ((org.get("name") if isinstance(org, dict) else None) or "").strip() or None

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
        id=f"governmentjobs-{pid}",
        title=title,
        company_name=company,
        job_url=url,
        location=location,
        date_posted=posted.date() if posted else None,
        description=description,
        compensation=compensation,
        job_type=[job_type] if job_type else None,
    )


def _run_async(coro):
    return asyncio.run(coro)


def _run_on_thread(coro):
    result_box: dict = {}

    def runner():
        try:
            result_box["ok"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            result_box["err"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "err" in result_box:
        raise result_box["err"]
    return result_box.get("ok")
