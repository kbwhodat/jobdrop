"""Glassdoor scraper — selenium-driverless headless.

## Why this is rewritten

Glassdoor sits behind Cloudflare. The original tls-client implementation
gets challenged on the GraphQL POST endpoint after ~2 fast requests
from a single IP — verified via stress test, 4/12 queries succeeding
across two rounds with quick-fire calls.

Following the pattern that beat Google: drive a real headless browser
and POST the GraphQL request via in-page fetch(), so Cloudflare sees
a legitimate browser session with all the right cookies and TLS
fingerprint.

## End-to-end flow inside scrape()

  1. Launch one headless Chrome (selenium-driverless).
  2. Navigate to a seed page (Glassdoor sets cf_clearance + reads CSRF).
  3. Extract the GraphQL CSRF token from the page HTML; fall back to
     a known-good token if missing.
  4. Resolve location via in-page fetch to /findPopularLocationAjax.htm.
  5. Loop over pages, posting the GraphQL JobSearchResultsQuery via
     in-page fetch to /graph. All requests share the same browser
     context — Cloudflare doesn't ratelimit.
  6. Parse the GraphQL response into JobPost the same way the original
     did (including parse_compensation/parse_location helpers in util.py).

## Limitations vs. the original

  - **No per-job descriptions.** The original made a SEPARATE
    JobDetailQuery POST per job to fetch full description text — that's
    N additional requests per page. We skip this in v1 to keep the
    scrape fast and avoid burning the browser session on tens of extra
    Cloudflare-gated POSTs. Description comes back as None.

The original tls-client implementation is preserved at
`__init__.py.tls-backup` for reference.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote_plus

from jobspy.glassdoor.constant import fallback_token, query_template
from jobspy.glassdoor.util import (
    get_cursor_for_page,
    parse_compensation,
    parse_location,
)
from jobspy.util import create_logger
from jobspy.model import (
    JobPost,
    JobResponse,
    Scraper,
    ScraperInput,
    Site,
)

log = create_logger("Glassdoor")

_SEED_PATH = "/Job/computer-science-jobs.htm"
_NAV_TIMEOUT_S = 30
_RENDER_SLEEP_S = 3.0


class Glassdoor(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.GLASSDOOR, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        self.base_url: str | None = None
        self.country = None
        self.scraper_input: ScraperInput | None = None
        self.jobs_per_page = 30
        self.max_pages = 30
        self.seen_urls: set[str] = set()

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        self.scraper_input.results_wanted = min(900, scraper_input.results_wanted)
        self.base_url = scraper_input.country.get_glassdoor_url().rstrip("/")
        self.seen_urls = set()

        try:
            from selenium_driverless import webdriver  # noqa: F401
        except ImportError:
            log.error(
                "Glassdoor: selenium-driverless required. "
                "Install with: pip install selenium-driverless"
            )
            return JobResponse(jobs=[])

        try:
            jobs = asyncio.run(self._scrape_async())
        except RuntimeError as e:
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                log.info("Glassdoor: running on dedicated thread (caller in event loop)")
                jobs = _run_on_thread(self._scrape_async())
            else:
                raise

        return JobResponse(jobs=jobs)

    async def _scrape_async(self) -> list[JobPost]:
        from selenium_driverless import webdriver

        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--window-size=1280,900")

        async with webdriver.Chrome(options=options) as driver:
            seed_url = f"{self.base_url}{_SEED_PATH}"
            try:
                await driver.get(seed_url, wait_load=True, timeout=_NAV_TIMEOUT_S)
            except Exception as e:
                log.error(f"Glassdoor: failed to load seed page: {e}")
                return []
            await asyncio.sleep(_RENDER_SLEEP_S)

            cur = await driver.current_url
            if "/sorry/" in cur or "challenge" in cur.lower():
                log.error(f"Glassdoor: blocked at seed page: {cur[:120]}")
                return []

            token = await self._get_token(driver)
            log.info(f"Glassdoor: token={token[:20]}...")

            location_id, location_type = await self._resolve_location(
                driver, self.scraper_input.location, self.scraper_input.is_remote
            )
            if location_type is None:
                log.error("Glassdoor: location not parsed")
                return []

            jobs: list[JobPost] = []
            cursor: str | None = None
            range_start = 1 + (self.scraper_input.offset // self.jobs_per_page)
            tot_pages = (self.scraper_input.results_wanted // self.jobs_per_page) + 2
            range_end = min(tot_pages, self.max_pages + 1)

            for page in range(range_start, range_end):
                log.info(f"Glassdoor: fetching page {page}/{range_end - 1}")
                page_jobs, cursor, partial_errors = await self._fetch_page(
                    driver, token, int(location_id), location_type, page, cursor
                )
                if partial_errors:
                    log.info(f"Glassdoor: partial errors {partial_errors[:3]}")
                jobs.extend(page_jobs)
                if not page_jobs or len(jobs) >= self.scraper_input.results_wanted:
                    break

            return jobs[: self.scraper_input.results_wanted]

    async def _get_token(self, driver) -> str:
        res = await driver.eval_async(
            """
            const html = document.documentElement.outerHTML;
            const m = html.match(/"token":\\s*"([^"]+)"/);
            return { token: m ? m[1] : null };
            """,
            serialization="json",
        )
        return (res.get("token") if isinstance(res, dict) else None) or fallback_token

    async def _resolve_location(
        self, driver, location: str | None, is_remote: bool
    ) -> tuple[int | str | None, str | None]:
        if not location or is_remote:
            return "11047", "STATE"  # remote
        res = await driver.eval_async(
            f"""
            const r = await fetch(
                '/findPopularLocationAjax.htm?maxLocationsToReturn=10&term={quote_plus(location)}',
                {{ credentials: 'include' }}
            );
            return {{ status: r.status, body: r.status === 200 ? await r.text() : '' }};
            """,
            serialization="json",
        )
        if res.get("status") != 200:
            log.error(f"Glassdoor: location lookup status {res.get('status')}")
            return None, None
        try:
            items = json.loads(res["body"])
        except Exception as e:
            log.error(f"Glassdoor: location response not JSON: {e}")
            return None, None
        if not items:
            log.error(f"Glassdoor: location '{location}' not found")
            return None, None
        item = items[0]
        loc_type_code = item.get("locationType")
        loc_type = {"C": "CITY", "S": "STATE", "N": "COUNTRY"}.get(loc_type_code)
        if not loc_type:
            log.error(f"Glassdoor: unknown locationType {loc_type_code!r}")
            return None, None
        return int(item["locationId"]), loc_type

    async def _fetch_page(
        self,
        driver,
        token: str,
        location_id: int,
        location_type: str,
        page_num: int,
        cursor: str | None,
    ) -> tuple[list[JobPost], str | None, list[str]]:
        payload = self._build_payload(location_id, location_type, page_num, cursor)
        res = await driver.eval_async(
            f"""
            const r = await fetch('/graph', {{
                method: 'POST',
                credentials: 'include',
                headers: {{
                    'content-type': 'application/json',
                    'gd-csrf-token': {json.dumps(token)},
                    'apollographql-client-name': 'job-search-next',
                }},
                body: {json.dumps(payload)}
            }});
            return {{ status: r.status, body: r.status === 200 ? await r.text() : '' }};
            """,
            serialization="json",
        )
        if res.get("status") != 200:
            log.error(f"Glassdoor: GraphQL status {res.get('status')}")
            return [], None, []
        try:
            data = json.loads(res["body"])
        except Exception as e:
            log.error(f"Glassdoor: GraphQL not JSON: {e}")
            return [], None, []
        first = data[0] if isinstance(data, list) and data else {}
        partial_errors: list[str] = [
            ".".join(map(str, e.get("path") or [])) or "?"
            for e in (first.get("errors") or [])
        ]
        listings = (first.get("data") or {}).get("jobListings") or {}
        raw_jobs = listings.get("jobListings") or []
        next_cursor = get_cursor_for_page(listings.get("paginationCursors") or [], page_num + 1)

        jobs: list[JobPost] = []
        for raw in raw_jobs:
            post = self._raw_to_jobpost(raw)
            if post:
                jobs.append(post)
        return jobs, next_cursor, partial_errors

    def _raw_to_jobpost(self, job_data: dict) -> JobPost | None:
        try:
            jv = job_data["jobview"]
            header = jv.get("header") or {}
            job = jv.get("job") or {}
            job_id = job.get("listingId")
            if not job_id:
                return None
            job_url = f"{self.base_url}/job-listing/j?jl={job_id}"
            if job_url in self.seen_urls:
                return None
            self.seen_urls.add(job_url)
            title = job.get("jobTitleText")
            if not title:
                return None
            company_name = header.get("employerNameFromSearch")
            employer = header.get("employer") or {}
            company_id = employer.get("id")
            location_name = header.get("locationName") or ""
            location_type = header.get("locationType")
            age_in_days = header.get("ageInDays")

            is_remote = location_type == "S"
            location = parse_location(location_name) if not is_remote else None

            date_posted = None
            if age_in_days is not None:
                date_posted = (datetime.now() - timedelta(days=age_in_days)).date()

            compensation = parse_compensation(header)
            company_url = (
                f"{self.base_url}/Overview/W-EI_IE{company_id}.htm"
                if company_id else None
            )

            return JobPost(
                id=f"gd-{job_id}",
                title=title,
                company_url=company_url,
                company_name=company_name,
                date_posted=date_posted,
                job_url=job_url,
                location=location,
                compensation=compensation,
                is_remote=is_remote,
                description=None,
            )
        except Exception as e:
            log.warning(f"Glassdoor: failed to parse job: {e}")
            return None

    def _build_payload(
        self,
        location_id: int,
        location_type: str,
        page_num: int,
        cursor: str | None,
    ) -> str:
        fromage = None
        if getattr(self.scraper_input, "hours_old", None):
            fromage = max(self.scraper_input.hours_old // 24, 1)
        filter_params: list[dict] = []
        if self.scraper_input.easy_apply:
            filter_params.append({"filterKey": "applicationType", "values": "1"})
        if fromage:
            filter_params.append({"filterKey": "fromAge", "values": str(fromage)})
        if self.scraper_input.job_type:
            filter_params.append(
                {"filterKey": "jobType", "values": self.scraper_input.job_type.value[0]}
            )
        payload = {
            "operationName": "JobSearchResultsQuery",
            "variables": {
                "excludeJobListingIds": [],
                "filterParams": filter_params,
                "keyword": self.scraper_input.search_term,
                "numJobsToShow": self.jobs_per_page,
                "locationType": location_type,
                "locationId": int(location_id),
                "parameterUrlInput": f"IL.0,12_I{location_type}{location_id}",
                "pageNumber": page_num,
                "pageCursor": cursor,
                "fromage": fromage,
                "sort": "date",
            },
            "query": query_template,
        }
        return json.dumps([payload])


def _run_on_thread(coro):
    """Run an awaitable from sync code already inside a running event loop.
    Uses a dedicated thread + new loop. Mirrors the pattern in jobspy/google."""
    import threading

    box: dict[str, Any] = {}

    def runner():
        try:
            box["ok"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "err" in box:
        raise box["err"]
    return box.get("ok")
