"""Built In scraper — server-rendered HTML at builtin.com/jobs.

Tech-vetted local job board family (Built In NYC/LA/Chicago/Atlanta/etc.).
Heavy overlap with LinkedIn/Indeed — the cross-source dedup pass in
jobdrop's scrape_jobs() collapses (company, title) duplicates so Built
In contributes its tech-quality filter without inflating result count.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from curl_cffi import requests as cc_requests

from jobdrop.builtin.util import log
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

_BASE = "https://builtin.com"
_TIMEOUT_S = 20
_DEFAULT_MAX_AGE_DAYS = 60
_PER_PAGE = 25

_CARD_RE = re.compile(
    r'(?s)<div\s+id="job-card-(\d+)"[^>]*data-id="job-card"[^>]*>(.*?)(?=<div\s+id="job-card-\d+"|<div\s+id="search-results-bottom"|$)'
)
_TITLE_RE = re.compile(
    r'<a\s+href="(/job/[a-zA-Z0-9_/-]+)"[^>]*data-id="job-card-title"[^>]*>([^<]+)</a>'
)
_COMPANY_RE = re.compile(
    r'<a\s+[^>]*data-id="company-title"[^>]*>\s*<span>([^<]+)</span>'
)
_DETAIL_SPAN_RE = re.compile(
    r'<span\s+class="font-barlow\s+text-gray-04">([^<]+)</span>'
)
_POSTED_RE = re.compile(
    r'<span\s+x-show="!showSavedTag"\s+x-cloak[^>]*>(?:<i[^>]*></i>)?([^<]+)</span>'
)


class BuiltIn(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.BUILTIN, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self.user_agent = user_agent

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted or 15
        start_offset = max(scraper_input.offset or 0, 0)
        start_page = (start_offset // _PER_PAGE) + 1
        first_page_drop = start_offset % _PER_PAGE

        sess = cc_requests.Session(impersonate="safari17_2_ios")

        all_cards: list[tuple[str, dict]] = []
        for offset_idx in range(5):
            page = start_page + offset_idx
            url = self._build_search_url(scraper_input, page)
            log.info(f"BuiltIn: fetching page {page} → {url[:120]}")
            try:
                r = sess.get(url, timeout=_TIMEOUT_S)
            except Exception as e:
                log.warning(f"BuiltIn: page {page} fetch failed: {e!r}")
                break
            if not r.ok:
                log.warning(f"BuiltIn: page {page} HTTP {r.status_code}")
                break
            cards = list(_CARD_RE.finditer(r.text))
            if not cards:
                break
            for c in cards:
                jid = c.group(1)
                block = c.group(2)
                all_cards.append((jid, _parse_card(block)))
            if len(cards) < _PER_PAGE:
                break
            if len(all_cards) >= wanted + first_page_drop + 5:
                break

        if first_page_drop and all_cards:
            all_cards = all_cards[first_page_drop:]

        log.info(f"BuiltIn: parsed {len(all_cards)} cards across pages")

        cutoff = _resolve_cutoff(scraper_input)
        seen_ids: set[str] = set()
        jobs: list[JobPost] = []
        for jid, info in all_cards:
            posted = _parse_posted(info.get("posted_text"))
            if cutoff and posted and posted < cutoff:
                continue
            post = _build_jobpost(jid, info, posted)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)
            if len(jobs) >= wanted:
                break

        log.info(f"BuiltIn: returning {len(jobs)} jobs (offset={start_offset})")
        return JobResponse(jobs=jobs)

    def _build_search_url(self, si: ScraperInput, page: int) -> str:
        params = []
        if si.search_term:
            params.append(f"search={quote_plus(si.search_term)}")
        if si.location:
            params.append(f"location={quote_plus(si.location)}")
        if si.is_remote:
            params.append("remote=true")
        if page > 1:
            params.append(f"page={page}")
        suffix = ("?" + "&".join(params)) if params else ""
        return f"{_BASE}/jobs{suffix}"


def _parse_card(html_block: str) -> dict:
    title_m = _TITLE_RE.search(html_block)
    company_m = _COMPANY_RE.search(html_block)
    posted_m = _POSTED_RE.search(html_block)
    spans = _DETAIL_SPAN_RE.findall(html_block)

    workplace_type = None
    location_text = None
    salary_text = None
    for s in spans:
        s_clean = s.strip()
        if s_clean.lower() in {"hybrid", "remote", "on-site", "onsite", "in office"}:
            workplace_type = s_clean
        elif "annually" in s_clean.lower() or "hourly" in s_clean.lower() or "$" in s_clean:
            salary_text = s_clean
        elif location_text is None:
            location_text = s_clean

    return {
        "title": title_m.group(2).strip() if title_m else None,
        "url": f"{_BASE}{title_m.group(1)}" if title_m else None,
        "company": company_m.group(1).strip() if company_m else None,
        "workplace_type": workplace_type,
        "location_text": location_text,
        "salary_text": salary_text,
        "posted_text": posted_m.group(1).strip() if posted_m else None,
    }


def _parse_posted(text: str | None) -> datetime | None:
    if not text:
        return None
    t = text.lower().strip()
    now = datetime.now(timezone.utc)
    if "minute" in t or "second" in t or "hour" in t:
        return now
    m = re.search(r"(\d+)\s*day", t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\s*week", t)
    if m:
        return now - timedelta(weeks=int(m.group(1)))
    m = re.search(r"(\d+)\s*month", t)
    if m:
        return now - timedelta(days=int(m.group(1)) * 30)
    return None


def _resolve_cutoff(si: ScraperInput) -> datetime | None:
    hours = getattr(si, "hours_old", None)
    if hours and hours > 0:
        return datetime.now(timezone.utc) - timedelta(hours=hours)
    return datetime.now(timezone.utc) - timedelta(days=_DEFAULT_MAX_AGE_DAYS)


def _build_location(loc_text: str | None) -> Location | None:
    if not loc_text:
        return None
    parts = [p.strip() for p in loc_text.split(",") if p.strip()]
    if not parts:
        return None
    city = parts[0] if "remote" not in parts[0].lower() else None
    state = parts[1] if len(parts) >= 2 else None
    country = parts[2] if len(parts) >= 3 else None
    if not (city or state or country):
        return None
    return Location(city=city, state=state, country=country)


def _build_compensation(salary_text: str | None) -> Compensation | None:
    if not salary_text:
        return None
    m = re.search(
        r"\$?(\d+(?:\.\d+)?)\s*[Kk]?\s*[-–—]\s*\$?(\d+(?:\.\d+)?)\s*[Kk]?",
        salary_text,
    )
    if not m:
        return None
    try:
        lo = float(m.group(1))
        hi = float(m.group(2))
        if "k" in salary_text.lower() and lo < 1000:
            lo *= 1000
        if "k" in salary_text.lower() and hi < 1000:
            hi *= 1000
        interval = (
            CompensationInterval.HOURLY
            if "hour" in salary_text.lower()
            else CompensationInterval.YEARLY
        )
        return Compensation(
            interval=interval,
            min_amount=lo,
            max_amount=hi,
            currency="USD",
        )
    except (ValueError, TypeError):
        return None


def _build_jobpost(jid: str, info: dict, posted_dt: datetime | None) -> JobPost | None:
    title = info.get("title")
    if not title or not info.get("url"):
        return None
    is_remote = (info.get("workplace_type") or "").lower() == "remote"
    return JobPost(
        id=f"builtin-{jid}",
        title=title,
        company_name=info.get("company"),
        job_url=info["url"],
        location=_build_location(info.get("location_text")),
        is_remote=is_remote,
        date_posted=posted_dt.date() if posted_dt else None,
        compensation=_build_compensation(info.get("salary_text")),
    )
