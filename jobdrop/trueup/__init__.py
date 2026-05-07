"""TrueUp scraper — direct backend search call. Headless via curl_cffi.

TrueUp's frontend at trueup.io is fronted by Cloudflare; the SPA is
client-rendered with no SSR data. Reverse-engineered from the public
client JS bundle: TrueUp uses a hosted search index. The credentials
are search-only (public by design) and exposed in the unminified JS.

Pure curl_cffi safari17_2_ios → sub-second response, no browser, no
GUI, no Cloudflare battle. Calls go to a third-party search domain
that TrueUp's own CF rules don't gate.

## Schema (per-hit)

  - title, company, company_short
  - location (str)
  - url (canonical apply link, ATS direct)
  - salary_range_min / salary_range_max (int|None)
  - description_tags (list — skill keywords)
  - trajectory_score (float|None — TrueUp's company-momentum metric)
  - valuation (float|None — $B)
  - date_founded (ISO date)
  - updated_at (ISO date)
  - company_description_plus (list — labels like ['Early-stage', '$18M raised'])
  - company_id, job_id, objectID
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from curl_cffi import requests as cc_requests

from jobdrop.model import (
    Compensation,
    CompensationInterval,
    JobPost,
    JobResponse,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobdrop.trueup.util import log

# Public client-side search-only credentials (extracted from the
# unminified TrueUp JS bundle, exposed by design per Algolia client SDK).
_APP_ID = "V00CGAZFSS"
_API_KEY = "e07ab2a8c02b4250f8bff7cbaf528b7f"
_HOST = f"https://{_APP_ID}-dsn.algolia.net"
_INDEX = "job"

_PER_PAGE = 50
_MAX_PAGES = 10
_TIMEOUT_S = 20


class TrueUp(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.TRUEUP, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self.user_agent = user_agent

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = max(scraper_input.results_wanted or 15, 1)

        sess = cc_requests.Session(impersonate="safari17_2_ios")

        jobs: list[JobPost] = []
        seen_ids: set[str] = set()

        # Build query string — title + location concat (Algolia tokenizes)
        query_parts: list[str] = []
        if scraper_input.search_term:
            query_parts.append(scraper_input.search_term)
        if scraper_input.location and not scraper_input.is_remote:
            query_parts.append(scraper_input.location)
        if scraper_input.is_remote:
            query_parts.append("remote")
        query = " ".join(query_parts).strip()

        filters: list[str] = []
        if scraper_input.hours_old:
            cutoff_ts = int(time.time()) - scraper_input.hours_old * 3600
            filters.append(f"updated_at_timestamp >= {cutoff_ts}")

        for page_num in range(_MAX_PAGES):
            params = {
                "query": query,
                "hitsPerPage": _PER_PAGE,
                "page": page_num,
            }
            if filters:
                params["filters"] = " AND ".join(filters)

            body = {"params": urlencode(params)}
            try:
                r = sess.post(
                    f"{_HOST}/1/indexes/{_INDEX}/query",
                    json=body,
                    headers={
                        "X-Algolia-Application-Id": _APP_ID,
                        "X-Algolia-API-Key": _API_KEY,
                        "Content-Type": "application/json",
                    },
                    timeout=_TIMEOUT_S,
                )
            except Exception as e:
                log.warning(f"trueup: page {page_num} fetch error: {e!r}")
                break

            if r.status_code != 200:
                log.warning(
                    f"trueup: page {page_num} got HTTP {r.status_code} "
                    f"({len(r.text)} bytes); aborting"
                )
                break

            try:
                j = r.json()
            except Exception as e:
                log.warning(f"trueup: page {page_num} JSON parse error: {e!r}")
                break

            hits = j.get("hits") or []
            page_jobs: list[JobPost] = []
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                post = self._build_post(hit, scraper_input)
                if post is None or post.id in seen_ids:
                    continue
                seen_ids.add(post.id)
                page_jobs.append(post)

            jobs.extend(page_jobs)
            log.info(
                f"trueup: page {page_num} → {len(page_jobs)} new "
                f"(total {len(jobs)}, nbHits={j.get('nbHits', 0)})"
            )

            if len(jobs) >= wanted:
                jobs = jobs[:wanted]
                break

            n_pages = j.get("nbPages", 0)
            if page_num + 1 >= n_pages or len(hits) == 0:
                break

        log.info(f"trueup: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)

    def _build_post(self, hit: dict, si: ScraperInput) -> JobPost | None:
        post_id = str(hit.get("job_id") or hit.get("objectID") or "").strip()
        title = (hit.get("title") or "").strip()
        if not post_id or not title:
            return None

        company_name = hit.get("company") or hit.get("company_short")
        company_id = hit.get("company_id")
        company_url = (
            f"https://{hit['company_url_clean']}"
            if hit.get("company_url_clean")
            else (f"https://trueup.io/co/{company_id}" if company_id else None)
        )

        location = _build_location(hit.get("location"))
        is_remote = "remote" in (hit.get("location") or "").lower()

        date_posted = None
        ts = hit.get("updated_at_timestamp")
        if ts:
            try:
                date_posted = datetime.fromtimestamp(
                    int(ts), tz=timezone.utc
                ).date()
            except (ValueError, OSError, OverflowError):
                date_posted = None

        compensation = _build_compensation(hit)

        # TrueUp's USP — assemble the rich company-health signals into
        # a description block so users see them inline.
        desc_lines: list[str] = []
        if hit.get("business_description_short"):
            desc_lines.append(str(hit["business_description_short"]))
        cdp = hit.get("company_description_plus") or []
        if isinstance(cdp, list) and cdp:
            desc_lines.append(" • ".join(str(x) for x in cdp))
        tags = hit.get("description_tags") or []
        if isinstance(tags, list) and tags:
            desc_lines.append("Tags: " + ", ".join(str(t) for t in tags))
        traj = hit.get("trajectory_score")
        if isinstance(traj, (int, float)) and traj > 0:
            desc_lines.append(f"TrueUp trajectory score: {traj:.1f}")
        val = hit.get("valuation")
        if isinstance(val, (int, float)) and val > 0:
            desc_lines.append(f"Valuation: ${val:.1f}B")
        ats_warn = hit.get("ats_ux_warning")
        if isinstance(ats_warn, str) and ats_warn.strip():
            desc_lines.append(f"⚠️ {ats_warn}")
        description = "\n\n".join(desc_lines) or None

        return JobPost(
            id=post_id,
            title=title,
            company_name=company_name,
            job_url=hit.get("url") or f"https://trueup.io/job/{post_id}",
            location=location,
            description=description,
            company_url=company_url,
            compensation=compensation,
            date_posted=date_posted,
            is_remote=is_remote,
        )


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _build_location(raw: str | None) -> Location | None:
    if not raw or not isinstance(raw, str):
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    city = parts[0]
    state = parts[1] if len(parts) >= 2 else None
    country = parts[2] if len(parts) >= 3 else None
    return Location(city=city, state=state, country=country)


def _build_compensation(hit: dict) -> Compensation | None:
    mn = hit.get("salary_range_min")
    mx = hit.get("salary_range_max")
    if mn is None and mx is None:
        return None
    try:
        return Compensation(
            min_amount=float(mn) if mn is not None else None,
            max_amount=float(mx) if mx is not None else None,
            currency="USD",
            interval=CompensationInterval.YEARLY,
        )
    except (ValueError, TypeError):
        return None
