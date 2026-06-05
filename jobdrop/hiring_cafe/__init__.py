"""Hiring Cafe scraper — AI-enriched job listings via direct JSON endpoint.

## Headless via curl_cffi

The static-data Next.js endpoint `/_next/data/{buildId}/index.json` is
NOT behind the same Cloudflare challenge that gates the home page. It
accepts the same `searchState` query param the SPA uses and returns
the same `pageProps.ssrHits` — server-side filtered.

Pure curl_cffi safari17_2_ios → sub-second response, no browser, no
GUI, no anti-bot battle.

## Architecture

  Caller → curl_cffi safari17_2_ios →
    GET /_next/data/{buildId}/index.json?searchState={url-encoded JSON}
  → response.pageProps.ssrHits[] → JobPost objects

When HC deploys a new build, buildId changes. We cache the current
buildId at module level. If a fetch returns 404 (buildId stale), we
fall back ONCE to a headless Camoufox fetch (Firefox-based, the only
headless path that defeats CF on the home page — same pattern as
``jobdrop.wellfound``) to extract a fresh buildId, update the cache,
and retry.

## SearchState shape

  - searchQuery: keyword (with location appended as bag-of-words)
  - workplaceTypes: ["Remote"] / ["Remote","Hybrid","Onsite"]
  - commitmentTypes: ["Full Time"] etc., maps from JobType
  - dateFetchedPastNDays: from hours_old / 24
  - sortBy: "default"
  - page: 1, 2, 3, ...
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import datetime
from typing import Any
from urllib.parse import quote

from curl_cffi import requests as cc_requests

from jobdrop.hiring_cafe.util import log
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

_BASE = "https://hiring.cafe"
_NEXT_DATA_TIMEOUT_S = 20
_MAX_PAGES = 5
_PER_PAGE = 120

# Module-level cache — refreshed on 404 (when HC deploys a new build).
_BUILD_ID: str = "guHPklrF3GXUbbW7723hM"
_BUILD_ID_LOCK = threading.Lock()


class HiringCafe(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.HIRING_CAFE, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self.user_agent = user_agent

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = max(scraper_input.results_wanted or 15, 1)
        start_offset = max(scraper_input.offset or 0, 0)
        start_page = start_offset // _PER_PAGE + 1  # HC `page` is 1-indexed
        first_page_drop = start_offset % _PER_PAGE

        sess = cc_requests.Session(impersonate="safari17_2_ios")

        jobs: list[JobPost] = []
        seen_ids: set[str] = set()

        for page_num in range(start_page, start_page + _MAX_PAGES):
            state = self._build_state(scraper_input, page_num)
            json_data = self._fetch_page(sess, state, page_num)
            if json_data is None:
                break

            page_jobs, has_more = self._parse_json(json_data, scraper_input, seen_ids)
            if page_num == start_page and first_page_drop:
                page_jobs = page_jobs[first_page_drop:]
            jobs.extend(page_jobs)
            log.info(
                f"hiring_cafe: page {page_num} → {len(page_jobs)} new "
                f"(total {len(jobs)}, has_more={has_more})"
            )

            if len(jobs) >= wanted:
                jobs = jobs[:wanted]
                break
            if not has_more or len(page_jobs) == 0:
                break

        log.info(f"hiring_cafe: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)

    def _fetch_page(self, sess, state: dict[str, Any], page_num: int) -> dict | None:
        """GET /_next/data/{buildId}/index.json — refresh buildId on 404."""
        encoded = quote(json.dumps(state, separators=(",", ":")))
        for attempt in range(2):
            url = f"{_BASE}/_next/data/{_BUILD_ID}/index.json?searchState={encoded}"
            try:
                r = sess.get(
                    url,
                    timeout=_NEXT_DATA_TIMEOUT_S,
                    headers={"x-nextjs-data": "1", "Accept": "*/*"},
                )
            except Exception as e:
                log.warning(f"hiring_cafe: page {page_num} fetch error: {e!r}")
                return None

            if r.status_code == 200:
                try:
                    return r.json()
                except Exception as e:
                    log.warning(f"hiring_cafe: page {page_num} JSON parse error: {e!r}")
                    return None

            if r.status_code == 404 and attempt == 0:
                log.info(f"hiring_cafe: buildId {_BUILD_ID!r} stale, refreshing...")
                if _refresh_build_id():
                    log.info(f"hiring_cafe: buildId refreshed → {_BUILD_ID!r}")
                    continue
                log.warning("hiring_cafe: buildId refresh failed; aborting")
                return None

            log.warning(
                f"hiring_cafe: page {page_num} got HTTP {r.status_code} "
                f"({len(r.text)} bytes); aborting"
            )
            return None
        return None

    def _build_state(self, si: ScraperInput, page: int) -> dict[str, Any]:
        # Hiring Cafe's locations[] filter requires a Google-Places object
        # with lat/lon + address_components. Without geocoding we just
        # append the raw location string to searchQuery — HC's AI search
        # treats it as a bag of words. Bare concat is safer than "in {location}".
        query_parts: list[str] = []
        if si.search_term:
            query_parts.append(si.search_term)
        if si.location:
            query_parts.append(si.location)
        state: dict[str, Any] = {
            "searchQuery": " ".join(query_parts).strip(),
            "sortBy": "default",
            "page": page,
        }

        if si.is_remote:
            state["workplaceTypes"] = ["Remote"]
        else:
            state["workplaceTypes"] = ["Remote", "Hybrid", "Onsite"]

        commitment_map = {
            JobType.FULL_TIME: "Full Time",
            JobType.PART_TIME: "Part Time",
            JobType.CONTRACT: "Contract",
            JobType.INTERNSHIP: "Internship",
            JobType.TEMPORARY: "Temporary",
            JobType.VOLUNTEER: "Volunteer",
        }
        if si.job_type and si.job_type in commitment_map:
            state["commitmentTypes"] = [commitment_map[si.job_type]]
        else:
            state["commitmentTypes"] = [
                "Full Time", "Part Time", "Contract", "Internship",
                "Temporary", "Seasonal", "Volunteer",
            ]

        hours_old = si.hours_old
        if hours_old:
            state["dateFetchedPastNDays"] = max(1, int(hours_old / 24) or 1)
        else:
            state["dateFetchedPastNDays"] = 121

        return state

    def _parse_json(
        self,
        nd_response: dict,
        si: ScraperInput,
        seen_ids: set[str],
    ) -> tuple[list[JobPost], bool]:
        try:
            pp = nd_response.get("pageProps", {}) or {}
        except Exception:
            return [], False

        ssr = pp.get("ssrHits") or pp.get("results") or pp.get("hits") or []
        jobs_out: list[JobPost] = []

        for hit in ssr:
            if not isinstance(hit, dict):
                continue
            post = self._build_post(hit)
            if post is None or post.id in seen_ids:
                continue
            if hit.get("is_expired") is True:
                continue
            seen_ids.add(post.id)
            jobs_out.append(post)

        has_more = len(ssr) >= _PER_PAGE
        return jobs_out, has_more

    def _build_post(self, hit: dict) -> JobPost | None:
        post_id = str(hit.get("id") or "").strip()
        ji = hit.get("job_information") or {}
        v5 = hit.get("v5_processed_job_data") or {}

        title = (
            v5.get("core_job_title")
            or ji.get("title")
            or ji.get("job_title_raw")
            or ""
        ).strip()
        if not post_id or not title:
            return None

        company_name = v5.get("company_name")
        company_website = v5.get("company_website")
        company_url = (
            f"https://{company_website}"
            if company_website and not company_website.startswith("http")
            else company_website
        )

        location = _build_location(v5)
        is_remote = (v5.get("workplace_type") or "").lower() == "remote"

        date_posted = None
        iso = v5.get("estimated_publish_date") or hit.get("estimated_publish_date")
        if iso:
            try:
                date_posted = datetime.fromisoformat(
                    iso.replace("Z", "+00:00")
                ).date()
            except (ValueError, TypeError):
                date_posted = None

        compensation = _build_compensation(v5)
        commitment_list = v5.get("commitment") or []
        job_type_list = _map_commitment(commitment_list)

        apply_url = hit.get("apply_url")
        job_url = apply_url or f"{_BASE}/jobs/{post_id}"

        description = ji.get("description") or v5.get("requirements_summary")

        return JobPost(
            id=post_id,
            title=title,
            company_name=company_name,
            job_url=job_url,
            location=location,
            description=description,
            company_url=company_url,
            job_type=job_type_list,
            compensation=compensation,
            date_posted=date_posted,
            is_remote=is_remote,
            job_level=v5.get("seniority_level"),
            company_industry=v5.get("company_sector_and_industry"),
        )


# ─────────────────────────────────────────────────────────────────────────
# BuildId refresh — fallback only, fires on 404 from cached buildId
# ─────────────────────────────────────────────────────────────────────────


def _refresh_build_id() -> bool:
    """Lazy fallback: launch headless Camoufox ONCE to extract fresh buildId
    from Hiring Cafe's home page (CF-protected; Firefox-based headless is the
    only path that defeats CF — same pattern as ``jobdrop.wellfound``).

    Updates module-level `_BUILD_ID`. Returns True on success.
    Only fires when /_next/data returns 404 (HC deployed new build).
    """
    global _BUILD_ID

    with _BUILD_ID_LOCK:
        # Double-check pattern: another thread may have refreshed already
        try:
            sess = cc_requests.Session(impersonate="safari17_2_ios")
            r = sess.get(
                f"{_BASE}/_next/data/{_BUILD_ID}/index.json",
                timeout=10,
                headers={"x-nextjs-data": "1"},
            )
            if r.status_code == 200:
                return True
        except Exception:
            pass

        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            log.warning(
                "hiring_cafe: camoufox not installed — cannot refresh buildId"
            )
            return False

        async def _extract():
            async with AsyncCamoufox(headless=True, humanize=True) as browser:
                page = await browser.new_page()
                await page.goto(_BASE + "/", wait_until="load", timeout=45_000)
                await page.wait_for_timeout(5_000)
                html = await page.content()
                m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
                return m.group(1) if m else None

        try:
            new_id = _run_async(_extract())
            if new_id and new_id != _BUILD_ID:
                _BUILD_ID = new_id
                return True
            return False
        except Exception as e:
            log.warning(f"hiring_cafe: Camoufox buildId-refresh failed: {e!r}")
            return False


def _run_async(coro):
    """Run an awaitable from sync code, even if a loop is already running."""
    try:
        return asyncio.run(coro)
    except RuntimeError as e:
        if "asyncio.run" not in str(e) and "running event loop" not in str(e):
            raise
        result_box: dict[str, Any] = {}

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


# ─────────────────────────────────────────────────────────────────────────
# Helpers (unchanged from prior version)
# ─────────────────────────────────────────────────────────────────────────


def _build_location(v5: dict) -> Location | None:
    formatted = v5.get("formatted_workplace_location")
    if formatted:
        parts = [p.strip() for p in formatted.split(",") if p.strip()]
        if parts:
            city = parts[0]
            state = parts[1] if len(parts) >= 2 else None
            country = parts[2] if len(parts) >= 3 else None
            return Location(city=city, state=state, country=country)

    cities = v5.get("workplace_cities") or []
    if cities:
        sample = cities[0]
        parts = [p.strip() for p in sample.split(",") if p.strip()]
        if parts:
            city = parts[0]
            state = parts[1] if len(parts) >= 2 else None
            country = parts[2] if len(parts) >= 3 else None
            return Location(city=city, state=state, country=country)
    return None


def _build_compensation(v5: dict) -> Compensation | None:
    transparent = v5.get("is_compensation_transparent")
    if transparent is False:
        return None

    freq = (v5.get("listed_compensation_frequency") or "Yearly").lower()
    interval_map = {
        "yearly": CompensationInterval.YEARLY,
        "monthly": CompensationInterval.MONTHLY,
        "weekly": CompensationInterval.WEEKLY,
        "daily": CompensationInterval.DAILY,
        "hourly": CompensationInterval.HOURLY,
    }
    interval = interval_map.get(freq, CompensationInterval.YEARLY)

    prefix = freq if freq in interval_map else "yearly"
    min_amt = v5.get(f"{prefix}_min_compensation")
    max_amt = v5.get(f"{prefix}_max_compensation")

    if min_amt is None and max_amt is None:
        return None

    currency = v5.get("listed_compensation_currency") or "USD"
    try:
        return Compensation(
            min_amount=float(min_amt) if min_amt is not None else None,
            max_amount=float(max_amt) if max_amt is not None else None,
            currency=currency,
            interval=interval,
        )
    except (ValueError, TypeError):
        return None


_COMMITMENT_TO_JOBTYPE = {
    "full time": JobType.FULL_TIME,
    "fulltime": JobType.FULL_TIME,
    "part time": JobType.PART_TIME,
    "parttime": JobType.PART_TIME,
    "contract": JobType.CONTRACT,
    "contractor": JobType.CONTRACT,
    "internship": JobType.INTERNSHIP,
    "intern": JobType.INTERNSHIP,
    "temporary": JobType.TEMPORARY,
    "temp": JobType.TEMPORARY,
    "seasonal": JobType.TEMPORARY,
    "volunteer": JobType.VOLUNTEER,
}


def _map_commitment(commitment_list: list[str]) -> list[JobType] | None:
    if not commitment_list:
        return None
    out: list[JobType] = []
    for c in commitment_list:
        if not isinstance(c, str):
            continue
        jt = _COMMITMENT_TO_JOBTYPE.get(c.lower().strip())
        if jt and jt not in out:
            out.append(jt)
    return out or None
