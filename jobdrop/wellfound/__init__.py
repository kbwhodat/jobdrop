"""Wellfound (formerly AngelList) scraper — startup-job listings.

## Why Camoufox

Wellfound is fronted by DataDome with a per-route WAF rule on `/role/*`
that requires a high-trust `datadome` cookie (only earnable by passing
DD's JS challenge in a real browser).

Empirically validated against /role/* + /jobs (May 2026, 6 URLs back-
to-back, 0 blocks):
  - curl_cffi (26 TLS profiles)        → blocked on /role/*
  - selenium-driverless headed         → passes /jobs (untested /role/*)
  - selenium-driverless headless       → blocked
  - Patchright headed + headless       → blocked everywhere
  - Zendriver headless                 → blocked everywhere
  - Pydoll headless                    → blocked everywhere
  - Camoufox headless                  → PASSES BOTH

Camoufox is a Firefox fork with C++-level fingerprint spoofing.
DD's headless detection is heavily Chromium-tuned, so Firefox sails
through where every Chromium-based tool fails.

## Lazy fetch

First call downloads ~300MB patched Firefox to ~/.cache/camoufox/
(takes ~30s on a fast connection). Subsequent calls reuse the binary.
The MCP daemon's first Wellfound query is therefore slow; everything
after is ~5-6s/page.

## URL strategy

  search_term + location          → /role/l/{role}/{city}
  search_term + is_remote=True    → /role/l/{role}/remote
  search_term only                → /role/{role}
  no search_term                  → /jobs (49-job featured carousel)
"""
from __future__ import annotations

import asyncio
import re
import threading
from datetime import datetime, timezone
from typing import Any

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
from jobdrop.wellfound.util import log

_BASE = "https://wellfound.com"
_NAV_TIMEOUT_MS = 30_000
_RENDER_SLEEP_S = 5.0
_MAX_PAGES = 5  # safety cap


