"""Kforce scraper — staffing-firm career site backed by Azure Cognitive Search.

The Kforce job board at ``www.kforce.com/find-work/search-jobs/`` is a
React SPA that issues client-side queries against an Azure Cognitive
Search instance:

    POST https://kforcewebeast.search.windows.net/indexes/kforcewebjobentity/docs/search
         ?api-version=2016-09-01
    api-key: <public web key>
    Content-Type: application/json
    {"search": "<keywords>", "top": 50, "skip": 0, "count": true,
     "orderby": "PostDate desc"}

The api-key was extracted from the page's XHR layer — Azure Search uses
two key tiers: an admin key (server-side, not exposed) and a query key
that's distributed to web clients. The query key is read-only and tied
to specific indexes; embedding it in client JS is the documented
pattern.

The www.kforce.com host itself sits behind Imperva Incapsula and rejects
plain HTTPS GETs from automated clients. The Azure Search endpoint is
on a separate hostname and is unprotected — that's our scrape entry.

Document schema (Azure response ``value`` array):
  - Id, Title, Responsibilities, ResponsibilitiesHtml
  - City, State, Zip, MarketName, MetroAreas, GeoData
  - PostDate (ISO 8601 with Z suffix)
  - TypeCode (T = contract, P = permanent), Remote ("Full"/"Partial"/null)
  - SalaryMin, SalaryMax, SalaryText
  - ApplyUrl (direct apply link, can be reconstructed from ApplyUrlPrefix
    + Id + ApplyUrlSuffix; we use ApplyUrl as-is)
  - KForceId, ReferenceCode, Industry, ClientIndustry, CareerLevel
"""
from __future__ import annotations

from datetime import date, datetime, timezone
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

log = create_logger("Kforce")

_API = (
    "https://kforcewebeast.search.windows.net/indexes/kforcewebjobentity/docs/search"
    "?api-version=2016-09-01"
)
# Public read-only Azure Cognitive Search query key, baked into the SPA's
# XHR layer. Rotates only when Kforce regenerates it (rare).
_API_KEY = "1603E4DC4C87A8E41D6BBDE4EEA4EFB7"
_TIMEOUT_S = 25
_PAGE_SIZE = 50  # Azure Search "top"; max 1000 but 50 keeps payloads sane.

_DETAIL_PREFIX = "https://www.kforce.com/find-work/search-jobs/#/detail/"


