"""The Muse scraper — curated company-direct postings.

The Muse skews toward mid/large tech companies with culture-brand
investment (think AWS, Stripe, HubSpot, etc.). Lower volume than
aggregators but distinct value: postings come straight from employer
career pages, less recruiter noise.

## Caveats

  - No salary data on Muse (their UI doesn't expose it either).
  - Search is by category/location filter, not free-text keyword.
    To find specific titles, we filter the result set client-side
    against the search_term.

Configuration is supplied via `_defaults._get`.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import requests

from jobdrop._defaults import _get
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

log = create_logger("TheMuse")

_API_URL = "https://www.themuse.com/api/public/jobs"
_TIMEOUT_S = 20
_MAX_PAGES = 5  # Muse paginates 20 results per page; walk a few to find matches


class TheMuse(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.THE_MUSE, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        api_key = _get(6).strip()

        # Muse's category-filter API returns suspiciously empty result sets
        # (verified in testing — "Networks and Hardware", "Engineering", and
        # any reasonable category name all returned 0 jobs). Falling back to
        # paginated unfiltered fetch + client-side keyword match is more
        # reliable for niche searches.
        tokens: list[str] = []
        if scraper_input.search_term:
            tokens = [t for t in scraper_input.search_term.lower().split() if len(t) >= 3]

        jobs: list[JobPost] = []
        seen_ids: set[str] = set()
        total_raw = 0

        for page in range(_MAX_PAGES):
            params: dict[str, Any] = {"page": page}
            if api_key:
                params["api_key"] = api_key
            if scraper_input.location:
                params["location"] = scraper_input.location
            try:
                r = requests.get(_API_URL, params=params, timeout=_TIMEOUT_S)
            except Exception as e:
                log.error(f"TheMuse: request failed on page {page}: {e}")
                break
            if not r.ok:
                log.error(f"TheMuse: status {r.status_code} on page {page}")
                break
            page_items = r.json().get("results", [])
            total_raw += len(page_items)
            if not page_items:
                break
            # Client-side keyword filter — only keep matches when caller
            # provided search_term tokens.
            if tokens:
                page_items = [
                    it for it in page_items
                    if any(
                        tok in (it.get("name") or "").lower()
                        or tok in (it.get("short_name") or "").lower()
                        for tok in tokens
                    )
                ]
            for item in page_items:
                post = _build_jobpost(item, scraper_input.country)
                if post is None or post.id in seen_ids:
                    continue
                seen_ids.add(post.id)
                jobs.append(post)
                if len(jobs) >= scraper_input.results_wanted:
                    break
            if len(jobs) >= scraper_input.results_wanted:
                break

        log.info(
            f"TheMuse: scanned {total_raw} raw items across {page+1} pages, "
            f"returning {len(jobs)} after keyword filter"
        )
        return JobResponse(jobs=jobs)


def _build_jobpost(item: dict, country: Country | None) -> JobPost | None:
    try:
        listing_id = item.get("id")
        if listing_id is None:
            return None
        title = (item.get("name") or "").strip() or None
        if not title:
            return None
        title = " ".join(title.split())

        company_obj = item.get("company") or {}
        company = (company_obj.get("name") or "").strip() or None

        # First location wins; Muse jobs often list multiple
        locations = item.get("locations") or []
        location_obj: Location | None = None
        if locations:
            loc_name = (locations[0].get("name") or "").strip()
            if loc_name:
                if loc_name.lower() in {"flexible / remote", "remote"}:
                    location_obj = Location(country=country or Country.USA)
                else:
                    parts = [p.strip() for p in loc_name.split(",")]
                    city = parts[0] if parts else None
                    state = parts[1] if len(parts) > 1 else None
                    location_obj = Location(city=city, state=state, country=country or Country.USA)

        date_posted: date | None = None
        date_str = item.get("publication_date")
        if date_str:
            try:
                date_posted = datetime.fromisoformat(date_str.rstrip("Z").split(".")[0]).date()
            except (ValueError, AttributeError):
                pass

        levels = item.get("levels") or []
        job_level = (levels[0].get("name") if levels else None)

        categories = item.get("categories") or []
        company_industry = (categories[0].get("name") if categories else None)

        is_remote = any(
            "remote" in (l.get("name") or "").lower() for l in locations
        )

        return JobPost(
            id=f"tm-{listing_id}",
            title=title,
            company_name=company,
            location=location_obj,
            description=item.get("contents") or None,
            date_posted=date_posted,
            job_url=(item.get("refs") or {}).get("landing_page") or "",
            company_industry=company_industry,
            job_level=job_level,
            is_remote=is_remote,
        )
    except Exception as e:
        log.warning(f"TheMuse: skipping malformed item: {e}")
        return None