class Wellfound(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.WELLFOUND, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self.user_agent = user_agent

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        try:
            from camoufox.async_api import AsyncCamoufox  # noqa: F401
        except ImportError:
            log.error(
                "wellfound: camoufox not installed. "
                "Add `camoufox` (auto-downloads patched Firefox on first use)."
            )
            return JobResponse(jobs=[])

        try:
            jobs = asyncio.run(self._scrape_async(scraper_input))
        except RuntimeError as e:
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                log.info("wellfound: nested loop detected; running on dedicated thread")
                jobs = _run_on_thread(self._scrape_async(scraper_input))
            else:
                raise

        log.info(f"wellfound: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)

    async def _scrape_async(self, si: ScraperInput) -> list[JobPost]:
        from camoufox.async_api import AsyncCamoufox

        wanted = max(si.results_wanted or 15, 1)
        hours_old = si.hours_old
        cutoff_ts = (
            datetime.now(timezone.utc).timestamp() - hours_old * 3600
            if hours_old
            else None
        )

        base_path = self._base_path(si)
        log.info(f"wellfound: base path = {base_path!r}, results_wanted={wanted}")

        jobs: list[JobPost] = []
        seen_ids: set[str] = set()

        async with AsyncCamoufox(headless=True, humanize=True) as browser:
            page = await browser.new_page()
            try:
                await page.goto(_BASE + "/", wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                await asyncio.sleep(3)
            except Exception as e:
                log.warning(f"wellfound: warm / failed: {e!r}")

            for page_num in range(1, _MAX_PAGES + 1):
                url = _BASE + base_path
                url += f"&page={page_num}" if "?" in url else f"?page={page_num}"

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                    await asyncio.sleep(_RENDER_SLEEP_S)
                    html = await page.content()
                except Exception as e:
                    log.warning(f"wellfound: page {page_num} fetch failed: {e!r}")
                    break

                page_jobs, has_more = self._parse_html(html, si, cutoff_ts, seen_ids)
                jobs.extend(page_jobs)
                log.info(
                    f"wellfound: page {page_num} → {len(page_jobs)} new "
                    f"(total {len(jobs)}, has_more={has_more})"
                )

                if len(jobs) >= wanted:
                    jobs = jobs[:wanted]
                    break
                if not has_more:
                    break

        return jobs

    def _base_path(self, si: ScraperInput) -> str:
        role = si.search_term
        loc = si.location

        if not role:
            return "/jobs"

        role_slug = _slug(role)
        if not role_slug:
            return "/jobs"

        if si.is_remote:
            return f"/role/l/{role_slug}/remote"

        if loc:
            city = _city_slug(loc)
            if city:
                return f"/role/l/{role_slug}/{city}"
        return f"/role/{role_slug}"

    def _parse_html(
        self,
        html: str,
        si: ScraperInput,
        cutoff_ts: float | None,
        seen_ids: set[str],
    ) -> tuple[list[JobPost], bool]:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            log.warning("wellfound: __NEXT_DATA__ missing — DD challenge or page change")
            return [], False

        import json
        try:
            nd = json.loads(m.group(1))
        except Exception as e:
            log.warning(f"wellfound: __NEXT_DATA__ parse error: {e!r}")
            return [], False

        try:
            data = nd["props"]["pageProps"]["apolloState"]["data"]
        except (KeyError, TypeError):
            log.warning("wellfound: apolloState.data not found")
            return [], False

        results_container = None
        results_key = None
        for k, v in data.get("ROOT_QUERY", {}).items():
            if k.startswith("seoLandingPageJobSearchResults") and isinstance(v, dict):
                results_container = v
                results_key = k
                break

        startup_refs: list[str] = []
        if results_container:
            startups_list = results_container.get("startups") or []
            startup_refs = [
                r.get("__ref") for r in startups_list
                if isinstance(r, dict) and r.get("__ref")
            ]
            page_count = results_container.get("pageCount") or 1
            current_page = self._infer_page_from_key(results_key) or 1
            has_more = current_page < page_count
        else:
            startup_refs = [
                k for k, v in data.items()
                if isinstance(v, dict) and v.get("__typename") == "StartupResult"
            ]
            has_more = False

        jobs_out: list[JobPost] = []
        for ref in startup_refs:
            startup = data.get(ref)
            if not isinstance(startup, dict):
                continue
            company_name = startup.get("name")
            company_slug = startup.get("slug")
            company_logo = startup.get("logoUrl")
            company_url = f"{_BASE}/company/{company_slug}" if company_slug else None

            for jl_ref in startup.get("highlightedJobListings") or []:
                jl_key = jl_ref.get("__ref") if isinstance(jl_ref, dict) else None
                if not jl_key:
                    continue
                listing = data.get(jl_key)
                if not isinstance(listing, dict):
                    continue
                post = self._build_post(
                    listing,
                    company_name=company_name,
                    company_url=company_url,
                    company_logo=company_logo,
                )
                if post is None or post.id in seen_ids:
                    continue
                if cutoff_ts is not None and listing.get("liveStartAt"):
                    if float(listing["liveStartAt"]) < cutoff_ts:
                        continue
                if si.is_remote and not post.is_remote:
                    continue
                if (
                    si.job_type
                    and post.job_type
                    and si.job_type not in post.job_type
                ):
                    continue
                seen_ids.add(post.id)
                jobs_out.append(post)

        return jobs_out, has_more

    @staticmethod
    def _infer_page_from_key(key: str | None) -> int | None:
        m = re.search(r'"page"\s*:\s*(\d+)', key or "")
        return int(m.group(1)) if m else None

    def _build_post(
        self,
        listing: dict,
        *,
        company_name: str | None,
        company_url: str | None,
        company_logo: str | None,
    ) -> JobPost | None:
        job_id = str(listing.get("id") or "").strip()
        title = (listing.get("title") or "").strip()
        if not job_id or not title:
            return None

        # Wellfound's router requires the SEO slug — bare /jobs/{id} returns 404.
        # Canonical form: /jobs/{id}-{slug}. Slug is always present in the API
        # response; fall back to bare-id only if somehow empty.
        slug = (listing.get("slug") or "").strip()
        job_url = f"{_BASE}/jobs/{job_id}-{slug}" if slug else f"{_BASE}/jobs/{job_id}"
        loc_names = listing.get("locationNames") or []
        location = _build_location(loc_names[0] if loc_names else None)

        date_posted = None
        live_start = listing.get("liveStartAt")
        if isinstance(live_start, (int, float)):
            try:
                date_posted = datetime.fromtimestamp(
                    float(live_start), tz=timezone.utc
                ).date()
            except (OverflowError, ValueError, OSError):
                date_posted = None

        compensation = _parse_compensation(listing.get("compensation"))
        job_type_list = _map_job_type(listing.get("jobType"))

        is_remote = bool(listing.get("remote"))
        if not is_remote and isinstance(title, str) and "remote" in title.lower():
            is_remote = True

        return JobPost(
            id=job_id,
            title=title,
            company_name=company_name,
            job_url=job_url,
            location=location,
            description=listing.get("description") or None,
            company_url=company_url,
            company_logo=company_logo,
            job_type=job_type_list,
            compensation=compensation,
            date_posted=date_posted,
            is_remote=is_remote,
        )


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _slug(s: str) -> str:
    """'Data Engineer' → 'data-engineer'; 'C++ Developer' → 'c-developer'."""
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s/+\-]", "", s)
    s = re.sub(r"[\s/+]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _city_slug(loc: str) -> str:
    """'Atlanta, GA' → 'atlanta'; 'San Francisco, CA' → 'san-francisco'."""
    if not loc:
        return ""
    return _slug(loc.split(",")[0].strip())


def _build_location(raw: str | None) -> Location | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    city = parts[0]
    state = parts[1] if len(parts) >= 2 else None
    country = parts[2] if len(parts) >= 3 else None
    return Location(city=city, state=state, country=country)


def _to_amount(num_s: str, suffix: str) -> float:
    n = float(num_s)
    if suffix.lower() == "k":
        n *= 1_000
    elif suffix.lower() == "m":
        n *= 1_000_000
    return n


def _detect_interval(s: str) -> CompensationInterval:
    sl = s.lower()
    if "/hour" in sl or "hourly" in sl or "/hr" in sl or "per hour" in sl:
        return CompensationInterval.HOURLY
    if "/week" in sl or "weekly" in sl or "per week" in sl:
        return CompensationInterval.WEEKLY
    if "/month" in sl or "monthly" in sl or "per month" in sl:
        return CompensationInterval.MONTHLY
    return CompensationInterval.YEARLY


_RANGE_RE = re.compile(
    r"\$\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*[\u2013\u2014\-~]+\s*\$?\s*(\d+(?:\.\d+)?)\s*([kKmM]?)"
)
_SINGLE_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)\s*([kKmM]?)")


def _parse_compensation(s: str | None) -> Compensation | None:
    """Wellfound shape: '$136k – $180k' / '$50/hour' / '$120k – $160k • 0.05% – 0.2%'."""
    if not s:
        return None
    sl = s.lower()
    if "no salary" in sl or "not listed" in sl:
        return None

    cash = s.split("•")[0]  # strip equity component
    interval = _detect_interval(cash)

    m = _RANGE_RE.search(cash)
    if m:
        try:
            return Compensation(
                min_amount=_to_amount(m.group(1), m.group(2)),
                max_amount=_to_amount(m.group(3), m.group(4)),
                currency="USD",
                interval=interval,
            )
        except (ValueError, OverflowError):
            return None

    m = _SINGLE_RE.search(cash)
    if m:
        try:
            v = _to_amount(m.group(1), m.group(2))
            return Compensation(
                min_amount=v, max_amount=v, currency="USD", interval=interval
            )
        except (ValueError, OverflowError):
            return None

    return None


_JOB_TYPE_MAP = {
    "full-time": JobType.FULL_TIME,
    "fulltime": JobType.FULL_TIME,
    "full_time": JobType.FULL_TIME,
    "part-time": JobType.PART_TIME,
    "parttime": JobType.PART_TIME,
    "part_time": JobType.PART_TIME,
    "contract": JobType.CONTRACT,
    "contractor": JobType.CONTRACT,
    "internship": JobType.INTERNSHIP,
    "intern": JobType.INTERNSHIP,
    "cofounder": JobType.OTHER,
    "co-founder": JobType.OTHER,
    "temporary": JobType.TEMPORARY,
    "temp": JobType.TEMPORARY,
    "volunteer": JobType.VOLUNTEER,
}


def _map_job_type(jt: str | None) -> list[JobType] | None:
    if not jt:
        return None
    val = _JOB_TYPE_MAP.get(jt.lower().strip())
    return [val] if val else None


def _run_on_thread(coro):
    """Execute an awaitable from sync code that's inside a running loop."""
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
