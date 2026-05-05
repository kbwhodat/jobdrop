"""Adzuna scraper — large free aggregator with rich salary data.

Adzuna ingests from Indeed, Reed, and many regional/specialty boards.
~40% overlap with our Indeed scraper but distinct value: 100% salary
fill rate (predicted-when-missing, but always populated) which is
better than Indeed's ~85%.

## Auth

Two query parameters required:
  - app_id  (~8 hex chars, the registered application ID)
  - app_key (32 hex chars, the application key)

Read from env vars `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`. Free tier
allows 250 calls/month per dev account. Register at
https://developer.adzuna.com/.

## Country code

Adzuna URL embeds a 2-char country code: `/jobs/<country>/search/...`.
We map jobspy's Country enum to those codes — defaults to "us".
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

import requests

from jobspy.model import (
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
from jobspy.util import create_logger

log = create_logger("Adzuna")

_TIMEOUT_S = 20

# Adzuna's supported country codes (subset of common ones — full list at
# https://developer.adzuna.com/docs/search). Map jobspy's Country enum
# to Adzuna's 2-char code.
_COUNTRY_MAP = {
    Country.USA: "us",
    Country.UK: "gb",
    Country.CANADA: "ca",
    Country.AUSTRALIA: "au",
    Country.GERMANY: "de",
    Country.FRANCE: "fr",
    Country.NETHERLANDS: "nl",
    Country.ITALY: "it",
    Country.AUSTRIA: "at",
    Country.BRAZIL: "br",
    Country.INDIA: "in",
    Country.MEXICO: "mx",
    Country.NEWZEALAND: "nz",
    Country.POLAND: "pl",
    Country.SINGAPORE: "sg",
    Country.SOUTHAFRICA: "za",
    Country.SPAIN: "es",
    Country.SWITZERLAND: "ch",
}

# Adzuna's contract_time values mapped to JobType
_JOB_TYPE_MAP = {
    "full_time": JobType.FULL_TIME,
    "part_time": JobType.PART_TIME,
}
# Adzuna's contract_type values
_CONTRACT_TYPE_MAP = {
    "contract": JobType.CONTRACT,
    "permanent": JobType.FULL_TIME,
}


class Adzuna(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.ADZUNA, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
        app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
        if not app_id or not app_key:
            log.error(
                "Adzuna: missing ADZUNA_APP_ID or ADZUNA_APP_KEY env vars. "
                "Register at developer.adzuna.com/."
            )
            return JobResponse(jobs=[])

        country_code = _COUNTRY_MAP.get(scraper_input.country, "us")
        per_page = min(scraper_input.results_wanted or 50, 50)

        params: dict[str, Any] = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": per_page,
            "what": scraper_input.search_term or "",
        }
        if scraper_input.location:
            params["where"] = scraper_input.location
        if scraper_input.distance:
            params["distance"] = scraper_input.distance
        if scraper_input.is_remote:
            # Adzuna doesn't have a first-class remote filter; appending
            # the keyword is the documented workaround.
            params["what"] = f"{params['what']} remote".strip()
        if getattr(scraper_input, "hours_old", None):
            params["max_days_old"] = max(scraper_input.hours_old // 24, 1)
        if scraper_input.job_type == JobType.FULL_TIME:
            params["full_time"] = "1"
        elif scraper_input.job_type == JobType.PART_TIME:
            params["part_time"] = "1"

        url = f"https://api.adzuna.com/v1/api/jobs/{country_code}/search/1"
        try:
            r = requests.get(url, params=params, timeout=_TIMEOUT_S)
        except Exception as e:
            log.error(f"Adzuna: request failed: {e}")
            return JobResponse(jobs=[])

        if not r.ok:
            log.error(f"Adzuna: status {r.status_code} — {r.text[:200]}")
            return JobResponse(jobs=[])

        items = r.json().get("results", [])
        log.info(f"Adzuna: {len(items)} raw items")

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

        log.info(f"Adzuna: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


def _build_jobpost(item: dict, country: Country | None) -> JobPost | None:
    try:
        listing_id = item.get("id")
        if not listing_id:
            return None
        title = (item.get("title") or "").strip() or None
        if not title:
            return None
        # Adzuna sometimes wraps titles in newlines/whitespace
        title = " ".join(title.split())

        company = (item.get("company") or {}).get("display_name")
        if company:
            company = company.strip() or None

        # Location: display_name is a comma-joined string; area is a list
        location_obj: Location | None = None
        loc_data = item.get("location") or {}
        loc_display = (loc_data.get("display_name") or "").strip()
        area = loc_data.get("area") or []
        # area is typically [country, state, city] — last is most specific
        city = area[-1] if area else None
        state = area[-2] if len(area) >= 2 else None
        if loc_display or city:
            location_obj = Location(
                city=city or loc_display or None,
                state=state,
                country=country or Country.USA,
            )

        # Compensation: salary_min / salary_max, both numeric. Adzuna often
        # provides estimates; salary_is_predicted="1" flags those.
        compensation: Compensation | None = None
        sal_min = item.get("salary_min")
        sal_max = item.get("salary_max")
        if sal_min is not None or sal_max is not None:
            try:
                mn = float(sal_min) if sal_min is not None else None
                mx = float(sal_max) if sal_max is not None else None
                # Adzuna returns hourly as small numbers (~$25/hr) and yearly
                # as full numbers (~$95,000). Heuristic: <500 → hourly.
                interval = (
                    CompensationInterval.HOURLY
                    if (mn or mx or 0) < 500
                    else CompensationInterval.YEARLY
                )
                compensation = Compensation(
                    interval=interval,
                    min_amount=mn,
                    max_amount=mx,
                    currency="USD",
                )
            except (TypeError, ValueError):
                pass

        # Date posted: `created` is ISO-8601 with optional Z suffix
        date_posted: date | None = None
        created = item.get("created")
        if created:
            try:
                clean = created.rstrip("Z").split(".")[0]
                date_posted = datetime.fromisoformat(clean).date()
            except (ValueError, AttributeError):
                pass

        # Job type: contract_time + contract_type both present
        job_types: list[JobType] = []
        ct_time = item.get("contract_time")
        ct_type = item.get("contract_type")
        if ct_time in _JOB_TYPE_MAP:
            job_types.append(_JOB_TYPE_MAP[ct_time])
        if ct_type in _CONTRACT_TYPE_MAP:
            jt = _CONTRACT_TYPE_MAP[ct_type]
            if jt not in job_types:
                job_types.append(jt)

        category = (item.get("category") or {}).get("label")
        description = item.get("description") or None

        return JobPost(
            id=f"az-{listing_id}",
            title=title,
            company_name=company,
            location=location_obj,
            description=description,
            date_posted=date_posted,
            job_url=item.get("redirect_url") or "",
            compensation=compensation,
            job_type=job_types or None,
            company_industry=category,
        )
    except Exception as e:
        log.warning(f"Adzuna: skipping malformed item: {e}")
        return None
