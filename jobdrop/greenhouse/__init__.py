"""Greenhouse scraper — Google-dorked discovery + public API enrichment.

Greenhouse hosts a separate job board per company (anthropic, anduril,
stripe, etc.) at:
  https://boards.greenhouse.io/<board>/jobs/<id>           (legacy URL)
  https://job-boards.greenhouse.io/<board>/jobs/<id>       (current URL)

There is no global Greenhouse-wide search. To search across ALL
greenhouse-hosted boards, we issue a Google ``site:`` query, harvest
posting URLs from the SERP, then fan out to Greenhouse's public board
API for clean structured data.

## Stage 1: Google discovery

Query template:
  ``site:job-boards.greenhouse.io OR site:boards.greenhouse.io "<keywords>" "<location>"``

We use ``selenium-driverless`` to drive a headless Chrome instance and
defeat Google's anti-bot wall — same pattern as ``jobdrop.google``.
Pagination via ``&start=N`` (10 results/page).

## Stage 2: API enrichment

For each ``(board_token, job_id)`` extracted from the SERP, GET
  ``https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}?content=true``
which returns clean JSON:
  - ``title``, ``company_name``
  - ``location.name`` (free-form: "London, UK; San Francisco, CA")
  - ``offices`` — structured per-location entries
  - ``departments`` — surfaced as company_industry
  - ``metadata`` — includes "Location Type: On-Site"/"Remote"/"Hybrid"
  - ``content`` — full job description (HTML)
  - ``updated_at`` / ``first_published`` — ISO 8601

API calls run in parallel via ThreadPoolExecutor.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from jobdrop.model import (
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

log = create_logger("Greenhouse")

# Match both URL hosts and capture (board_token, job_id) in a single regex.
_GH_URL_RE = re.compile(
    r"https?://(?:job-)?boards\.greenhouse\.io/([^/\s\"'>?#]+)/jobs/(\d+)"
)
_GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&start={start}"
_API_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}?content=true"

_NAV_TIMEOUT_S = 30
_RENDER_SLEEP_S = 3.0
_API_TIMEOUT_S = 20
_API_WORKERS = 8

# Default freshness ceiling when caller doesn't pass hours_old. Greenhouse
# search results pulled via Google can include postings 1.5+ years old —
# Google indexes them and the company hasn't taken them down yet, so the
# API still returns 200. Cap at 90 days as a sane default; users can pass
# any explicit ``hours_old`` to override (smaller = stricter, 0/None +
# pass-through wins everything).
_DEFAULT_MAX_AGE_DAYS = 90


class Greenhouse(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.GREENHOUSE, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted

        try:
            from selenium_driverless import webdriver  # noqa: F401
        except ImportError:
            log.error(
                "Greenhouse: selenium-driverless is required for Google "
                "discovery. Install with: pip install selenium-driverless"
            )
            return JobResponse(jobs=[])

        query = _build_query(scraper_input)
        log.info(f"Greenhouse: Google query = {query!r}")

        # Stage 1: collect greenhouse URLs from Google. Walk extra SERP
        # pages until we have >= wanted unique URLs (or run out).
        try:
            postings = _run_async(_discover_via_google(query, wanted))
        except RuntimeError as e:
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                postings = _run_on_thread(_discover_via_google(query, wanted))
            else:
                raise
        log.info(f"Greenhouse: discovered {len(postings)} unique postings")

        if not postings:
            return JobResponse(jobs=[])

        # Stage 2: parallel API fetches for clean structured data. We
        # over-fetch beyond `wanted` because the freshness filter below
        # may drop a chunk of stale results.
        fetch_target = min(len(postings), max(wanted * 2, wanted + 10))
        details = _fetch_details_bulk(postings[:fetch_target])

        # Resolve freshness cutoff. Caller's hours_old wins. Default to
        # 90 days for Greenhouse since Google indexes very old postings.
        cutoff = _resolve_cutoff(scraper_input)
        now = datetime.now(timezone.utc)
        log.info(
            f"Greenhouse: freshness cutoff = {cutoff.isoformat()} "
            f"({(now - cutoff).days}d back)"
        )

        jobs: list[JobPost] = []
        dropped_old = 0
        dropped_deadline = 0
        for (board, jid) in postings[:fetch_target]:
            data = details.get((board, jid))
            if data is None:
                continue  # 404 — job was removed/filled, silent skip

            # Drop postings whose application_deadline has already passed.
            # Rare but unambiguous.
            dl = _parse_iso_dt(data.get("application_deadline"))
            if dl is not None and dl < now:
                dropped_deadline += 1
                continue

            # Drop postings older than the freshness cutoff. We use
            # first_published, falling back to updated_at if missing.
            posted_dt = (
                _parse_iso_dt(data.get("first_published"))
                or _parse_iso_dt(data.get("updated_at"))
            )
            if posted_dt is not None and posted_dt < cutoff:
                dropped_old += 1
                continue

            post = _build_jobpost(data, board, scraper_input.country)
            if post is not None:
                jobs.append(post)
            if len(jobs) >= wanted:
                break

        if dropped_old or dropped_deadline:
            log.info(
                f"Greenhouse: filtered {dropped_old} stale (>cutoff) + "
                f"{dropped_deadline} past-deadline postings"
            )
        log.info(f"Greenhouse: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


def _resolve_cutoff(si: ScraperInput) -> datetime:
    """Return the UTC datetime below which postings are considered stale."""
    hours = getattr(si, "hours_old", None)
    if hours and hours > 0:
        return datetime.now(timezone.utc) - timedelta(hours=hours)
    return datetime.now(timezone.utc) - timedelta(days=_DEFAULT_MAX_AGE_DAYS)


# -----------------------------------------------------------------------------
# Stage 1 — Google discovery
# -----------------------------------------------------------------------------


def _build_query(si: ScraperInput) -> str:
    """Compose a Google ``site:`` dork from ScraperInput.

    We OR the two known greenhouse hostnames so we capture both legacy
    ``boards.greenhouse.io`` and current ``job-boards.greenhouse.io`` URLs
    in a single query.

    Location handling — important: quoting ``"Atlanta, GA"`` with the
    comma+state forces Google to look for that exact phrase, which most
    greenhouse pages don't contain (they spell it "Atlanta, Georgia"
    or "Atlanta" alone). We quote only the *city* and let the state
    appear as an unquoted bare token, which Google treats as a
    softer hint instead of a hard match.
    """
    parts: list[str] = ["site:job-boards.greenhouse.io OR site:boards.greenhouse.io"]
    if si.search_term:
        parts.append(f'"{si.search_term}"')
    if si.location:
        city, state = _split_loc(si.location)
        if city:
            parts.append(f'"{city}"')
        if state:
            parts.append(state)
    if si.is_remote:
        parts.append('"remote"')
    return " ".join(parts)


async def _discover_via_google(query: str, wanted: int) -> list[tuple[str, str]]:
    """Drive headless Chrome through Google SERPs until we have enough URLs.

    Returns a list of (board_token, job_id) in result order, deduplicated.
    """
    from selenium_driverless import webdriver

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1280,900")

    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    encoded = quote_plus(query)

    async with webdriver.Chrome(options=options) as driver:
        # Walk up to 5 SERP pages (~50 results) to satisfy `wanted`.
        for page in range(5):
            url = _GOOGLE_SEARCH_URL.format(query=encoded, start=page * 10)
            log.info(f"Greenhouse: SERP page {page + 1} → {url[:120]}")
            try:
                await driver.get(url, wait_load=True, timeout=_NAV_TIMEOUT_S)
            except Exception as e:
                log.error(f"Greenhouse: SERP fetch failed on page {page + 1}: {e}")
                break
            await asyncio.sleep(_RENDER_SLEEP_S)

            current_url = await driver.current_url
            if "/sorry/" in current_url:
                log.error(
                    f"Greenhouse: hit Google /sorry/ CAPTCHA on page {page + 1}. "
                    "Returning what we have."
                )
                break

            html = await driver.page_source
            new_count = 0
            for m in _GH_URL_RE.finditer(html):
                key = (m.group(1), m.group(2))
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(key)
                new_count += 1

            log.info(
                f"Greenhouse: page {page + 1} added {new_count} URLs "
                f"(total {len(ordered)} / wanted {wanted})"
            )
            if len(ordered) >= wanted:
                break
            if new_count == 0:
                # Either Google returned the same results twice or we ran
                # out of matches — no point fetching more pages.
                break

    return ordered


# -----------------------------------------------------------------------------
# Stage 2 — Greenhouse API enrichment
# -----------------------------------------------------------------------------


def _fetch_details_bulk(
    postings: list[tuple[str, str]],
) -> dict[tuple[str, str], dict]:
    """Parallel-fetch Greenhouse public-API job details. Failures are logged
    and the entry is skipped — we never crash on a single 404."""
    out: dict[tuple[str, str], dict] = {}
    if not postings:
        return out

    def fetch(item: tuple[str, str]) -> tuple[tuple[str, str], dict | None]:
        board, jid = item
        try:
            r = requests.get(
                _API_URL.format(board=board, id=jid),
                timeout=_API_TIMEOUT_S,
                headers={"Accept": "application/json"},
            )
            if r.ok:
                return item, r.json()
            log.debug(f"Greenhouse API {board}/{jid}: HTTP {r.status_code}")
        except Exception as e:
            log.debug(f"Greenhouse API {board}/{jid} failed: {e}")
        return item, None

    with ThreadPoolExecutor(max_workers=_API_WORKERS) as ex:
        for fut in as_completed([ex.submit(fetch, p) for p in postings]):
            key, data = fut.result()
            if data is not None:
                out[key] = data
    if len(out) < len(postings):
        log.info(
            f"Greenhouse: API hit {len(out)}/{len(postings)} — "
            "rest were 404 or fetch-failed"
        )
    return out


def _build_jobpost(
    data: dict, board: str, country: Country | None,
) -> JobPost | None:
    job_id = data.get("id")
    title = (data.get("title") or "").strip()
    if not job_id or not title:
        return None
    title = " ".join(title.split())

    # Location: take the first office for structured city/state if we
    # have one; otherwise fall back to the free-form location.name.
    offices = data.get("offices") or []
    location_obj: Location | None = None
    if offices:
        first = offices[0] or {}
        loc_text = (first.get("location") or first.get("name") or "").strip()
        city, state = _split_loc(loc_text)
        location_obj = Location(
            city=city, state=state, country=country or Country.USA,
        )
    elif (data.get("location") or {}).get("name"):
        loc_text = data["location"]["name"]
        city, state = _split_loc(loc_text.split(";")[0])
        location_obj = Location(
            city=city, state=state, country=country or Country.USA,
        )

    is_remote = _detect_remote(data)
    posted = (
        _parse_iso_date(data.get("first_published"))
        or _parse_iso_date(data.get("updated_at"))
    )

    # Description: ``content`` is HTML with entities; decode entities and
    # strip tags for a plain-text body. Keep paragraph breaks as newlines
    # so the description is human-readable.
    description = _html_to_text(data.get("content") or "") or None

    departments = data.get("departments") or []
    industry = (departments[0].get("name") if departments else None)

    return JobPost(
        id=f"gh-{board}-{job_id}",
        title=title,
        company_name=(data.get("company_name") or "").strip() or None,
        location=location_obj,
        description=description,
        job_url=data.get("absolute_url") or "",
        date_posted=posted,
        is_remote=is_remote,
        company_industry=industry,
    )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _split_loc(text: str) -> tuple[str | None, str | None]:
    parts = [p.strip() for p in text.split(",")]
    city = parts[0] if parts else None
    state = parts[1] if len(parts) > 1 else None
    return (city or None), (state or None)


def _detect_remote(data: dict) -> bool:
    """Greenhouse exposes work-arrangement two ways — neither is 100%
    reliable on its own:

      1. ``metadata`` may have ``{name: "Location Type", value: "Remote"}``
      2. ``location.name`` / office names may say "Remote" or "Remote-Friendly"

    Mark fully-remote when either signal is present, but only when the
    text is a clear "Remote" — "Remote-Friendly" is hybrid-ish and we
    err on the conservative side.
    """
    for entry in data.get("metadata") or []:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "")
        if not isinstance(name, str):
            continue
        # ``value`` can be a string, list, or nested dict depending on
        # ``value_type`` (single_select / multi_select / etc.) — coerce
        # to a flat lowercase string and search within it.
        raw_val = entry.get("value")
        if isinstance(raw_val, str):
            value_str = raw_val
        elif isinstance(raw_val, list):
            value_str = " ".join(str(x) for x in raw_val)
        elif isinstance(raw_val, dict):
            # e.g. {"label": "Remote", "value": ...} or {"name": "Remote"}
            value_str = " ".join(
                str(v) for v in raw_val.values() if isinstance(v, str)
            )
        else:
            value_str = ""
        if "location type" in name.lower() and "remote" in value_str.lower():
            # Treat any "Remote" mention in the Location Type field as
            # remote-friendly. We don't try to disambiguate "Hybrid" vs
            # "Fully Remote" here — the description usually clarifies.
            return True
    loc_obj = data.get("location") or {}
    loc_name = (loc_obj.get("name") or "") if isinstance(loc_obj, dict) else ""
    if loc_name.lower() in ("remote", "anywhere"):
        return True
    return False


def _html_to_text(content: str) -> str:
    if not content:
        return ""
    decoded = html_lib.unescape(content)
    soup = BeautifulSoup(decoded, "html.parser")
    # Replace block tags with newlines so paragraphs aren't run-together.
    for br in soup.find_all(["br"]):
        br.replace_with("\n")
    for block in soup.find_all(["p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6"]):
        block.append("\n")
    text = soup.get_text()
    # Collapse runs of blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except (ValueError, TypeError):
        return None


def _parse_iso_dt(s: str | None) -> datetime | None:
    """Parse an ISO 8601 string and force a UTC tz so comparisons against
    ``datetime.now(timezone.utc)`` work uniformly.

    Greenhouse returns timestamps with a fixed offset (e.g.
    ``2026-05-06T13:54:16-04:00``) which fromisoformat handles natively.
    Naive timestamps are interpreted as UTC.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _run_async(coro):
    return asyncio.run(coro)


def _run_on_thread(coro):
    """Same shim as the Google scraper — run an awaitable from sync code
    that's already inside a running event loop."""
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
