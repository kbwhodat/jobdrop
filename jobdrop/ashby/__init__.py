"""Ashby scraper — Google-dorked discovery + public GraphQL API enrichment.

Ashby hosts a separate job board per company (anthropic, ramp, linear,
notion, openai, etc.) at:
  https://jobs.ashbyhq.com/{org_slug}

There is no global Ashby-wide search. To find jobs across ALL Ashby-
hosted boards, we issue a Google ``site:jobs.ashbyhq.com`` query for
the caller's keywords, harvest org slugs from the SERP, then fan out
to Ashby's public GraphQL endpoint for clean structured data.

## Stage 1: Google discovery

Query template:
  ``site:jobs.ashbyhq.com "<keywords>" "<location>"``

We use ``zendriver`` (CDP-direct, no WebDriver fingerprint) to drive a
headless Chrome instance — same pattern as ``jobdrop.greenhouse``.

## Stage 2: API enrichment

For each discovered org slug, POST to
  ``https://jobs.ashbyhq.com/api/non-user-graphql``
which returns the full job board's postings as JSON.
"""
from __future__ import annotations

import asyncio
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus

from curl_cffi import requests as cc_requests

from jobdrop.ashby.util import log
from jobdrop.model import (
    JobPost,
    JobResponse,
    JobType,
    Location,
    Scraper,
    ScraperInput,
    Site,
)

_ASHBY_HOST = "jobs.ashbyhq.com"
_GRAPHQL_URL = f"https://{_ASHBY_HOST}/api/non-user-graphql"
_BOARD_URL = "https://" + _ASHBY_HOST + "/{org}/{job_id}"
_GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&start={start}"

# Capture the org slug from any jobs.ashbyhq.com URL, ignoring query strings
# and URL-encoded SERP redirect garbage.
_ASHBY_URL_RE = re.compile(
    r"https?://jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)(?:[/?#]|$)"
)
_SLUG_BLOCKLIST = {"www", "api", "static", "career", "careers"}

_NAV_TIMEOUT_S = 30
_RENDER_SLEEP_S = 3.0
_API_TIMEOUT_S = 15
_API_WORKERS = 12

