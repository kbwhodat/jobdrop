"""iCIMS scraper — Google-dorked discovery + JSON-LD enrichment.

iCIMS is a multi-tenant ATS where each customer gets a subdomain like
``careers-{tenant}.icims.com`` or ``staff-{tenant}.icims.com``. There's
no public cross-tenant index, so the only way to find live postings
across the whole network is dorking. Detail pages embed a clean
Schema.org JobPosting JSON-LD block with ``datePosted``,
``validThrough``, ``hiringOrganization``, ``jobLocation``, etc.

## Stage 1: Google discovery (zendriver)

  Query: ``site:icims.com "<keywords>" "<location>"``

URL captures:
  - ``https://{prefix}-{tenant}.icims.com/jobs/{id}/{slug}/job``

## Stage 2: JSON-LD enrichment

For each URL, GET via curl_cffi and parse the JobPosting JSON-LD.
Two-layer freshness filter (matches Greenhouse/GovernmentJobs pattern):
  - Drop if ``validThrough`` < now (deadline passed)
  - Drop if ``datePosted`` < cutoff (default 90 days, override via hours_old)

Falls back to ``<h1>`` + ``og:title`` parsing if JSON-LD is missing.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus, urlparse

from curl_cffi import requests as cc_requests

from jobdrop.icims.util import log
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
_DETAIL_URL_RE = re.compile(
    r"https?://([a-zA-Z0-9-]+)\.icims\.com/jobs/(\d+)/[a-zA-Z0-9_%.-]+/job(?:[/?#]|$)"
)
_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<h1[^>]*>([^<]+)</h1>", re.IGNORECASE)
_OG_TITLE_RE = re.compile(
    r'<meta\s+property="og:title"\s+content="([^"]+)"', re.IGNORECASE
)
_OG_DESC_RE = re.compile(
    r'<meta\s+property="og:description"\s+content="([^"]+)"', re.IGNORECASE
)
_LOC_IN_TITLE_RE = re.compile(
    r"\bin\s+([A-Z][A-Za-z .\-]+?),\s*([A-Z][A-Za-z .\-]+?)(?:\s*\||\s*$)"
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

_RENDER_SLEEP_S = 3.0
_API_TIMEOUT_S = 15
_API_WORKERS = 8
_DEFAULT_MAX_AGE_DAYS = 90
_DEFAULT_MAX_SERP_PAGES = 5
_MAX_SERP_PAGES_HARDCAP = 10


class ICIMS(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.ICIMS, proxies=proxies, ca_cert=ca_cert)
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
                "iCIMS: zendriver is required for Google discovery. "
                "Install with: pip install zendriver"
            )
            return JobResponse(jobs=[])

        query = _build_query(scraper_input)
        log.info(f"iCIMS: Google query = {query!r}")

        # Walk enough SERP pages to cover offset + wanted (canonical Google
        # paging starts at 0; we slice jobs[offset:] at the end). Without
        # this, offset>0 just walks an overlapping SERP window.
        target_urls = start_offset + wanted * 2 + 5
        max_pages = min(
            _MAX_SERP_PAGES_HARDCAP,
            max(_DEFAULT_MAX_SERP_PAGES, (target_urls // 10) + 1),
        )
        try:
            urls = _run_async(_discover_via_google(query, target_urls, max_pages))
        except RuntimeError as e:
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                urls = _run_on_thread(_discover_via_google(query, target_urls, max_pages))
            else:
                raise
        # Sort discovered URLs alphabetically so offset-based pagination
        # is deterministic when the underlying Google SERP returns the
        # same URL set (it isn't always — Google ranking has noise — so
        # pagination across distant offsets remains best-effort, like
        # all dork-based scrapers).
        urls = sorted(urls)
        log.info(f"iCIMS: discovered {len(urls)} unique URLs")
        if not urls:
            return JobResponse(jobs=[])

        sess = cc_requests.Session(impersonate="safari17_2_ios")
        fetch_target = min(len(urls), max(target_urls, wanted + 10))
        # Preserve SERP-discovery order through parallel enrichment so that
        # offset-based slicing is stable across repeated scrapes.
        url_subset = urls[:fetch_target]
        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=_API_WORKERS) as ex:
            futures = {ex.submit(_fetch_detail, sess, u): u for u in url_subset}
            for fut in as_completed(futures):
                url = futures[fut]
                data = fut.result()
                if data:
                    results[url] = data
        enriched: list[tuple[str, dict]] = [
            (u, results[u]) for u in url_subset if u in results
        ]
        log.info(f"iCIMS: enriched {len(enriched)}/{fetch_target} detail pages")

        title_token = (scraper_input.search_term or "").lower().strip()
        cutoff = _resolve_cutoff(scraper_input)
        now = datetime.now(timezone.utc)

        seen_ids: set[str] = set()
        jobs: list[JobPost] = []
        dropped_old = 0
        dropped_expired = 0
        for url, data in enriched:
            valid_through = data.get("valid_through")
            if valid_through is not None and valid_through < now:
                dropped_expired += 1
                continue
            posted = data.get("posted_dt")
            if cutoff and posted and posted < cutoff:
                dropped_old += 1
                continue
            title = (data.get("title") or "").strip()
            if title_token and title_token not in title.lower():
                continue
            post = _build_jobpost(url, data, posted)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)

        if dropped_old or dropped_expired:
            log.info(
                f"iCIMS: filtered {dropped_old} stale + "
                f"{dropped_expired} past-deadline postings"
            )
        jobs = jobs[start_offset : start_offset + wanted]
        log.info(f"iCIMS: returning {len(jobs)} jobs (offset={start_offset})")
        return JobResponse(jobs=jobs)


def _build_query(si: ScraperInput) -> str:
    parts: list[str] = ["site:icims.com"]
    if si.search_term:
        parts.append(f'"{si.search_term}"')
    if si.location:
        city = si.location.split(",")[0].strip()
        if city:
            parts.append(f'"{city}"')
    return " ".join(parts)


async def _discover_via_google(
    query: str, target_count: int, max_pages: int = _DEFAULT_MAX_SERP_PAGES,
) -> list[str]:
    import zendriver as zd
    encoded = quote_plus(query)
    seen_urls: set[str] = set()
    ordered: list[str] = []
    browser = await zd.start(
        headless=True, sandbox=False, browser_args=["--window-size=1280,900"],
    )
    try:
        for page_idx in range(max_pages):
            url = _GOOGLE_SEARCH_URL.format(
                query=encoded, start=page_idx * 10,
            )
            log.info(f"iCIMS: SERP page {page_idx + 1} → {url[:120]}")
            try:
                tab = await browser.get(url)
            except Exception as e:
                log.error(f"iCIMS: SERP fetch failed on page {page_idx + 1}: {e}")
                break
            await asyncio.sleep(_RENDER_SLEEP_S)
            try:
                current = await tab.evaluate("location.href")
            except Exception:
                current = url
            if "/sorry/" in str(current):
                log.error(
                    f"iCIMS: hit Google /sorry/ on page {page_idx + 1}. "
                    "Returning what we have."
                )
                break
            try:
                html = await tab.get_content()
            except Exception:
                html = await tab.evaluate("document.documentElement.outerHTML") or ""

            new_count = 0
            for m in _DETAIL_URL_RE.finditer(html or ""):
                full = m.group(0).rstrip("/?#")
                full = full.split("&amp;")[0].split("#")[0].rstrip("/")
                if full not in seen_urls:
                    seen_urls.add(full)
                    ordered.append(full)
                    new_count += 1

            log.info(
                f"iCIMS: page {page_idx + 1} added {new_count} URLs "
                f"(total {len(ordered)} / target {target_count})"
            )
            if len(ordered) >= target_count:
                break
            if new_count == 0:
                break
    finally:
        await browser.stop()
    return ordered


def _fetch_detail(sess: cc_requests.Session, url: str) -> dict | None:
    try:
        r = sess.get(url, timeout=_API_TIMEOUT_S, allow_redirects=True)
    except Exception as e:
        log.debug(f"iCIMS: fetch {url[:80]} failed: {e!r}")
        return None
    if not r.ok:
        return None
    text = r.text
    if not text or "gone:" in text[:200].lower() or len(text) < 500:
        return None

    ld = _extract_jobposting_ldjson(text)
    if ld:
        return _build_data_from_ldjson(ld)
    return _build_data_from_meta(text)


def _extract_jobposting_ldjson(text: str) -> dict | None:
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


def _build_data_from_ldjson(d: dict) -> dict:
    title = (d.get("title") or "").strip() or None

    org = d.get("hiringOrganization") or {}
    company = (
        ((org.get("name") if isinstance(org, dict) else None) or "").strip() or None
    )

    location = _build_location(d.get("jobLocation"))

    posted_dt = _parse_iso_dt(d.get("datePosted"))
    valid_through = _parse_iso_dt(d.get("validThrough"))

    employment = d.get("employmentType")
    job_type = None
    if isinstance(employment, str):
        job_type = _EMPLOYMENT_TYPE_MAP.get(employment.upper())
    elif isinstance(employment, list) and employment:
        for e in employment:
            if isinstance(e, str):
                job_type = _EMPLOYMENT_TYPE_MAP.get(e.upper())
                if job_type:
                    break

    compensation = _build_compensation(d.get("baseSalary"))

    description_html = d.get("description") or ""
    description = re.sub(r"<[^>]+>", " ", description_html)
    description = re.sub(r"\s+", " ", description).strip() or None
    if description and len(description) > 8000:
        description = description[:8000]

    return {
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "posted_dt": posted_dt,
        "valid_through": valid_through,
        "job_type": job_type,
        "compensation": compensation,
    }


def _build_data_from_meta(text: str) -> dict | None:
    """Fallback when no JSON-LD: parse <h1> + og:* meta tags."""
    h1_m = _TITLE_RE.search(text)
    og_title_m = _OG_TITLE_RE.search(text)
    og_desc_m = _OG_DESC_RE.search(text)

    title = None
    if h1_m:
        title = h1_m.group(1).strip()
    if not title and og_title_m:
        title = og_title_m.group(1).split("|")[0].split(" in ")[0].strip()
    if not title:
        return None

    location = None
    if og_title_m:
        loc_m = _LOC_IN_TITLE_RE.search(og_title_m.group(1))
        if loc_m:
            city = loc_m.group(1).strip()
            state = loc_m.group(2).strip().split("|")[0].strip()
            location = Location(city=city, state=state)

    description = None
    if og_desc_m:
        description = re.sub(r"\s+", " ", og_desc_m.group(1)).strip()[:4000] or None

    return {
        "title": title,
        "company": None,
        "location": location,
        "description": description,
        "posted_dt": None,
        "valid_through": None,
        "job_type": None,
        "compensation": None,
    }


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


def _resolve_cutoff(si: ScraperInput) -> datetime | None:
    hours = getattr(si, "hours_old", None)
    if hours and hours > 0:
        return datetime.now(timezone.utc) - timedelta(hours=hours)
    return datetime.now(timezone.utc) - timedelta(days=_DEFAULT_MAX_AGE_DAYS)


def _company_from_host(url: str) -> str | None:
    try:
        host = urlparse(url).netloc
    except Exception:
        return None
    sub = host.split(".")[0] if host.endswith(".icims.com") else None
    if not sub:
        return None
    for prefix in ("careers-", "staff-", "non-clinical-", "jobs-"):
        if sub.startswith(prefix):
            sub = sub[len(prefix) :]
            break
    return sub.replace("-", " ").title() if sub else None


def _build_jobpost(
    url: str, data: dict, posted_dt: datetime | None,
) -> JobPost | None:
    title = (data.get("title") or "").strip()
    if not title:
        return None
    m = re.search(r"/jobs/(\d+)/", url)
    pid = m.group(1) if m else url.rsplit("/", 1)[-1]
    host = urlparse(url).netloc
    company = data.get("company") or _company_from_host(url)
    job_type = data.get("job_type")
    return JobPost(
        id=f"icims-{host}-{pid}",
        title=title,
        company_name=company,
        job_url=url,
        location=data.get("location"),
        date_posted=posted_dt.date() if posted_dt else None,
        description=data.get("description"),
        compensation=data.get("compensation"),
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
