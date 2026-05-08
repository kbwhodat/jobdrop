"""Bayt scraper — HTML search page via zendriver.

Bayt's plain HTTP fetch returns ``HTTP 403 Forbidden`` to ``requests``
and ``curl_cffi`` clients (likely a per-IP/per-UA bot wall). The HTML
itself is server-rendered with stable CSS hooks, so a real browser
fetch + BeautifulSoup parse works.

Approach: drive the search page with zendriver (CDP-direct, no
WebDriver fingerprint), grab the rendered HTML, and parse with the
existing BS4 selectors:

  - Card container:  ``li[data-js-job]``
  - Title:           ``h2 > a``
  - Company:         ``div.t-nowrap.p10l > span``
  - Location:        ``div.t-mute.t-small``

URL pattern (unchanged):
  ``https://www.bayt.com/en/international/jobs/{kw-slug}-jobs/?page={N}``
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from bs4 import BeautifulSoup

from jobdrop.model import (
    Country,
    JobPost,
    JobResponse,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobdrop.util import create_logger

log = create_logger("Bayt")

_BASE = "https://www.bayt.com"
_RENDER_SLEEP_S = 6.0


class BaytScraper(Scraper):
    base_url = _BASE

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.BAYT, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self.country = "worldwide"
        # user_agent accepted for compatibility but not forwarded —
        # overriding the UA contradicts zendriver's natural fingerprint.
        self._user_agent = user_agent

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted or 10

        try:
            import zendriver as zd  # noqa: F401
        except ImportError:
            log.error(
                "Bayt: zendriver is required. "
                "Install with: pip install zendriver"
            )
            return JobResponse(jobs=[])

        try:
            pages_html = _run_async(
                _fetch_pages(scraper_input.search_term or "", wanted)
            )
        except RuntimeError as e:
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                pages_html = _run_on_thread(
                    _fetch_pages(scraper_input.search_term or "", wanted)
                )
            else:
                raise

        job_list: list[JobPost] = []
        seen_ids: set[str] = set()
        for html in pages_html:
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.find_all("li", attrs={"data-js-job": True})
            log.info(f"Bayt: found {len(cards)} cards on page")
            for card in cards:
                try:
                    post = self._extract_job_info(card)
                except Exception as e:
                    log.error(f"Bayt: Error extracting job info: {e}")
                    continue
                if not post or post.id in seen_ids:
                    continue
                seen_ids.add(post.id)
                job_list.append(post)
                if len(job_list) >= wanted:
                    break
            if len(job_list) >= wanted:
                break

        log.info(f"Bayt: returning {len(job_list)} jobs")
        return JobResponse(jobs=job_list[:wanted])

    def _extract_job_info(self, job) -> JobPost | None:
        job_general_information = job.find("h2")
        if not job_general_information:
            return None

        job_title = job_general_information.get_text(strip=True)
        job_url = self._extract_job_url(job_general_information)
        if not job_url:
            return None

        company_tag = job.find("div", class_="t-nowrap p10l")
        company_name = (
            company_tag.find("span").get_text(strip=True)
            if company_tag and company_tag.find("span")
            else None
        )

        location_tag = job.find("div", class_="t-mute t-small")
        location = location_tag.get_text(strip=True) if location_tag else None

        job_id = f"bayt-{abs(hash(job_url))}"
        location_obj = Location(
            city=location,
            country=Country.from_string(self.country),
        )
        return JobPost(
            id=job_id,
            title=job_title,
            company_name=company_name,
            location=location_obj,
            job_url=job_url,
        )

    def _extract_job_url(self, job_general_information) -> str | None:
        a_tag = job_general_information.find("a")
        if a_tag and a_tag.has_attr("href"):
            return _BASE + a_tag["href"].strip()
        return None


# -----------------------------------------------------------------------------
# Async fetch helpers
# -----------------------------------------------------------------------------


def _build_url(search_term: str, page: int) -> str:
    """Bayt URL: kebab-case slug. Spaces → hyphens, lowercased."""
    slug = "-".join(part for part in (search_term or "").lower().split() if part)
    if not slug:
        slug = "jobs"
    return f"{_BASE}/en/international/jobs/{slug}-jobs/?page={page}"


async def _fetch_pages(search_term: str, wanted: int) -> list[str]:
    """Fetch enough Bayt SERPs (30 cards/page) to satisfy ``wanted``.

    Returns a list of rendered HTML strings — one per page — to be
    parsed by the BS4 logic in ``BaytScraper``.
    """
    import zendriver as zd

    browser = await zd.start(
        headless=True, sandbox=False, browser_args=["--window-size=1280,900"],
    )
    pages: list[str] = []
    # Bayt renders 30 jobs per page. Cap at 5 pages (~150 cards).
    max_pages = min(5, max(1, (wanted + 29) // 30))
    try:
        for page in range(1, max_pages + 1):
            url = _build_url(search_term, page)
            log.info(f"Bayt: fetching page {page} → {url}")
            try:
                tab = await browser.get(url)
            except Exception as e:
                log.error(f"Bayt: page {page} fetch failed: {e}")
                break
            await asyncio.sleep(_RENDER_SLEEP_S)
            html = await tab.get_content()
            pages.append(html)
            if len(pages) * 30 >= wanted:
                break
    finally:
        await browser.stop()
    return pages


def _run_async(coro):
    return asyncio.run(coro)


def _run_on_thread(coro):
    """Run an awaitable from sync code already inside a running loop."""
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
