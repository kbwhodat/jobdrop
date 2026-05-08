"""Lever scraper — Google-dorked discovery + public REST API enrichment.

Lever hosts a separate job board per company (plaid, hashicorp, kraken,
etc.) at:
  https://jobs.lever.co/{slug}

There is no global cross-company search. We dork Google for
``site:jobs.lever.co`` matching the caller's keywords, harvest org
slugs from the SERP, then call Lever's public REST API per org.

## Stage 1: Google discovery (zendriver)

  Query template: ``site:jobs.lever.co "<keywords>" "<location>"``

## Stage 2: Lever REST enrichment

  GET https://api.lever.co/v0/postings/{slug}?mode=json
  → array of postings with fields:
      id, text (title), createdAt (epoch ms), hostedUrl,
      categories: {location, team, commitment, department}
"""
from __future__ import annotations

import asyncio
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote_plus

from curl_cffi import requests as cc_requests

from jobdrop.lever.util import log
from jobdrop.model import (
    JobPost,
    JobResponse,
    JobType,
    Location,
    Scraper,
    ScraperInput,
    Site,
)

_LEVER_HOST = "jobs.lever.co"
_API = "https://api.lever.co/v0/postings/{slug}?mode=json"
_GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&start={start}"

_LEVER_URL_RE = re.compile(
    r"https?://jobs\.lever\.co/([a-zA-Z0-9_-]+)(?:[/?#]|$)"
)
_SLUG_BLOCKLIST = {"www", "api", "static", "career", "careers"}

_RENDER_SLEEP_S = 3.0
_API_TIMEOUT_S = 15
_API_WORKERS = 12

_COMMITMENT_MAP = {
    "Full-time": JobType.FULL_TIME,
    "Part-time": JobType.PART_TIME,
    "Contract": JobType.CONTRACT,
    "Internship": JobType.INTERNSHIP,
    "Temporary": JobType.TEMPORARY,
}


class Lever(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.LEVER, proxies=proxies, ca_cert=ca_cert)
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
                "Lever: zendriver is required for Google discovery. "
                "Install with: pip install zendriver"
            )
            return JobResponse(jobs=[])

        query = _build_query(scraper_input)
        log.info(f"Lever: Google query = {query!r}")

        # Stage 1: discover org slugs from SERP
        try:
            slugs = _run_async(_discover_orgs(query, wanted))
        except RuntimeError as e:
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                slugs = _run_on_thread(_discover_orgs(query, wanted))
            else:
                raise
        log.info(f"Lever: discovered {len(slugs)} orgs from SERP")
        if not slugs:
            return JobResponse(jobs=[])

        # Stage 2: parallel REST fetch per org
        sess = cc_requests.Session(impersonate="safari17_2_ios")
        all_postings: list[tuple[str, dict]] = []
        with ThreadPoolExecutor(max_workers=_API_WORKERS) as ex:
            futures = {ex.submit(_fetch_board, sess, s): s for s in slugs}
            for fut in as_completed(futures):
                slug = futures[fut]
                try:
                    postings = fut.result()
                except Exception as e:
                    log.debug(f"Lever: {slug} fetch failed: {e!r}")
                    continue
                for p in postings:
                    all_postings.append((slug, p))
        log.info(f"Lever: API enrichment hit {len(all_postings)} postings across {len(slugs)} orgs")

        # Stage 3: client-side filter — title-substring only (team/location
        # text matches gave false positives like "engineer" matching
        # "Engineering" team for a Service Delivery Manager role).
        title_token = (scraper_input.search_term or "").lower().strip()
        location_filter = (scraper_input.location or "").lower().strip()
        is_remote = bool(scraper_input.is_remote)

        filtered: list[tuple[str, dict]] = []
        for slug, p in all_postings:
            title = (p.get("text") or "").lower()
            cats = p.get("categories") or {}
            loc = (cats.get("location") or "").lower()
            if title_token and title_token not in title:
                continue
            if location_filter and location_filter not in loc:
                if not (("remote" in loc) and is_remote):
                    continue
            if is_remote and "remote" not in loc:
                continue
            filtered.append((slug, p))

        log.info(f"Lever: {len(filtered)} match filters")

        # Stage 4: dedup by id, then paginate
        seen_ids: set[str] = set()
        jobs: list[JobPost] = []
        for slug, p in filtered:
            post = _build_jobpost(slug, p)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)
        jobs = jobs[start_offset : start_offset + wanted]

        log.info(f"Lever: returning {len(jobs)} jobs (offset={start_offset})")
        return JobResponse(jobs=jobs)


