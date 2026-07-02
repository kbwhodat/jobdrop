"""Snagajob scraper — US hourly / part-time job marketplace.

Snagajob is the dominant US board for hourly retail, food, warehouse,
caregiving, and shift-work roles. High-volume, often-overlooked source
for searches focused on part-time, weekend-only, or no-degree positions.

Discovered API contract (no auth, anonymous-friendly):

    GET https://www.snagajob.com/api/jobs/v1/search
        ?keywords=<text>
        &location=<City, ST>
        &radius=<miles>
        &pageSize=<n>
        &pageNumber=<1..>
        &IncludeExpired=true

    Headers: Accept: application/json — TLS fingerprint matters
    (Snagajob's edge rejects vanilla `requests` with 403 but accepts
    curl_cffi's chrome120 impersonation cleanly).

Response shape:

    {"actualTotal": <int>, "list": [<job>, ...], ...}

Per-job fields used:
  - ``postingId``           → JobPost.id "snag-<id>" + URL
  - ``title``, ``companyName``
  - ``location``            → {city, stateProvinceCode, postalCode}
  - ``wages``               → {text, median, wageType} structured pay
  - ``categories[0]``       → "Full-time" / "Part-time" / etc.
  - ``industries[0]``       → company_industry
  - ``createdDate``, ``updateDate``  → ISO 8601, prefer createdDate
  - ``isExpired``, ``isContractor``  → filter expired, classify contract
  - ``isOneClick``, ``isEasyApply``  → not surfaced (no easy_apply UI flag)
  - ``fextures``            → tagged skills/perks; used as description preview

User-facing job URL: ``https://www.snagajob.com/jobs/<postingId>``
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from curl_cffi import requests as cc_requests

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

log = create_logger("Snagajob")

_API_URL = "https://www.snagajob.com/api/jobs/v1/search"
_JOB_URL_TMPL = "https://www.snagajob.com/jobs/{posting_id}"
_TIMEOUT_S = 25
_PAGE_SIZE = 20

_JOB_TYPE_MAP = {
    "full-time": JobType.FULL_TIME,
    "fulltime": JobType.FULL_TIME,
    "part-time": JobType.PART_TIME,
    "parttime": JobType.PART_TIME,
    "contract": JobType.CONTRACT,
    "internship": JobType.INTERNSHIP,
    "seasonal": JobType.TEMPORARY,
    "temporary": JobType.TEMPORARY,
}


class Snagajob(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.SNAGAJOB, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        wanted = scraper_input.results_wanted
        jobs: list[JobPost] = []
        seen_ids: set[str] = set()

        # Snagajob paginates server-side. We walk pages until wanted or empty.
        page = 1 + (scraper_input.offset // _PAGE_SIZE)
        while len(jobs) < wanted:
            params: dict[str, Any] = {
                "keywords": scraper_input.search_term or "",
                "location": scraper_input.location or "",
                "radius": scraper_input.distance or 50,
                "pageSize": _PAGE_SIZE,
                "pageNumber": page,
                "IncludeExpired": "false",
            }
            try:
                r = cc_requests.get(
                    _API_URL,
                    params=params,
                    headers={"Accept": "application/json"},
                    impersonate="chrome120",
                    timeout=_TIMEOUT_S,
                )
            except Exception as e:  # noqa: BLE001
                log.error(f"Snagajob: request failed page={page}: {e}")
                break
            if not r.ok:
                log.error(f"Snagajob: status {r.status_code} on page {page}")
                break
            try:
                payload = r.json()
            except ValueError:
                log.error("Snagajob: non-JSON response")
                break

            items = payload.get("list") or []
            if not items:
                break

            new_this_page = 0
            for item in items:
                post = _build_jobpost(item, scraper_input.country)
                if post is None or post.id in seen_ids:
                    continue
                seen_ids.add(post.id)
                jobs.append(post)
                new_this_page += 1
                if len(jobs) >= wanted:
                    break

            log.info(f"Snagajob: page {page} → {new_this_page} new (total {len(jobs)})")

            # Stop walking if API said we're past the dataset
            actual_total = payload.get("actualTotal") or 0
            if actual_total and page * _PAGE_SIZE >= actual_total:
                break
            # Defensive cap — Snagajob occasionally returns repeats forever
            if page > 25:
                break
            page += 1

        log.info(f"Snagajob: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


def _build_jobpost(item: dict, country: Country | None) -> JobPost | None:
    try:
        posting_id = item.get("postingId")
        if not posting_id:
            return None
        if item.get("isExpired"):
            return None

        title = (item.get("title") or "").strip() or None
        if not title:
            return None
        title = " ".join(title.split())

        company = (item.get("companyName") or "").strip() or None
        loc_obj = _build_location(item.get("location"), country)

        compensation = _build_compensation(item.get("wages"))
        date_posted = _parse_iso_date(item.get("createdDate") or item.get("updateDate"))

        # Job type from categories field
        job_types: list[JobType] = []
        for cat in item.get("categories") or []:
            normalized = cat.replace(" ", "").lower()
            mapped = _JOB_TYPE_MAP.get(normalized)
            if mapped and mapped not in job_types:
                job_types.append(mapped)
        if item.get("isContractor") and JobType.CONTRACT not in job_types:
            job_types.append(JobType.CONTRACT)

        industries = item.get("industries") or []
        industry = industries[0] if industries else None

        # Use fextures (Snagajob's skill/feature tags) as a description preview.
        # These are short tags like ["cashier", "medicalbenefits", "weekends"].
        fextures = item.get("fextures") or []
        description = ", ".join(fextures) if fextures else None

        return JobPost(
            id=f"snag-{posting_id}",
            title=title,
            company_name=company,
            location=loc_obj,
            description=description,
            date_posted=date_posted,
            job_url=_JOB_URL_TMPL.format(posting_id=posting_id),
            compensation=compensation,
            job_type=job_types or None,
            company_industry=industry,
            company_logo=item.get("logoUrl"),
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"Snagajob: skipping malformed item: {e}")
        return None


def _build_location(raw: Any, country: Country | None) -> Location | None:
    if not isinstance(raw, dict):
        return None
    city = (raw.get("city") or "").strip() or None
    state = (raw.get("stateProvinceCode") or raw.get("stateProvince") or "").strip() or None
    if not city and not state:
        # Fall back to flattened locationName "Atlanta, GA 30303"
        loc_name = (raw.get("locationName") or "").strip()
        if loc_name:
            parts = [p.strip() for p in loc_name.split(",")]
            city = parts[0] if parts else None
            if len(parts) > 1:
                # "GA 30303" → state = GA
                state = parts[1].split()[0] if parts[1] else None
    if not city and not state:
        return None
    return Location(city=city, state=state, country=country or Country.USA)


def _build_compensation(raw: Any) -> Compensation | None:
    if not isinstance(raw, dict):
        return None
    # Snagajob wages: median (only), or min+max, or text only. wageType=1 hourly,
    # 2 weekly, 3 monthly, 4 yearly per their API docs.
    interval_lookup = {
        1: CompensationInterval.HOURLY,
        2: CompensationInterval.WEEKLY,
        3: CompensationInterval.MONTHLY,
        4: CompensationInterval.YEARLY,
    }
    interval = interval_lookup.get(raw.get("wageType"))
    mn = raw.get("min")
    mx = raw.get("max")
    median = raw.get("median")
    if mn is None and mx is None and median is None:
        return None
    if mn is None:
        mn = median
    if mx is None:
        mx = median
    try:
        mn_f = float(mn) if mn is not None else None
        mx_f = float(mx) if mx is not None else None
    except (TypeError, ValueError):
        return None
    if mn_f is None and mx_f is None:
        return None
    # Default unknown interval to hourly — most Snagajob roles are hourly.
    if interval is None:
        interval = CompensationInterval.HOURLY
    return Compensation(
        interval=interval,
        min_amount=mn_f,
        max_amount=mx_f,
        currency="USD",
    )


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None
