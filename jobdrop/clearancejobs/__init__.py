"""ClearanceJobs scraper — security-clearance-required job marketplace.

ClearanceJobs (a DHI service) is the dominant US marketplace for jobs
that require security clearance. The site is a Vue 3 SPA backed by a
clean JSON API at ``/api/v1/jobs/search`` accepting POST + JSON body.

Discovered request shape (no auth required for search):

    POST https://www.clearancejobs.com/api/v1/jobs/search
    Content-Type: application/json
    {"keywords": "...", "page": 1, "perPage": 20, "sort": "date_desc",
     "filters": {"city": "Atlanta", "state": "Georgia"}}

Response shape (top level):

    {"data": [<job>...], "meta": {"pagination": {...}, ...}}

Per-job fields used:
  - ``id``                — int, becomes JobPost.id "cj-<id>"
  - ``job_name``          — title
  - ``job_url``           — absolute URL
  - ``created_at``        — ISO 8601 (preferred date_posted source)
  - ``updated_at``        — ISO 8601 (fallback)
  - ``company_name``      — employer name (may be "Name Hidden")
  - ``locations[0]``      — {location: "City, ST", type: "Remote"|"On-Site/Office"|"Hybrid"}
  - ``career_level``      — "5+ yrs exp", "Mid Career", etc. — surfaced as job_level
  - ``clearance``         — "Secret", "Top Secret", "TS/SCI", "Intel Agency", etc.
  - ``polygraph``         — "Full Scope Polygraph", "CI Polygraph", "None"
  - ``preview_text``      — first ~200 chars of the JD; full body needs detail-page fetch

Clearance/polygraph requirements are appended to the description preview
so downstream consumers (the MCP search tool) can filter on them.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any

import requests

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

log = create_logger("ClearanceJobs")

_API = "https://www.clearancejobs.com/api/v1/jobs/search"
_DETAIL_API = "https://www.clearancejobs.com/api/v1/jobs/{id}"
_TIMEOUT_S = 25
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# The CJ API hard-caps perPage at 20 regardless of what we send (verified
# empirically — sending perPage=50 still returns 20). Pagination total can
# exceed 2000 pages on broad keywords; we walk until results_wanted is met.
_PAGE_SIZE = 20

# Concurrency for detail-page fetches. CJ's detail endpoint is fast (~100ms
# per call) and unauthenticated; 5 parallel workers stays well below any
# observed rate limit and gets a 50-job pull from ~5s sequential to ~1s.
_DETAIL_WORKERS = 5


class ClearanceJobs(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.CLEARANCE_JOBS, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self._ua = user_agent or _DEFAULT_UA

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted
        country = scraper_input.country

        # NOTE on filtering: CJ's faceted filters (city, state, remote,
        # clearance) require IDs from their dropdown facet endpoints, not
        # free-text names. Sending free-text filter values returns 200 but
        # silently ignores the filter — verified for city/state and remote.
        # Until we wire up the facet IDs, location and is_remote inputs
        # are intentionally not applied at the API layer; callers should
        # refine results client-side or scope by keyword.
        raw_jobs: list[dict] = []
        seen: set[int] = set()
        page = 1
        # Cap pagination defensively — at 20/page that's 1000 results,
        # well above any reasonable single-call request.
        while len(raw_jobs) < wanted and page <= 50:
            body: dict[str, Any] = {
                "keywords": scraper_input.search_term or "",
                "page": page,
                "perPage": _PAGE_SIZE,
                "sort": "date_desc",
            }

            try:
                r = requests.post(
                    _API,
                    json=body,
                    headers={
                        "User-Agent": self._ua,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    timeout=_TIMEOUT_S,
                )
            except Exception as e:
                log.error(f"ClearanceJobs: request failed on page {page}: {e}")
                break

            if r.status_code == 422:
                log.error(
                    f"ClearanceJobs: 422 validation failed; body={body} "
                    f"resp={r.text[:300]}"
                )
                break
            if not r.ok:
                log.error(f"ClearanceJobs: HTTP {r.status_code} on page {page}")
                break

            try:
                payload = r.json()
            except ValueError:
                log.error("ClearanceJobs: non-JSON response")
                break

            page_jobs = payload.get("data") or []
            if not page_jobs:
                break

            for raw in page_jobs:
                jid = raw.get("id")
                if jid is None or jid in seen:
                    continue
                seen.add(jid)
                raw_jobs.append(raw)
                if len(raw_jobs) >= wanted:
                    break

            # Trust the server's pagination metadata: if there's no
            # next_page, we've reached the end. Otherwise advance.
            pagination = (payload.get("meta") or {}).get("pagination") or {}
            next_page = pagination.get("next_page")
            if not next_page:
                break
            page = next_page

        # Search results give us only a 200-char preview, no salary, no
        # job_type. Hit the detail endpoint per job in parallel to enrich.
        details = _fetch_details_bulk([r["id"] for r in raw_jobs], self._ua)

        jobs: list[JobPost] = []
        for raw in raw_jobs:
            detail = details.get(raw.get("id")) or {}
            post = _build_jobpost(raw, detail, country)
            if post is not None:
                jobs.append(post)

        log.info(f"ClearanceJobs: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


def _fetch_details_bulk(job_ids: list[int], ua: str) -> dict[int, dict]:
    """Fetch CJ detail pages concurrently. Returns {job_id: detail_dict}.

    Failures (network errors, 404s) are silently skipped — caller falls
    back to search-page data when an entry is missing.
    """
    if not job_ids:
        return {}
    out: dict[int, dict] = {}
    headers = {"User-Agent": ua, "Accept": "application/json"}

    def fetch_one(jid: int) -> tuple[int, dict | None]:
        try:
            r = requests.get(
                _DETAIL_API.format(id=jid),
                headers=headers,
                timeout=_TIMEOUT_S,
            )
            if r.ok:
                return jid, r.json()
        except Exception as e:
            log.debug(f"ClearanceJobs detail fetch {jid} failed: {e}")
        return jid, None

    with ThreadPoolExecutor(max_workers=_DETAIL_WORKERS) as ex:
        for fut in as_completed([ex.submit(fetch_one, jid) for jid in job_ids]):
            jid, payload = fut.result()
            if payload is not None:
                out[jid] = payload
    if len(out) < len(job_ids):
        log.info(
            f"ClearanceJobs: detail fetch hit {len(out)}/{len(job_ids)} — "
            "falling back to search data for the rest"
        )
    return out


def _split_city_state(text: str) -> tuple[str | None, str | None]:
    parts = [p.strip() for p in text.split(",")]
    city = parts[0] if parts else None
    state = parts[1] if len(parts) > 1 else None
    return (city or None), (state or None)


def _build_jobpost(
    raw: dict, detail: dict, country: Country | None
) -> JobPost | None:
    """Build a JobPost from search-result ``raw`` enriched with optional
    detail-page ``detail`` (may be empty dict if the detail fetch failed).

    Field-precedence rules: prefer detail-page values where present, fall
    back to search-page values, and never crash on either being missing.
    """
    job_id = raw.get("id")
    title = (raw.get("job_name") or detail.get("job_name") or "").strip()
    if not job_id or not title:
        return None

    title = " ".join(title.split())

    # Location + remote classification.
    #
    # CJ's data model is messy — a single posting can have multiple
    # location entries with different ``type`` values, and "fully remote"
    # is encoded inconsistently. Empirical taxonomy across ~420 sampled
    # jobs:
    #   type == "On-Site/Office"   → on-site
    #   type == "Off-Site/Hybrid"  → hybrid (treat as not-fully-remote)
    #   type == "No Preference"    → location field describes it
    #   type == ""                 → loosely-typed; check location text
    # Plus title-level signals: "Foo Engineer (Remote)" and location text
    # like "Remote/Hybrid", "Remote", or bare "United States" with no city.
    #
    # We classify by union: ANY signal pointing at fully-remote wins.
    locs = detail.get("locations") or raw.get("locations") or []
    location_obj: Location | None = None
    if locs:
        first = locs[0] or {}
        loc_text = (first.get("location") or "").strip()
        city, state = _split_city_state(loc_text)
        location_obj = Location(
            city=city,
            state=state,
            country=country or Country.USA,
        )

    # Detail page exposes ``remote`` as a real boolean — authoritative.
    # Fall back to text-based heuristic if the detail fetch failed.
    detail_remote = detail.get("remote")
    if isinstance(detail_remote, bool):
        is_remote = detail_remote
        hybrid_flag = False
    else:
        is_remote, hybrid_flag = _classify_remote(title, locs)

    posted = (
        _parse_iso_date(detail.get("posted_at"))
        or _parse_iso_date(raw.get("created_at"))
        or _parse_iso_date(raw.get("updated_at"))
    )

    # Clearance + polygraph are CJ's distinguishing metadata. Surface them
    # in the description preview so the MCP search tool's text search hits
    # them naturally.
    full_desc = (detail.get("description") or "").strip()
    preview = (raw.get("preview_text") or "").strip()
    body = full_desc or preview
    clearance = (
        (detail.get("clearance_text") or detail.get("clearance") or "")
        if isinstance(detail.get("clearance_text") or detail.get("clearance"), str)
        else (raw.get("clearance") or "")
    ).strip()
    polygraph = (raw.get("polygraph") or "").strip()
    description_parts: list[str] = []
    if clearance:
        description_parts.append(f"[Clearance: {clearance}]")
    # Skip the polygraph tag when it's null/none/not-specified — those
    # values aren't useful signal and just clutter the description.
    if polygraph and polygraph.lower() not in ("none", "not specified", "n/a"):
        description_parts.append(f"[Polygraph: {polygraph}]")
    # Surface hybrid status in the description so it's discoverable even
    # though is_remote is binary.
    if hybrid_flag:
        description_parts.append("[Hybrid]")
    if body:
        description_parts.append(body)
    description = "\n".join(description_parts) or None

    job_type_list = _map_detail_job_type(detail.get("job_type"))
    compensation = _build_compensation(
        detail.get("salary_min"),
        detail.get("salary_max"),
        detail.get("salary"),
    )

    return JobPost(
        id=f"cj-{job_id}",
        title=title,
        company_name=(
            detail.get("company_name") or raw.get("company_name") or ""
        ).strip() or None,
        location=location_obj,
        description=description,
        job_url=detail.get("job_url") or raw.get("job_url") or "",
        date_posted=posted,
        is_remote=is_remote,
        job_level=(raw.get("career_level") or None),
        job_type=job_type_list,
        compensation=compensation,
    )


def _map_detail_job_type(value: Any) -> list[JobType] | None:
    """Map CJ detail's ``job_type`` field (a dict like
    ``{"id": "e", "value": "Employee"}``) to jobdrop's JobType enum.

    Observed ``id`` codes:
      - "e" Employee     → full-time
      - "c" Contractor/Consultant → contract
    Anything else is left None (so we don't lie about the classification).
    """
    if not isinstance(value, dict):
        return None
    code = (value.get("id") or "").strip().lower()
    label = (value.get("value") or "").strip().lower()
    if code == "e" or "employee" in label:
        return [JobType.FULL_TIME]
    if code == "c" or "contract" in label or "consultant" in label:
        return [JobType.CONTRACT]
    if "intern" in label:
        return [JobType.INTERNSHIP]
    return None


def _build_compensation(
    sal_min: Any, sal_max: Any, sal_text: Any,
) -> Compensation | None:
    """Build a Compensation from CJ detail-page salary fields.

    salary_min / salary_max are integers when populated, None otherwise.
    salary is a dict (e.g. {"value": "$150,000 and above"}) or None.
    Empty / "Not Specified" cases collapse to None.
    """
    try:
        lo = float(sal_min) if isinstance(sal_min, (int, float)) and sal_min else None
        hi = float(sal_max) if isinstance(sal_max, (int, float)) and sal_max else None
    except (ValueError, TypeError):
        lo, hi = None, None
    if lo is None and hi is None:
        return None
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


def _classify_remote(title: str, locs: list) -> tuple[bool, bool]:
    """Return (is_fully_remote, is_hybrid) by scanning every location.

    Title-level "(Remote)" suffix is a strong signal: CJ employers use it
    even when the structured ``type`` field is empty or set to "Hybrid".
    """
    is_remote = False
    is_hybrid = False
    title_lc = title.lower()
    if "(remote)" in title_lc or title_lc.endswith(" remote"):
        is_remote = True

    for loc in locs or []:
        if not isinstance(loc, dict):
            continue
        t = (loc.get("type") or "").strip().lower()
        text = (loc.get("location") or "").strip().lower()
        # Structured signal: ``Off-Site/Remote`` (rare) or any type that
        # collapses to plain "remote".
        if t == "remote" or t == "off-site/remote":
            is_remote = True
        elif "hybrid" in t:
            is_hybrid = True
        # Free-text fallback: location string says it.
        if "remote" in text and "hybrid" not in text:
            is_remote = True
        elif "remote" in text and "hybrid" in text:
            # "Remote/Hybrid" — ambiguous; flag both, prefer remote since
            # the listing is willing to be fully remote.
            is_remote = True
            is_hybrid = True
    # If we marked remote, hybrid becomes redundant noise.
    if is_remote:
        is_hybrid = False
    return is_remote, is_hybrid


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except (ValueError, TypeError):
        return None