_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
  ) {
    jobPostings {
      id
      title
      teamId
      locationId
      locationName
      employmentType
      secondaryLocations { locationId locationName }
    }
  }
}
"""

_EMPLOYMENT_TYPE_MAP = {
    "FullTime": JobType.FULL_TIME,
    "PartTime": JobType.PART_TIME,
    "Contract": JobType.CONTRACT,
    "Intern": JobType.INTERNSHIP,
    "Temporary": JobType.TEMPORARY,
}


class Ashby(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.ASHBY, proxies=proxies, ca_cert=ca_cert)
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
                "Ashby: zendriver is required for Google discovery. "
                "Install with: pip install zendriver"
            )
            return JobResponse(jobs=[])

        query = _build_query(scraper_input)
        log.info(f"Ashby: Google query = {query!r}")

        # Stage 1: dork Google for ashby boards matching the query
        try:
            org_slugs = _run_async(_discover_orgs(query, wanted))
        except RuntimeError as e:
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                org_slugs = _run_on_thread(_discover_orgs(query, wanted))
            else:
                raise
        log.info(f"Ashby: discovered {len(org_slugs)} orgs from SERP")
        if not org_slugs:
            return JobResponse(jobs=[])

        # Stage 2: parallel API fetch per org
        sess = cc_requests.Session(impersonate="safari17_2_ios")
        all_postings: list[tuple[str, dict]] = []
        with ThreadPoolExecutor(max_workers=_API_WORKERS) as ex:
            futures = {ex.submit(_fetch_board, sess, org): org for org in org_slugs}
            for fut in as_completed(futures):
                org = futures[fut]
                try:
                    postings = fut.result()
                except Exception as e:
                    log.debug(f"Ashby: {org} board fetch failed: {e!r}")
                    continue
                for p in postings:
                    all_postings.append((org, p))
        log.info(
            f"Ashby: API enrichment hit {len(all_postings)} postings "
            f"across {len(org_slugs)} orgs"
        )

        # Stage 3: client-side filter — title-substring only (location/team
        # text matches give too many false positives like "Atlanta Engineering
        # Office" matching a "engineer" search). Then location + remote.
        title_token = (scraper_input.search_term or "").lower().strip()
        location_filter = (scraper_input.location or "").lower().strip()
        is_remote = bool(scraper_input.is_remote)

        filtered: list[tuple[str, dict]] = []
        for org, p in all_postings:
            title = (p.get("title") or "").lower()
            loc = (p.get("locationName") or "").lower()
            sec_locs = " ".join(
                (sl.get("locationName") or "").lower()
                for sl in (p.get("secondaryLocations") or [])
            )

            if title_token and title_token not in title:
                continue
            if location_filter:
                if (
                    location_filter not in loc
                    and location_filter not in sec_locs
                    and not (("remote" in loc or "remote" in sec_locs) and is_remote)
                ):
                    continue
            if is_remote and "remote" not in loc and "remote" not in sec_locs:
                continue
            filtered.append((org, p))

        log.info(f"Ashby: {len(filtered)} postings match filters")

        # Stage 4: dedup by id, then paginate
        seen_ids: set[str] = set()
        jobs: list[JobPost] = []
        for org, p in filtered:
            post = _build_jobpost(org, p)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)
        jobs = jobs[start_offset : start_offset + wanted]

        log.info(f"Ashby: returning {len(jobs)} jobs (offset={start_offset})")
        return JobResponse(jobs=jobs)


# ─────────────────────────────────────────────────────────────────────────
# Stage 1 — Google discovery (zendriver)
# ─────────────────────────────────────────────────────────────────────────


def _build_query(si: ScraperInput) -> str:
    parts: list[str] = [f"site:{_ASHBY_HOST}"]
    if si.search_term:
        parts.append(f'"{si.search_term}"')
    if si.location:
        city = si.location.split(",")[0].strip()
        if city:
            parts.append(f'"{city}"')
    if si.is_remote:
        parts.append('"remote"')
    return " ".join(parts)


async def _discover_orgs(query: str, wanted: int) -> list[str]:
    """Walk Google SERPs via zendriver, extract unique Ashby org slugs."""
    import zendriver as zd
    encoded = quote_plus(query)
    seen: set[str] = set()
    ordered: list[str] = []
    browser = await zd.start(
        headless=True, sandbox=False, browser_args=["--window-size=1280,900"],
    )
    try:
        for page_idx in range(5):
            url = _GOOGLE_SEARCH_URL.format(query=encoded, start=page_idx * 10)
            log.info(f"Ashby: SERP page {page_idx + 1} → {url[:120]}")
            try:
                tab = await browser.get(url)
            except Exception as e:
                log.error(f"Ashby: SERP fetch failed on page {page_idx + 1}: {e}")
                break
            await asyncio.sleep(_RENDER_SLEEP_S)
            try:
                current = await tab.evaluate("location.href")
            except Exception:
                current = url
            if "/sorry/" in str(current):
                log.error(
                    f"Ashby: hit Google /sorry/ on page {page_idx + 1}. "
                    "Returning what we have."
                )
                break
            try:
                html = await tab.get_content()
            except Exception:
                html = await tab.evaluate("document.documentElement.outerHTML") or ""

            new_count = 0
            for m in _ASHBY_URL_RE.finditer(html):
                slug = m.group(1).lower().strip()
                if slug in _SLUG_BLOCKLIST or len(slug) > 50:
                    continue
                if slug not in seen:
                    seen.add(slug)
                    ordered.append(slug)
                    new_count += 1

            log.info(
                f"Ashby: page {page_idx + 1} added {new_count} orgs "
                f"(total {len(ordered)} / wanted {wanted})"
            )
            if len(ordered) >= max(wanted, 30):
                break
            if new_count == 0:
                break
    finally:
        await browser.stop()
    return ordered


# ─────────────────────────────────────────────────────────────────────────
# Stage 2 — Ashby GraphQL enrichment
# ─────────────────────────────────────────────────────────────────────────


def _fetch_board(sess: cc_requests.Session, org: str) -> list[dict]:
    body = {
        "operationName": "ApiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": org},
        "query": _QUERY,
    }
    r = sess.post(
        _GRAPHQL_URL,
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=_API_TIMEOUT_S,
    )
    if not r.ok:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    jb = (data.get("data") or {}).get("jobBoard")
    if not jb:
        return []
    return jb.get("jobPostings") or []


# ─────────────────────────────────────────────────────────────────────────
# Build JobPost
# ─────────────────────────────────────────────────────────────────────────


def _build_jobpost(org: str, raw: dict) -> JobPost | None:
    pid = raw.get("id")
    title = (raw.get("title") or "").strip()
    if not pid or not title:
        return None

    location = _build_location(raw.get("locationName"))
    is_remote = "remote" in (raw.get("locationName") or "").lower()

    job_type = _EMPLOYMENT_TYPE_MAP.get(raw.get("employmentType"))
    job_type_list = [job_type] if job_type else None

    return JobPost(
        id=f"ashby-{org}-{pid}",
        title=title,
        company_name=_humanize_org(org),
        job_url=_BOARD_URL.format(org=org, job_id=pid),
        location=location,
        is_remote=is_remote,
        job_type=job_type_list,
    )


def _build_location(loc_name: str | None) -> Location | None:
    if not loc_name:
        return None
    parts = [p.strip() for p in loc_name.split(",") if p.strip()]
    if not parts:
        return None
    city = parts[0] if "remote" not in parts[0].lower() else None
    state = parts[1] if len(parts) >= 2 else None
    country = parts[2] if len(parts) >= 3 else None
    if not (city or state or country):
        return None
    return Location(city=city, state=state, country=country)


def _humanize_org(org: str) -> str:
    return org.replace("-", " ").replace("_", " ").title()


# ─────────────────────────────────────────────────────────────────────────
# Async runner helpers (mirrors greenhouse pattern)
# ─────────────────────────────────────────────────────────────────────────


def _run_async(coro):
    return asyncio.run(coro)


def _run_on_thread(coro):
    """Run coro in a fresh asyncio loop on a dedicated thread.
    Used when the caller is already inside a running event loop."""
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
