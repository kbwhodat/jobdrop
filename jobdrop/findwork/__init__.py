"""Findwork scraper — dev-focused job board.

Findwork mostly indexes software engineering / dev / SRE / startup roles
with direct-employer postings (less recruiter spam than aggregators).
Lower fit for traditional NOC / IT-tech roles, higher fit for tech-y
positions where the user is targeting specific companies.

## Caveats

  - No salary data in the API response — that field doesn't exist on
    Findwork's side either.
  - Location is a free-text string (often "Remote" or "City, ST" or
    "City, ST and Y other locations").

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
    JobType,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobdrop.util import create_logger

log = create_logger("Findwork")

_API_URL = "https://findwork.dev/api/jobs/"
_TIMEOUT_S = 20

_JOB_TYPE_MAP = {
    "full-time": JobType.FULL_TIME,
    "part-time": JobType.PART_TIME,
    "contract": JobType.CONTRACT,
    "internship": JobType.INTERNSHIP,
    "temporary": JobType.TEMPORARY,
}


class Findwork(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.FINDWORK, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        api_key = _get(5).strip()
        if not api_key:
            log.error("Findwork: configuration unavailable")
            return JobResponse(jobs=[])

        params: dict[str, Any] = {}
        if scraper_input.search_term:
            params["search"] = scraper_input.search_term
        if scraper_input.location:
            params["location"] = scraper_input.location
        if scraper_input.is_remote:
            params["remote"] = "true"

        try:
            r = requests.get(
                _API_URL,
                params=params,
                headers={"Authorization": f"Token {api_key}", "Accept": "application/json"},
                timeout=_TIMEOUT_S,
            )
        except Exception as e:
            log.error(f"Findwork: request failed: {e}")
            return JobResponse(jobs=[])

        if not r.ok:
            log.error(f"Findwork: status {r.status_code} — {r.text[:200]}")
            return JobResponse(jobs=[])

        items = r.json().get("results", [])
        log.info(f"Findwork: {len(items)} raw items")

        jobs: list[JobPost] = []
        seen_ids: set[str] = set()
        for item in items:
            post = _build_jobpost(item, scraper_input.country)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)
            if len(jobs) >= scraper_input.results_wanted:
                break

        log.info(f"Findwork: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


def _build_jobpost(item: dict, country: Country | None) -> JobPost | None:
    try:
        listing_id = item.get("id")
        if listing_id is None:
            return None
        title = (item.get("role") or "").strip() or None
        if not title:
            return None
        title = " ".join(title.split())

        company = (item.get("company_name") or "").strip() or None

        loc_raw = (item.get("location") or "").strip()
        location_obj: Location | None = None
        if loc_raw and loc_raw.lower() != "remote":
            # Findwork sometimes returns "City, ST and 3 other locations" —
            # strip the suffix.
            loc_clean = loc_raw.split(" and ")[0].strip()
            parts = [p.strip() for p in loc_clean.split(",")]
            city = parts[0] if parts else None
            state = parts[1] if len(parts) > 1 else None
            location_obj = Location(city=city, state=state, country=country or Country.USA)

        date_posted: date | None = None
        date_str = item.get("date_posted")
        if date_str:
            try:
                date_posted = datetime.fromisoformat(date_str.rstrip("Z").split(".")[0]).date()
            except (ValueError, AttributeError):
                pass

        job_types: list[JobType] = []
        et = (item.get("employment_type") or "").strip().lower()
        if et in _JOB_TYPE_MAP:
            job_types.append(_JOB_TYPE_MAP[et])

        is_remote = bool(item.get("remote"))

        return JobPost(
            id=f"fw-{listing_id}",
            title=title,
            company_name=company,
            company_num_employees=item.get("company_num_employees"),
            company_logo=item.get("logo"),
            location=location_obj,
            description=item.get("text") or None,
            date_posted=date_posted,
            job_url=item.get("url") or "",
            job_type=job_types or None,
            is_remote=is_remote,
        )
    except Exception as e:
        log.warning(f"Findwork: skipping malformed item: {e}")
        return None
