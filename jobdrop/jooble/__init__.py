"""Jooble scraper — international job aggregator.

Jooble ingests from many regional/specialty boards Indeed misses.
Heavy overlap with our Indeed scraper for US-only queries (~70%
duplicates expected) but valuable for European/Asian regional listings
and for occasional unique smaller-board placements.

## Caveats

  - Salary is returned as a free-text string (e.g. "$50K - $80K a year"),
    not structured. We regex-parse it.
  - Many results are noisy ("technician" matches Forklift Tech, etc.)
    when the keyword is too generic.

Configuration is supplied via `_defaults._get`.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

import requests

from jobdrop._defaults import _get
from jobdrop.model import (
    Compensation,
    CompensationInterval,
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

log = create_logger("Jooble")

_TIMEOUT_S = 20

_JOB_TYPE_MAP = {
    "Full-time": JobType.FULL_TIME,
    "Full Time": JobType.FULL_TIME,
    "Part-time": JobType.PART_TIME,
    "Part Time": JobType.PART_TIME,
    "Contract": JobType.CONTRACT,
    "Internship": JobType.INTERNSHIP,
    "Temporary": JobType.TEMPORARY,
}

# Salary patterns — Jooble's `salary` field is free-text.
_SAL_RANGE_RE = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*([KkMm]?)"
    r"\s*(?:[-–—]+|to)\s*"
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*([KkMm]?)"
    r"\s*(?:per\s+|a\s+|an\s+|/)?"
    r"(year|yr|annual|hour|hr|month|week|day)?",
    re.I,
)
_INTERVAL_LOOKUP = {
    "year": CompensationInterval.YEARLY,
    "yr": CompensationInterval.YEARLY,
    "annual": CompensationInterval.YEARLY,
    "hour": CompensationInterval.HOURLY,
    "hr": CompensationInterval.HOURLY,
    "month": CompensationInterval.MONTHLY,
    "week": CompensationInterval.WEEKLY,
    "day": CompensationInterval.DAILY,
}


class Jooble(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.JOOBLE, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        api_key = _get(4).strip()
        if not api_key:
            log.error("Jooble: configuration unavailable")
            return JobResponse(jobs=[])

        body: dict[str, Any] = {
            "keywords": scraper_input.search_term or "",
            "location": scraper_input.location or "",
            "page": "1",
        }
        if getattr(scraper_input, "hours_old", None):
            n_days = max(scraper_input.hours_old // 24, 1)
            body["datecreatedfrom"] = (datetime.now() - timedelta(days=n_days)).strftime("%Y-%m-%d")

        try:
            r = requests.post(
                f"https://jooble.org/api/{api_key}",
                json=body,
                timeout=_TIMEOUT_S,
            )
        except Exception as e:
            log.error(f"Jooble: request failed: {e}")
            return JobResponse(jobs=[])

        if not r.ok:
            log.error(f"Jooble: status {r.status_code} — {r.text[:200]}")
            return JobResponse(jobs=[])

        items = r.json().get("jobs", [])
        log.info(f"Jooble: {len(items)} raw items")

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

        log.info(f"Jooble: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


def _parse_salary(text: str | None) -> Compensation | None:
    if not text:
        return None
    m = _SAL_RANGE_RE.search(text)
    if not m:
        return None
    try:
        mn = float(m.group(1).replace(",", ""))
        mx = float(m.group(3).replace(",", ""))
    except ValueError:
        return None
    if m.group(2) and m.group(2).lower() == "k":
        mn *= 1000
    if m.group(4) and m.group(4).lower() == "k":
        mx *= 1000
    if m.group(2) and m.group(2).lower() == "m":
        mn *= 1_000_000
    if m.group(4) and m.group(4).lower() == "m":
        mx *= 1_000_000
    interval_word = (m.group(5) or "").lower()
    interval = _INTERVAL_LOOKUP.get(interval_word)
    if interval is None:
        # Heuristic: large numbers default yearly, small default hourly
        interval = CompensationInterval.YEARLY if mn >= 10_000 else CompensationInterval.HOURLY
    return Compensation(
        interval=interval,
        min_amount=mn,
        max_amount=mx,
        currency="USD",
    )


def _build_jobpost(item: dict, country: Country | None) -> JobPost | None:
    try:
        listing_id = item.get("id") or item.get("link")
        if not listing_id:
            return None
        title = (item.get("title") or "").strip() or None
        if not title:
            return None
        title = " ".join(title.split())

        company = (item.get("company") or "").strip() or None
        loc_str = (item.get("location") or "").strip()

        location_obj: Location | None = None
        if loc_str:
            parts = [p.strip() for p in loc_str.split(",")]
            city = parts[0] if parts else None
            state = parts[1] if len(parts) > 1 else None
            location_obj = Location(city=city, state=state, country=country or Country.USA)

        compensation = _parse_salary(item.get("salary"))

        date_posted: date | None = None
        updated = item.get("updated")
        if updated:
            try:
                date_posted = datetime.fromisoformat(updated.split(".")[0]).date()
            except (ValueError, AttributeError):
                pass

        job_types: list[JobType] = []
        jt_raw = (item.get("type") or "").strip()
        if jt_raw in _JOB_TYPE_MAP:
            job_types.append(_JOB_TYPE_MAP[jt_raw])

        return JobPost(
            id=f"jb-{listing_id}",
            title=title,
            company_name=company,
            location=location_obj,
            description=item.get("snippet") or None,
            date_posted=date_posted,
            job_url=item.get("link") or "",
            compensation=compensation,
            job_type=job_types or None,
        )
    except Exception as e:
        log.warning(f"Jooble: skipping malformed item: {e}")
        return None
