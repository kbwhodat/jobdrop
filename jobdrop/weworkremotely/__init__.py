"""WeWorkRemotely scraper — public RSS feed at /remote-jobs.rss."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from curl_cffi import requests as cc_requests

from jobdrop.weworkremotely.util import log
from jobdrop.model import (
    JobPost,
    JobResponse,
    Location,
    Scraper,
    ScraperInput,
    Site,
)

_FEED_URL = "https://weworkremotely.com/remote-jobs.rss"
_TIMEOUT_S = 20
_DEFAULT_MAX_AGE_DAYS = 30


class WeWorkRemotely(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.WEWORKREMOTELY, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self.user_agent = user_agent

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted or 15
        start_offset = max(scraper_input.offset or 0, 0)

        sess = cc_requests.Session(impersonate="safari17_2_ios")
        try:
            r = sess.get(_FEED_URL, timeout=_TIMEOUT_S)
        except Exception as e:
            log.warning(f"WeWorkRemotely: feed fetch failed: {e!r}")
            return JobResponse(jobs=[])
        if not r.ok:
            log.warning(f"WeWorkRemotely: HTTP {r.status_code}")
            return JobResponse(jobs=[])

        try:
            root = ElementTree.fromstring(r.text)
        except Exception as e:
            log.warning(f"WeWorkRemotely: RSS parse error: {e!r}")
            return JobResponse(jobs=[])

        channel = root.find("channel")
        items = list(channel.findall("item")) if channel is not None else []
        log.info(f"WeWorkRemotely: feed returned {len(items)} items")

        title_token = (scraper_input.search_term or "").lower().strip()
        location_filter = (scraper_input.location or "").lower().strip()
        cutoff = _resolve_cutoff(scraper_input)

        seen_ids: set[str] = set()
        jobs: list[JobPost] = []
        for item in items:
            t = _text(item, "title") or ""
            link = _text(item, "link") or ""
            desc = _text(item, "description") or ""
            pubdate = _text(item, "pubDate") or ""
            region = _text(item, "region") or ""
            posted_dt = _parse_rfc822(pubdate)

            position, company = _split_title(t)
            if not position or not link:
                continue

            if title_token and title_token not in position.lower():
                continue
            if location_filter and region:
                rl = region.lower()
                if "only" in rl and location_filter not in rl and "remote" not in rl:
                    continue
            if cutoff and posted_dt and posted_dt < cutoff:
                continue

            post = _build_jobpost(position, company, link, desc, posted_dt, region)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)

        jobs = jobs[start_offset : start_offset + wanted]
        log.info(f"WeWorkRemotely: returning {len(jobs)} jobs (offset={start_offset})")
        return JobResponse(jobs=jobs)


def _text(elem, tag) -> str | None:
    if elem is None:
        return None
    found = elem.find(tag)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _split_title(t: str) -> tuple[str, str]:
    """WWR titles use 'Company: Position' or 'Position at Company'."""
    if ":" in t:
        company, _, position = t.partition(":")
        return position.strip(), company.strip()
    if " at " in t:
        position, _, company = t.partition(" at ")
        return position.strip(), company.strip()
    return t.strip(), ""


def _parse_rfc822(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _resolve_cutoff(si: ScraperInput) -> datetime | None:
    hours = getattr(si, "hours_old", None)
    if hours and hours > 0:
        return datetime.now(timezone.utc) - timedelta(hours=hours)
    return datetime.now(timezone.utc) - timedelta(days=_DEFAULT_MAX_AGE_DAYS)


def _build_jobpost(
    position: str, company: str, link: str, description_html: str,
    posted_dt: datetime | None, region: str,
) -> JobPost | None:
    m = re.search(r"/remote-jobs/(\d+)", link)
    if m:
        pid = m.group(1)
    else:
        m2 = re.search(r"/remote-jobs/([a-zA-Z0-9_-]+)", link)
        pid = m2.group(1) if m2 else link
    location = Location(country=None, city=None, state=region or "Remote")

    description = re.sub(r"<[^>]+>", " ", description_html or "")
    description = re.sub(r"\s+", " ", description).strip() or None

    return JobPost(
        id=f"weworkremotely-{pid}",
        title=position,
        company_name=company or None,
        job_url=link,
        location=location,
        is_remote=True,
        date_posted=posted_dt.date() if posted_dt else None,
        description=description,
    )
