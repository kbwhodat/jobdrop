"""Insight Global scraper — staffing-firm career site.

Insight Global is a US staffing agency. Their job board at
jobs.insightglobal.com is server-side rendered ASP.NET — each search
result page returns a full HTML document with both visible card fields
(date, title, location, salary) and a hidden ``<div style="display:none">``
JSON blob per result containing the canonical job ID, posted date (epoch
ms), state, ZIP, and full description.

URL contract (discovered empirically):
  ``/find_a_job/?srch=<keywords>&zip=<location>&rd=Distance&remote=<bool>``

  - ``srch``  : free-text keyword
  - ``zip``   : "City, ST" or ZIP. The site geocodes server-side.
  - ``rd``    : literal string "Distance" — required, value irrelevant
  - ``remote``: "true" / "false"

There is no public API; we parse the rendered HTML. The hidden JSON
blob (``<div style="display:none">{...}</div>``) is preferred for
structured fields; visible markup is the fallback for human-formatted
salary and posted-date display.

No credentials, no rate-limit signals observed at modest query volume.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

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

log = create_logger("InsightGlobal")

_BASE = "https://jobs.insightglobal.com"
_SEARCH = f"{_BASE}/find_a_job/"
_TIMEOUT_S = 25
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# /Date(1770047521000)/  →  the integer is epoch milliseconds (UTC).
_MS_DATE_RE = re.compile(r"/Date\((\d+)\)/")
# "$175k - $185k (estimate)" → captures lower, upper, optional "(estimate)"
_SAL_RE = re.compile(
    r"\$(?P<lo>[\d,.]+)\s*([kKmM])?\s*-\s*\$(?P<hi>[\d,.]+)\s*([kKmM])?",
    re.UNICODE,
)


class InsightGlobal(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.INSIGHT_GLOBAL, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self._ua = user_agent or _USER_AGENT

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        params: dict[str, str] = {
            "srch": scraper_input.search_term or "",
            "zip": scraper_input.location or "",
            "rd": "Distance",  # required literal — site rejects search without it
            "remote": "true" if scraper_input.is_remote else "false",
        }
        url: str | None = f"{_SEARCH}?{urlencode(params, safe=', ')}"
        wanted = scraper_input.results_wanted
        country = scraper_input.country
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        jobs: list[JobPost] = []
        # IG paginates results — page 1 lives at /find_a_job/?... and pages
        # 2+ at /jobs/find_a_job/<state>/<city>/<n>/?... . The rendered page
        # links to the next page in an <a class="page-link"> with rel set
        # by Page Forward; we follow that until we run out or hit `wanted`.
        while url and url not in seen_urls and len(jobs) < wanted:
            seen_urls.add(url)
            log.info(f"InsightGlobal: GET {url}")
            try:
                r = requests.get(
                    url,
                    headers={"User-Agent": self._ua, "Accept": "text/html"},
                    timeout=_TIMEOUT_S,
                )
            except Exception as e:
                log.error(f"InsightGlobal: request failed: {e}")
                break
            if not r.ok:
                log.error(f"InsightGlobal: HTTP {r.status_code}")
                break

            page_jobs, next_url = _parse_results_page(
                r.text,
                wanted=wanted - len(jobs),
                country=country,
            )
            # IG occasionally repeats a job across page boundaries —
            # dedup by JobPost.id ("ig-<JobID>") so the caller never sees
            # the same posting twice.
            new_jobs = [p for p in page_jobs if p.id not in seen_ids]
            for p in new_jobs:
                seen_ids.add(p.id)
            jobs.extend(new_jobs)
            url = next_url
            if not page_jobs:
                # Nothing on this page → assume end-of-results even if a link exists.
                break

        log.info(f"InsightGlobal: returning {len(jobs)} jobs")
        return JobResponse(jobs=jobs)


def _parse_results_page(
    html: str, wanted: int, country: Country | None
) -> tuple[list[JobPost], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[JobPost] = []
    for block in soup.select("div.result"):
        post = _parse_block(block, country)
        if post is None:
            continue
        jobs.append(post)
        if len(jobs) >= wanted:
            break
    # The "Page Forward" link is an <a class="page-link" rel="nofollow"
    # title="Page Forward" href="..."> inside <div class="paging-container">.
    next_a = soup.select_one('a.page-link[title="Page Forward"]')
    next_url: str | None = None
    if next_a and next_a.get("href"):
        href = next_a["href"]
        next_url = href if href.startswith("http") else f"{_BASE}{href}"
    return jobs, next_url


def _parse_block(block, country: Country | None) -> JobPost | None:
    # Hidden JSON blob — canonical structured source.
    hidden_json: dict[str, Any] = {}
    hidden = block.find("div", style=lambda s: s and "display:none" in s.replace(" ", ""))
    if hidden is not None:
        raw = hidden.get_text(strip=True)
        try:
            hidden_json = json.loads(raw)
        except (ValueError, TypeError):
            hidden_json = {}

    job_id = hidden_json.get("JobID")
    title_a = block.select_one("div.job-title a")
    if title_a is None or not job_id:
        return None
    title = " ".join(title_a.get_text(strip=True).split())
    rel_url = title_a.get("href") or ""
    job_url = rel_url if rel_url.startswith("http") else f"{_BASE}{rel_url}"

    # Visible info row: <p>City, ST</p> | <p>Category</p> | <p>Type</p> | <p>Salary</p>
    info_ps = [p.get_text(strip=True) for p in block.select("div.job-info > p")]
    location_text = info_ps[0] if info_ps else ""
    category = info_ps[1] if len(info_ps) > 1 else None
    type_text = info_ps[2] if len(info_ps) > 2 else None
    salary_text = info_ps[3] if len(info_ps) > 3 else None

    location_obj = _build_location(location_text, hidden_json, country)
    job_type_list = _map_job_type(type_text)
    compensation = _parse_salary(salary_text)
    posted = _parse_posted_date(hidden_json.get("PostedDate"), block)

    return JobPost(
        id=f"ig-{job_id}",
        title=title,
        company_name=None,  # IG is the staffing firm; client company isn't surfaced in search
        location=location_obj,
        description=hidden_json.get("Description") or None,
        job_url=job_url,
        date_posted=posted,
        compensation=compensation,
        job_type=job_type_list,
        company_industry=category,
    )


def _build_location(text: str, blob: dict, country: Country | None) -> Location | None:
    state = blob.get("State")
    city: str | None = None
    state_short: str | None = None
    if text:
        parts = [p.strip() for p in text.split(",")]
        if parts:
            city = parts[0] or None
        if len(parts) > 1:
            state_short = parts[1] or None
    return Location(
        city=city,
        state=state_short or state,
        country=country or Country.USA,
    )


def _map_job_type(text: str | None) -> list[JobType] | None:
    if not text:
        return None
    t = text.strip().lower()
    # IG uses Perm / Contract / Contract-to-Hire — map to JobSpy enum.
    if "perm" in t:
        return [JobType.FULL_TIME]
    if "contract" in t:
        return [JobType.CONTRACT]
    if "intern" in t:
        return [JobType.INTERNSHIP]
    return None


def _parse_salary(text: str | None) -> Compensation | None:
    if not text:
        return None
    m = _SAL_RE.search(text)
    if not m:
        return None
    lo = _to_number(m.group("lo"), m.group(2))
    hi = _to_number(m.group("hi"), m.group(4))
    if lo is None or hi is None:
        return None
    # Heuristic: under $1,000 = hourly, otherwise yearly. Matches IG's
    # "$45 - $52" hourly contract roles vs "$120k - $140k" salary roles.
    interval = (
        CompensationInterval.HOURLY if max(lo, hi) < 1000
        else CompensationInterval.YEARLY
    )
    return Compensation(
        interval=interval,
        min_amount=lo,
        max_amount=hi,
        currency="USD",
    )


def _to_number(s: str, suffix: str | None) -> float | None:
    try:
        n = float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None
    if suffix and suffix.lower() == "k":
        n *= 1_000
    elif suffix and suffix.lower() == "m":
        n *= 1_000_000
    return n


def _parse_posted_date(blob_value: str | None, block) -> date | None:
    # Prefer epoch ms from the hidden blob — it's UTC and unambiguous.
    if blob_value:
        m = _MS_DATE_RE.search(blob_value)
        if m:
            try:
                return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date()
            except (ValueError, OSError):
                pass
    # Fallback to the visible "Feb 02, 2026" date string.
    date_p = block.select_one("p.date")
    if date_p:
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(date_p.get_text(strip=True), fmt).date()
            except ValueError:
                continue
    return None