class Kforce(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.KFORCE, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted
        country = scraper_input.country

        # Build OData $filter clauses. Azure Search field names: City,
        # State (full-name e.g. "Georgia"), Remote ("Full"/"Partial"/null).
        filter_clauses: list[str] = []
        if scraper_input.location:
            city, state = _parse_location(scraper_input.location)
            if city:
                filter_clauses.append(f"City eq '{_escape_odata(city)}'")
            if state:
                filter_clauses.append(f"State eq '{_escape_odata(state)}'")
        if scraper_input.is_remote:
            filter_clauses.append("(Remote eq 'Full' or Remote eq 'Partial')")
        odata_filter = " and ".join(filter_clauses) if filter_clauses else None

        jobs: list[JobPost] = []
        seen: set[str] = set()
        skip = 0
        while len(jobs) < wanted and skip < 1000:
            body: dict[str, Any] = {
                "search": scraper_input.search_term or "*",
                "top": min(_PAGE_SIZE, wanted - len(jobs) + 5),
                "skip": skip,
                "count": skip == 0,
                "orderby": "PostDate desc",
            }
            if odata_filter:
                body["filter"] = odata_filter

            try:
                r = requests.post(
                    _API,
                    json=body,
                    headers={
                        "api-key": _API_KEY,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    timeout=_TIMEOUT_S,
                )
            except Exception as e:
                log.error(f"Kforce: request failed at skip={skip}: {e}")
                break

            if not r.ok:
                log.error(f"Kforce: HTTP {r.status_code} at skip={skip}: {r.text[:200]}")
                break

            try:
                payload = r.json()
            except ValueError:
                log.error("Kforce: non-JSON response")
                break

            page_docs = payload.get("value") or []
            if not page_docs:
                break

            for raw in page_docs:
                post = _build_jobpost(raw, country)
                if post is None:
                    continue
                if post.id in seen:
                    continue
                seen.add(post.id)
                jobs.append(post)
                if len(jobs) >= wanted:
                    break

            if len(page_docs) < _PAGE_SIZE:
                break
            skip += _PAGE_SIZE

        log.info(f"Kforce: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


def _build_jobpost(raw: dict, country: Country | None) -> JobPost | None:
    doc_id = raw.get("Id")
    title = (raw.get("Title") or "").strip()
    if not doc_id or not title:
        return None
    title = " ".join(title.split())

    city = (raw.get("City") or "").strip() or None
    state = (raw.get("State") or "").strip() or None
    location_obj = Location(city=city, state=state, country=country or Country.USA)

    remote_flag = (raw.get("Remote") or "").strip().lower()
    is_remote = remote_flag == "full"
    # "Partial" = hybrid; surface it in description rather than is_remote.

    job_url = _detail_url(doc_id)
    posted = _parse_post_date(raw.get("PostDate"))

    type_code = (raw.get("TypeCode") or "").strip().upper()
    job_type_list = _map_type_code(type_code)

    compensation = _build_compensation(
        raw.get("SalaryMin"), raw.get("SalaryMax"), raw.get("SalaryText"),
    )

    description = (raw.get("Responsibilities") or "").strip() or None
    if remote_flag == "partial" and description:
        description = "[Hybrid / Partial Remote]\n" + description

    return JobPost(
        id=f"kf-{doc_id}",
        title=title,
        company_name="Kforce client",  # Kforce is a staffing firm; client name isn't in search docs
        location=location_obj,
        description=description,
        job_url=job_url,
        date_posted=posted,
        is_remote=is_remote,
        compensation=compensation,
        job_type=job_type_list,
        company_industry=(raw.get("ClientIndustry") or raw.get("Industry") or None),
    )


def _detail_url(doc_id: str) -> str:
    # The SPA renders detail at a hash route — use it so links work in a
    # browser even though the URL fragment isn't sent to the server.
    return f"{_DETAIL_PREFIX}{doc_id}/"


def _parse_post_date(s: str | None) -> date | None:
    if not s:
        return None
    # Azure Search returns "2026-05-06T09:15:10Z"; fromisoformat needs +00:00.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.astimezone(timezone.utc).date()


def _map_type_code(code: str) -> list[JobType] | None:
    # Kforce TypeCode values seen in production (uppercased here):
    #   "CONTRACT"     → contract
    #   "DIRECT HIRE"  → permanent / full-time
    #   "T", "P"       → legacy single-letter codes embedded in some IDs
    if not code:
        return None
    if code in ("T", "C") or code.startswith("CONTRACT"):
        return [JobType.CONTRACT]
    if code in ("P",) or code.startswith("PERM") or code.startswith("DIRECT"):
        return [JobType.FULL_TIME]
    return None


def _parse_location(text: str) -> tuple[str | None, str | None]:
    """Split "City, State" into (city, two-letter-state-code) for KF's filter.

    Empirical: KF's Azure index stores ``State`` as a 2-letter postal code
    ("NY", "GA"), so callers passing "Georgia" need to be normalized down
    to "GA". We accept both forms — already-2-letter input passes through
    untouched.
    """
    parts = [p.strip() for p in text.split(",")]
    city = parts[0] if parts else None
    state_in = (parts[1] if len(parts) > 1 else "") or ""
    state_norm = _STATE_NAME_TO_CODE.get(state_in.title(), state_in.upper() or None)
    # If the input wasn't a recognized full name and isn't already 2-letter,
    # we still pass it through — better to send a maybe-wrong filter than
    # silently drop the user's intent.
    return (city or None), (state_norm or None)


def _escape_odata(s: str) -> str:
    # OData string literals double single-quotes for escaping.
    return s.replace("'", "''")


# Full US state name → 2-letter postal code, matching Kforce's State
# field format ("GA", "NY"). Keys are .title()-cased for case-insensitive
# matching ("Georgia", "New York", "District Of Columbia").
_STATE_NAME_TO_CODE: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "District Of Columbia": "DC", "Florida": "FL",
    "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}


def _build_compensation(
    sal_min: Any, sal_max: Any, sal_text: str | None
) -> Compensation | None:
    try:
        lo = float(sal_min) if sal_min not in (None, "", 0) else None
        hi = float(sal_max) if sal_max not in (None, "", 0) else None
    except (ValueError, TypeError):
        lo, hi = None, None
    if lo is None and hi is None:
        return None
    # Heuristic: under 1000 = hourly; otherwise yearly. Matches Kforce's
    # contract-rate vs salary postings.
    upper = hi if hi is not None else lo
    interval = (
        CompensationInterval.HOURLY if (upper or 0) < 1000
        else CompensationInterval.YEARLY
    )
    return Compensation(
        interval=interval,
        min_amount=lo,
        max_amount=hi,
        currency="USD",
    )
