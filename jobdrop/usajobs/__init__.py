"""USAJobs.gov scraper — official federal-jobs API.

Federal hiring lives almost exclusively on USAJobs. Returns roles
that don't appear in any other source we scrape — DoD/civilian IT,
GS-civilian network engineering, cleared NOC contractors, etc. Salary
ranges are exposed directly in the API.

Configuration (auth header + identifying email) is supplied via
`_defaults._get`.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import requests

from jobdrop._defaults import _get
from jobdrop.model import (
    Compensation,
    CompensationInterval,
    Country,
    JobPost,
    JobResponse,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobdrop.util import create_logger

log = create_logger("USAJobs")

_API_URL = "https://data.usajobs.gov/api/search"
_HOST = "data.usajobs.gov"
_TIMEOUT_S = 20
_PER_PAGE = 25  # USAJobs default; max is 500

# RateIntervalCode is a 2-char code in USAJobs API. PA=Per Annum is by far
# the most common; the other codes are documented at
# https://developer.usajobs.gov/api-reference/get-api-codelist-rateintervalcodes
_INTERVAL_MAP = {
    "PA": CompensationInterval.YEARLY,    # Per Annum
    "PH": CompensationInterval.HOURLY,    # Per Hour
    "PD": CompensationInterval.DAILY,     # Per Day
    "PW": CompensationInterval.WEEKLY,    # Per Week
    "BW": CompensationInterval.WEEKLY,    # Bi-Weekly (closest match)
    "PM": CompensationInterval.MONTHLY,   # Per Month
}


class USAJobs(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.USAJOBS, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        api_key = _get(2).strip()
        ua_email = _get(3).strip()
        if not api_key or not ua_email:
            log.error("USAJobs: configuration unavailable")
            return JobResponse(jobs=[])

        headers = {
            "Host": _HOST,
            "User-Agent": ua_email,
            "Authorization-Key": api_key,
            "Accept": "application/json",
        }

        params: dict[str, Any] = {
            "Keyword": scraper_input.search_term or "",
            "ResultsPerPage": min(scraper_input.results_wanted or _PER_PAGE, 500),
        }
        if scraper_input.location:
            params["LocationName"] = scraper_input.location
        if scraper_input.is_remote:
            params["RemoteIndicator"] = "true"
        if getattr(scraper_input, "hours_old", None):
            params["DatePosted"] = max(scraper_input.hours_old // 24, 1)

        try:
            r = requests.get(_API_URL, headers=headers, params=params, timeout=_TIMEOUT_S)
        except Exception as e:
            log.error(f"USAJobs: request failed: {e}")
            return JobResponse(jobs=[])

        if not r.ok:
            log.error(f"USAJobs: status {r.status_code} — {r.text[:200]}")
            return JobResponse(jobs=[])

        items = r.json().get("SearchResult", {}).get("SearchResultItems", [])
        log.info(f"USAJobs: {len(items)} raw items")

        jobs: list[JobPost] = []
        seen_ids: set[str] = set()
        for item in items:
            post = _build_jobpost(item)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)
            if len(jobs) >= scraper_input.results_wanted:
                break

        log.info(f"USAJobs: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


def _build_jobpost(item: dict) -> JobPost | None:
    try:
        d = item.get("MatchedObjectDescriptor") or {}
        listing_id = item.get("MatchedObjectId") or d.get("PositionID") or ""
        if not listing_id:
            return None
        title = d.get("PositionTitle")
        if not title:
            return None

        company_name = d.get("OrganizationName") or d.get("DepartmentName")

        # Location: first PositionLocation entry — "City, State, Country"
        loc_objs = d.get("PositionLocation") or []
        location_obj: Location | None = None
        if loc_objs:
            first = loc_objs[0]
            city = first.get("CityName")
            state = first.get("CountrySubDivisionCode")
            country_str = first.get("CountryCode")
            country = (
                Country.USA
                if country_str in {"United States", "United States of America"}
                else (country_str or Country.USA)
            )
            location_obj = Location(city=city, state=state, country=country)

        # Compensation: PositionRemuneration is a list, take first
        compensation: Compensation | None = None
        rem = (d.get("PositionRemuneration") or [{}])[0]
        if rem:
            mn_raw = rem.get("MinimumRange")
            mx_raw = rem.get("MaximumRange")
            interval_code = rem.get("RateIntervalCode") or ""
            try:
                mn = float(mn_raw) if mn_raw not in (None, "") else None
                mx = float(mx_raw) if mx_raw not in (None, "") else None
            except (TypeError, ValueError):
                mn = mx = None
            if mn is not None or mx is not None:
                compensation = Compensation(
                    interval=_INTERVAL_MAP.get(interval_code),
                    min_amount=mn,
                    max_amount=mx,
                    currency="USD",
                )

        # Date posted: PublicationStartDate or PositionStartDate
        date_posted: date | None = None
        date_str = d.get("PublicationStartDate") or d.get("PositionStartDate")
        if date_str:
            try:
                date_posted = datetime.fromisoformat(date_str.split(".")[0]).date()
            except ValueError:
                pass

        # ApplyURI is a list of strings; first is the canonical apply URL
        apply_uri = d.get("ApplyURI") or []
        apply_url = apply_uri[0] if apply_uri else None

        # Standard USAJobs job-page URL
        position_uri = d.get("PositionURI")
        job_url = position_uri or apply_url or f"https://www.usajobs.gov/job/{listing_id}"

        # Description: USAJobs surfaces summary in QualificationSummary
        description = (
            (d.get("UserArea") or {}).get("Details", {}).get("JobSummary")
            or d.get("QualificationSummary")
        )

        return JobPost(
            id=f"uj-{listing_id}",
            title=title,
            company_name=company_name,
            company_url=None,
            company_url_direct=None,
            location=location_obj,
            description=description,
            date_posted=date_posted,
            job_url=job_url,
            job_url_direct=apply_url,
            compensation=compensation,
            is_remote=("Remote" in (d.get("PositionLocationDisplay") or "")),
        )
    except Exception as e:
        log.warning(f"USAJobs: skipping malformed item: {e}")
        return None