# ─────────────────────────────────────────────────────────────────────────
# Stage 1 — Google discovery
# ─────────────────────────────────────────────────────────────────────────


def _build_query(si: ScraperInput) -> str:
    parts: list[str] = [f"site:{_LEVER_HOST}"]
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
            log.info(f"Lever: SERP page {page_idx + 1} → {url[:120]}")
            try:
                tab = await browser.get(url)
            except Exception as e:
                log.error(f"Lever: SERP fetch failed on page {page_idx + 1}: {e}")
                break
            await asyncio.sleep(_RENDER_SLEEP_S)
            try:
                current = await tab.evaluate("location.href")
            except Exception:
                current = url
            if "/sorry/" in str(current):
                log.error(
                    f"Lever: hit Google /sorry/ on page {page_idx + 1}. "
                    "Returning what we have."
                )
                break
            try:
                html = await tab.get_content()
            except Exception:
                html = await tab.evaluate("document.documentElement.outerHTML") or ""

            new_count = 0
            for m in _LEVER_URL_RE.finditer(html):
                slug = m.group(1).lower().strip()
                if slug in _SLUG_BLOCKLIST or len(slug) > 50:
                    continue
                if slug not in seen:
                    seen.add(slug)
                    ordered.append(slug)
                    new_count += 1

            log.info(
                f"Lever: page {page_idx + 1} added {new_count} orgs "
                f"(total {len(ordered)} / wanted {wanted})"
            )
            if len(ordered) >= max(wanted, 20):
                break
            if new_count == 0:
                break
    finally:
        await browser.stop()
    return ordered


# ─────────────────────────────────────────────────────────────────────────
# Stage 2 — Lever REST enrichment
# ─────────────────────────────────────────────────────────────────────────


def _fetch_board(sess: cc_requests.Session, slug: str) -> list[dict]:
    try:
        r = sess.get(_API.format(slug=slug), timeout=_API_TIMEOUT_S)
        if not r.ok:
            return []
        return r.json() or []
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────
# Build JobPost
# ─────────────────────────────────────────────────────────────────────────


def _build_jobpost(slug: str, raw: dict) -> JobPost | None:
    pid = raw.get("id")
    title = (raw.get("text") or "").strip()
    if not pid or not title:
        return None
    cats = raw.get("categories") or {}
    loc_text = cats.get("location") or ""
    is_remote = "remote" in loc_text.lower()

    job_type = _COMMITMENT_MAP.get(cats.get("commitment"))
    job_type_list = [job_type] if job_type else None

    posted_dt = None
    created_ms = raw.get("createdAt")
    if isinstance(created_ms, (int, float)):
        try:
            posted_dt = datetime.fromtimestamp(created_ms / 1000).date()
        except (ValueError, OSError, OverflowError):
            posted_dt = None

    return JobPost(
        id=f"lever-{slug}-{pid}",
        title=title,
        company_name=_humanize_slug(slug),
        job_url=raw.get("hostedUrl") or f"https://jobs.lever.co/{slug}/{pid}",
        location=_build_location(loc_text),
        is_remote=is_remote,
        job_type=job_type_list,
        date_posted=posted_dt,
        company_industry=cats.get("department") or cats.get("team"),
    )


def _build_location(loc_text: str) -> Location | None:
    if not loc_text:
        return None
    parts = [p.strip() for p in loc_text.split(",") if p.strip()]
    if not parts:
        return None
    city = parts[0] if "remote" not in parts[0].lower() else None
    state = parts[1] if len(parts) >= 2 else None
    country = parts[2] if len(parts) >= 3 else None
    if not (city or state or country):
        return None
    return Location(city=city, state=state, country=country)


def _humanize_slug(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


# ─────────────────────────────────────────────────────────────────────────
# Async runner helpers
# ─────────────────────────────────────────────────────────────────────────


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
