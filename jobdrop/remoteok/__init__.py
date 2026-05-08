"""RemoteOK scraper — public JSON API at remoteok.com/api.

Single global JSON endpoint. No browser, no dorking.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from curl_cffi import requests as cc_requests

from jobdrop.remoteok.util import log
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

_API = "https://remoteok.com/api"
_TIMEOUT_S = 20
_DEFAULT_MAX_AGE_DAYS = 30


class RemoteOK(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.REMOTEOK, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self.user_agent = user_agent

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted or 15
        start_offset = max(scraper_input.offset or 0, 0)

        sess = cc_requests.Session(impersonate="safari17_2_ios")
        try:
            r = sess.get(
                _API,
                timeout=_TIMEOUT_S,
                headers={"User-Agent": "jobdrop/1.0 (+https://github.com/kbwhodat/jobdrop)"},
            )
        except Exception as e:
            log.warning(f"RemoteOK: API fetch failed: {e!r}")
            return JobResponse(jobs=[])
        if not r.ok:
            log.warning(f"RemoteOK: HTTP {r.status_code}")
            return JobResponse(jobs=[])
        try:
            payload = r.json()
        except Exception as e:
            log.warning(f"RemoteOK: JSON parse error: {e!r}")
            return JobResponse(jobs=[])

        raw = [e for e in payload if isinstance(e, dict) and e.get("position")]
        log.info(f"RemoteOK: API returned {len(raw)} postings")

        title_token = (scraper_input.search_term or "").lower().strip()
        location_filter = (scraper_input.location or "").lower().strip()
        cutoff = _resolve_cutoff(scraper_input)

        seen_ids: set[str] = set()
        jobs: list[JobPost] = []
        for p in raw:
            title = (p.get("position") or "").lower()
            if title_token and title_token not in title:
                continue
            loc_text = (p.get("location") or "").lower()
            if location_filter and location_filter not in loc_text:
                if "remote" not in loc_text and "anywhere" not in loc_text:
                    continue

            posted_dt = _parse_epoch(p.get("date"))
            if cutoff and posted_dt and posted_dt < cutoff:
                continue

            post = _build_jobpost(p, posted_dt)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)

        jobs = jobs[start_offset : start_offset + wanted]
        log.info(f"RemoteOK: returning {len(jobs)} jobs (offset={start_offset})")
        return JobResponse(jobs=jobs)


def _resolve_cutoff(si: ScraperInput) -> datetime | None:
    hours = getattr(si, "hours_old", None)
    if hours and hours > 0:
        return datetime.now(timezone.utc) - timedelta(hours=hours)
    return datetime.now(timezone.utc) - timedelta(days=_DEFAULT_MAX_AGE_DAYS)


def _parse_epoch(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        if isinstance(value, str):
            iso = value.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(iso)
            except ValueError:
                pass
    except (ValueError, OSError, OverflowError):
        return None
    return None


def _build_jobpost(raw: dict, posted_dt: datetime | None) -> JobPost | None:
    pid = str(raw.get("id") or raw.get("slug") or "").strip()
    title = (raw.get("position") or "").strip()
    company = (raw.get("company") or "").strip()
    if not pid or not title:
        return None
    loc_text = (raw.get("location") or "Remote").strip()
    location = Location(country=None, city=None, state=loc_text) if loc_text else None

    salary_min = raw.get("salary_min") if isinstance(raw.get("salary_min"), (int, float)) else None
    salary_max = raw.get("salary_max") if isinstance(raw.get("salary_max"), (int, float)) else None
    compensation = None
    if salary_min or salary_max:
        compensation = Compensation(
            interval=CompensationInterval.YEARLY,
            min_amount=float(salary_min) if salary_min else None,
            max_amount=float(salary_max) if salary_max else None,
            currency="USD",
        )

    return JobPost(
        id=f"remoteok-{pid}",
        title=title,
        company_name=company or None,
        job_url=raw.get("url") or f"https://remoteok.com/remote-jobs/{pid}",
        location=location,
        is_remote=True,
        date_posted=posted_dt.date() if posted_dt else None,
        description=(raw.get("description") or "").strip() or None,
        compensation=compensation,
    )
