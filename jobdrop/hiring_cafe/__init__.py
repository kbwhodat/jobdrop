"""Hiring Cafe scraper — AI-enriched job listings via searchState URL filter.

## Why selenium-driverless headed Chrome

Hiring Cafe is fronted by Cloudflare with a "Just a moment..." JS challenge
that blocks curl_cffi (all 26 TLS profiles), Camoufox headless, and any
non-browser HTTP. Real headed Chrome via selenium-driverless passes the
challenge and gets a valid `cf_clearance` cookie. Same engine we use for
Glassdoor, Google, and Greenhouse — no new dep.

## URL filter — `?searchState=<URL-encoded JSON>`

Empirically discovered (May 2026): hitting
  https://hiring.cafe/?searchState={...}
returns SSR-rendered ssrHits filtered by the JSON spec. Fields we set:
  - searchQuery        ← keyword
  - locations[]        ← structured Google-Places-shaped object
  - workplaceTypes     ← ["Remote"] / ["Onsite", "Hybrid"] / all
  - commitmentTypes    ← ["Full Time"] etc., maps from JobType
  - dateFetchedPastNDays ← from hours_old / 24
  - sortBy             ← "default"
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import datetime
from typing import Any
from urllib.parse import quote

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
_NAV_TIMEOUT_S = 30
_RENDER_SLEEP_S = 8.0
_PER_PAGE = 120
_MAX_PAGES = 5
_CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


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

        try:
            from selenium_driverless import webdriver  # noqa: F401
        except ImportError:
            log.error(
                "hiring_cafe: selenium-driverless required. "
                "Install: pip install selenium-driverless"
            )
            return JobResponse(jobs=[])

        try:
            jobs = asyncio.run(self._scrape_async(scraper_input))
        except RuntimeError as e:
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                log.info("hiring_cafe: nested loop detected; running on dedicated thread")
                jobs = _run_on_thread(self._scrape_async(scraper_input))
            else:
                raise

        log.info(f"hiring_cafe: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)

    async def _scrape_async(self, si: ScraperInput) -> list[JobPost]:
        from selenium_driverless import webdriver

        wanted = max(si.results_wanted or 15, 1)

        options = webdriver.ChromeOptions()
        options.binary_location = _CHROME_BIN
        options.add_argument("--no-sandbox")
        options.add_argument("--window-size=1280,900")
        options.add_argument("--window-position=-2400,-2400")

        jobs: list[JobPost] = []
        seen_ids: set[str] = set()

        async with webdriver.Chrome(options=options) as driver:
            try:
                await driver.get(_BASE + "/", wait_load=True, timeout=_NAV_TIMEOUT_S)
                await asyncio.sleep(5)
            except Exception as e:
                log.warning(f"hiring_cafe: warm / failed: {e!r}")

            for page_num in range(1, _MAX_PAGES + 1):
                state = self._build_state(si, page_num)
                encoded = quote(json.dumps(state, separators=(",", ":")))
                url = f"{_BASE}/?searchState={encoded}"

                try:
                    await driver.get(url, wait_load=True, timeout=_NAV_TIMEOUT_S)
                    await asyncio.sleep(_RENDER_SLEEP_S)
                    html = await driver.page_source
                except Exception as e:
                    log.warning(f"hiring_cafe: page {page_num} fetch failed: {e!r}")
                    break

                if "just a moment" in html.lower():
                    log.warning("hiring_cafe: Cloudflare challenge unresolved")
                    break

                page_jobs, has_more = self._parse_html(html, si, seen_ids)
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

        return jobs

    def _build_state(self, si: ScraperInput, page: int) -> dict[str, Any]:
        # Hiring Cafe's locations[] filter requires a Google-Places object with
        # lat/lon + address_components. Without geocoding we just append the
        # raw location string to searchQuery — HC's AI search treats it as a
        # bag-of-words. The "in {location}" syntax breaks AI search; bare
        # concatenation is safer (just words to match).
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

    def _parse_html(
        self,
        html: str,
        si: ScraperInput,
        seen_ids: set[str],
    ) -> tuple[list[JobPost], bool]:
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
        )
        if not m:
            log.warning("hiring_cafe: __NEXT_DATA__ missing — page shape changed")
            return [], False

        try:
            nd = json.loads(m.group(1))
        except Exception as e:
            log.warning(f"hiring_cafe: __NEXT_DATA__ parse error: {e!r}")
            return [], False

        try:
            pp = nd["props"]["pageProps"]
        except (KeyError, TypeError):
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
# Helpers
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
