"""CollabWork scraper — community/newsletter job aggregator API.

CollabWork (collabwork.com) is a B2B "AI Visibility & Employer
Discoverability" platform — they distribute their customers' job
postings into AI search results, niche newsletters, and Slack
communities. The marketing site has no public listings, but their
candidate-facing app at ``app.collabwork.com`` exposes the entire
~157K-job catalogue through a clean POST API.

## Endpoint

    POST https://app.collabwork.com/api/jobs/search
    Content-Type: application/json
    {
      "search_query": "<keyword>",   # NOT 'search'/'q'/'keyword' — those are silently ignored
      "location":     "<city|state|empty>",
      "page":         <int, 1-indexed>,
      "page_size":    <int, max 100>,
      "filters":      []
    }

Response shape:
    {
      "hits": [<job>...],
      "totalHits": int,
      "totalPages": int,
      "hasNextPage": bool,
      "hasPrevPage": bool,
      "page": int
    }

## Per-job fields used

  - ``id``, ``title``, ``company``
  - ``location`` (list[str]) + ``location_string`` (sometimes missing)
  - ``is_remote`` (bool)
  - ``employment_type`` — "FULL_TIME" / "PART_TIME" / "CONTRACT"
  - ``posted_at_timestamp`` — epoch ms (can be future-dated for
    employer's "starts in 28 days" listings)
  - ``salary_min`` / ``salary_max`` / ``salary_period`` ("Yearly"/"Hourly")
  - ``application_url`` — direct apply, often via ad-network redirects
    (de.jobsyn.org, click.appcast.io). Pass through as-is.
  - ``description`` — full text
  - ``industry``, ``sector`` — surfaced as company_industry

## Freshness

CollabWork's catalogue includes postings spanning months. We default to
a 90-day freshness cap (matches Greenhouse pattern); callers override
with ``hours_old``. Future-dated postings (employer pre-listing a role
that starts later) are kept — their `posted_at_timestamp` is in the
future, which is below the cutoff threshold by definition.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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

log = create_logger("CollabWork")

_API = "https://app.collabwork.com/api/jobs/search"
_TIMEOUT_S = 20
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_MAX_PAGE_SIZE = 100  # API caps perPage at 100; 200 is silently truncated.
_DEFAULT_MAX_AGE_DAYS = 90


class CollabWork(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.COLLAB_WORK, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self._ua = user_agent or _DEFAULT_UA

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted
        country = scraper_input.country
        cutoff = _resolve_cutoff(scraper_input)
        # Empirical caveats:
        #   1. Empty location ("") caps at 100 results regardless of total
        #      catalog size — the API treats this as a default-IP geo-search.
        #      Pass a real city/state for larger pulls.
        #   2. The location field is matched as a literal substring against
        #      the catalog's stored ``location`` strings (which use the
        #      space-form "Atlanta GA US"). Sending "Atlanta, GA" (comma +
        #      state) returns 0 hits because that exact phrase isn't in any
        #      record. Strip commas to convert standard JobSpy location
        #      input ("Atlanta, GA") into the API-friendly form.
        #   3. is_remote is ignored at the API layer (we tried multiple
        #      filter shapes — all silently dropped). We filter client-side
        #      below using a heuristic that inspects title + location text.
        loc_input = (scraper_input.location or "").strip()
        # "Atlanta, GA" → "Atlanta GA"; preserves city + state, drops comma.
        api_location = " ".join(part.strip() for part in loc_input.split(",") if part.strip())
        want_remote = bool(getattr(scraper_input, "is_remote", False))
        log.info(
            f"CollabWork: query={scraper_input.search_term!r} "
            f"location={scraper_input.location!r} cutoff={cutoff.isoformat()} "
            f"remote_only={want_remote}"
        )

        jobs: list[JobPost] = []
        seen_ids: set[str] = set()
        page = 1
        # Defensive cap — at 100/page that's 1500 jobs, well above any
        # reasonable single-call request.
        while len(jobs) < wanted and page <= 15:
            page_size = min(_MAX_PAGE_SIZE, max(wanted - len(jobs), 24) + 5)
            body: dict[str, Any] = {
                "search_query": scraper_input.search_term or "",
                "location": api_location,
                "page": page,
                "page_size": page_size,
                "filters": [],
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
                log.error(f"CollabWork: request failed at page {page}: {e}")
                break
            if not r.ok:
                log.error(f"CollabWork: HTTP {r.status_code} at page {page}: {r.text[:200]}")
                break

            try:
                payload = r.json()
            except ValueError:
                log.error("CollabWork: non-JSON response")
                break

            hits = payload.get("hits") or []
            if not hits:
                break

            dropped_old = 0
            for raw in hits:
                if not isinstance(raw, dict):
                    continue
                rid = raw.get("id")
                if not rid or rid in seen_ids:
                    continue
                seen_ids.add(rid)

                # Freshness gate. The API exposes the timestamp as
                # ``posted_at`` (epoch ms). Older response versions used
                # ``posted_at_timestamp``; we honor both for safety.
                # Missing or future-dated timestamps pass through (we keep
                # forward-listed jobs employers haven't opened yet).
                ts_ms = raw.get("posted_at")
                if ts_ms is None:
                    ts_ms = raw.get("posted_at_timestamp")
                if isinstance(ts_ms, (int, float)) and ts_ms > 0:
                    posted_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                    if posted_dt < cutoff:
                        dropped_old += 1
                        continue

                # Client-side remote filter — the API ignores this AND the
                # per-job ``is_remote`` field is reliably False in the data
                # (verified empirically across 50+ samples). Use the
                # heuristic that ALSO inspects title + location text.
                if want_remote and not _classify_remote(raw):
                    continue

                post = _build_jobpost(raw, country)
                if post is not None:
                    jobs.append(post)
                    if len(jobs) >= wanted:
                        break

            if dropped_old:
                log.info(f"CollabWork: page {page} filtered {dropped_old} stale postings")

            # Trust the server's pagination metadata.
            if not payload.get("hasNextPage"):
                break
            page += 1

        log.info(f"CollabWork: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


def _resolve_cutoff(si: ScraperInput) -> datetime:
    hours = getattr(si, "hours_old", None)
    if hours and hours > 0:
        return datetime.now(timezone.utc) - timedelta(hours=hours)
    return datetime.now(timezone.utc) - timedelta(days=_DEFAULT_MAX_AGE_DAYS)


def _build_jobpost(raw: dict, country: Country | None) -> JobPost | None:
    rid = raw.get("id")
    title = (raw.get("title") or "").strip()
    if not rid or not title:
        return None
    title = " ".join(title.split())

    # Location: location_string is preferred, fall back to first entry of
    # location[] which is a list of strings like "Atlanta GA US".
    loc_text = (raw.get("location_string") or "").strip()
    if not loc_text:
        loc_list = raw.get("location") or []
        if loc_list and isinstance(loc_list, list):
            loc_text = str(loc_list[0]).strip()
    location_obj = _parse_location(loc_text, country)

    posted = _parse_posted(raw.get("posted_at") or raw.get("posted_at_timestamp"))
    job_type_list = _map_employment_type(raw.get("employment_type"))
    compensation = _build_compensation(
        raw.get("salary_min"),
        raw.get("salary_max"),
        raw.get("salary_period"),
    )
    industry = (raw.get("industry") or raw.get("sector") or None)

    return JobPost(
        id=f"cw-{rid}",
        title=title,
        company_name=(raw.get("company") or "").strip() or None,
        location=location_obj,
        description=(raw.get("description") or "").strip() or None,
        job_url=raw.get("application_url") or "",
        date_posted=posted,
        is_remote=_classify_remote(raw),
        compensation=compensation,
        job_type=job_type_list,
        company_industry=industry,
    )


def _classify_remote(raw: dict) -> bool:
    """Detect remote-ness from a CollabWork hit.

    The structured ``is_remote`` field is reliably False in this data
    (verified empirically), so we union three signals:
      1. ``is_remote`` field == True (rare)
      2. title contains "remote" (e.g. "Remote Licensed Therapist")
      3. any entry in ``location`` list contains "remote" (case-insensitive)
    """
    if raw.get("is_remote") is True:
        return True
    title = (raw.get("title") or "").lower()
    if "remote" in title:
        return True
    for loc in raw.get("location") or []:
        if isinstance(loc, str) and "remote" in loc.lower():
            return True
    return False


def _parse_location(text: str, country: Country | None) -> Location | None:
    """Parse CollabWork's location strings.

    Observed shapes (in order of frequency):
      "Atlanta GA US"                        — single space-form
      "Athens GA US, Atlanta GA US"          — multi-location comma-joined
      "Atlanta, GA"                          — comma-form (older entries)
      "Atlanta, USA" / ""                    — partial / empty

    For multi-location strings (e.g. a job posted in two cities), we keep
    the first segment as the canonical location. Detection: when each
    comma-separated segment ends with a 2-3 char token (the country
    code), it's a multi-location string, not a comma-form city/state.
    """
    if not text:
        return Location(country=country or Country.USA)

    if "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
        # Multi-location heuristic: every segment ends with a 2-3 char
        # token (country code like "US"). Take the first.
        if len(parts) >= 2 and all(
            p.split() and len(p.split()[-1]) <= 3 for p in parts
        ):
            text = parts[0]
            # Fall through to space-form parsing.
        else:
            # Comma-form: "City, ST" or "City, Country"
            city = parts[0] if parts else None
            state = parts[1] if len(parts) > 1 else None
            return Location(
                city=city or None,
                state=state or None,
                country=country or Country.USA,
            )

    # Space form. Trailing token is usually country code; second-to-last
    # is state code (2 chars).
    tokens = text.split()
    if len(tokens) >= 3 and len(tokens[-1]) <= 3:
        # "Atlanta GA US" → city = "Atlanta", state = "GA"
        state = tokens[-2] if len(tokens[-2]) <= 3 else None
        city = " ".join(tokens[:-2]) if state else " ".join(tokens[:-1])
    elif len(tokens) >= 2 and len(tokens[-1]) <= 3:
        # "Atlanta GA" → city + state
        state = tokens[-1]
        city = " ".join(tokens[:-1])
    else:
        city = text
        state = None
    return Location(
        city=(city or None),
        state=(state or None),
        country=country or Country.USA,
    )


def _parse_posted(ts_ms: Any) -> date | None:
    if not isinstance(ts_ms, (int, float)) or ts_ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
    except (ValueError, OSError, OverflowError):
        return None


def _map_employment_type(value: Any) -> list[JobType] | None:
    if not isinstance(value, str):
        return None
    v = value.strip().upper()
    if v in ("FULL_TIME", "FULLTIME", "FULL TIME"):
        return [JobType.FULL_TIME]
    if v in ("PART_TIME", "PARTTIME", "PART TIME"):
        return [JobType.PART_TIME]
    if v in ("CONTRACT", "CONTRACTOR"):
        return [JobType.CONTRACT]
    if v in ("INTERNSHIP", "INTERN"):
        return [JobType.INTERNSHIP]
    if v in ("TEMPORARY", "TEMP"):
        return [JobType.TEMPORARY]
    return None


def _build_compensation(
    sal_min: Any, sal_max: Any, period: Any,
) -> Compensation | None:
    """CollabWork stores salary_min/max as integers (0 when not provided)
    and ``salary_period`` as ``"Yearly"`` / ``"Hourly"`` / etc.
    """
    try:
        lo = float(sal_min) if isinstance(sal_min, (int, float)) and sal_min else None
        hi = float(sal_max) if isinstance(sal_max, (int, float)) and sal_max else None
    except (ValueError, TypeError):
        lo, hi = None, None
    if lo is None and hi is None:
        return None
    p = (period or "").strip().lower() if isinstance(period, str) else ""
    if p == "yearly" or p == "annual":
        interval = CompensationInterval.YEARLY
    elif p == "hourly":
        interval = CompensationInterval.HOURLY
    elif p == "monthly":
        interval = CompensationInterval.MONTHLY
    elif p == "weekly":
        interval = CompensationInterval.WEEKLY
    elif p == "daily":
        interval = CompensationInterval.DAILY
    else:
        # Heuristic fallback when period is missing: < 1k = hourly, else yearly.
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
