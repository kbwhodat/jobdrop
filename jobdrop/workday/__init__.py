"""Workday scraper — Google-dorked discovery + public CXS API.

Workday hosts a separate tenant per company at unique URLs:
  https://{tenant}.{wdN}.myworkdayjobs.com/{locale}/{site}/job/...

Each tenant has its own datacenter (`wd1`/`wd5`/`wd12`/...) and its
own ``site`` name (``NVIDIAExternalCareerSite``, ``Cox_Careers``, etc.).

We dork ``site:myworkdayjobs.com`` for the caller's keywords, capture
``(tenant, datacenter, site)`` triples from the SERP URLs themselves
(no guessing), then POST to each tenant's CXS API for full job data.

## Stage 1: Google discovery (zendriver)

Capture group from SERP URL: tenant, datacenter, site name.

## Stage 2: CXS API enrichment

  POST https://{tenant}.{datacenter}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
  body: {appliedFacets:{}, limit:N, offset:0, searchText:"<keywords>"}

Response: {jobPostings: [...], total: N}
Each posting: {title, locationsText, postedOn, externalPath, ...}
"""
from __future__ import annotations

import asyncio
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import quote_plus

from curl_cffi import requests as cc_requests

from jobdrop.workday.util import log
from jobdrop.model import (
    JobPost,
    JobResponse,
    Location,
    Scraper,
    ScraperInput,
    Site,
)

_HOST_FRAGMENT = "myworkdayjobs.com"
_GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&start={start}"

# SERP URL: https://{tenant}.{wdN}.myworkdayjobs.com/{locale}/{site}/job/...
# Capture (tenant, datacenter, site). Site name MUST be followed by `/job/`
# or end-of-URL — without that anchor, the regex would match address path
# segments (e.g. ``/200-Galleria-Parkway-SE-...``) as fake sites and create
# duplicate "configs" for the same tenant.
_WD_URL_RE = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com"
    r"(?:/[a-z]{2}-[A-Z]{2})?"
    r"/([A-Za-z0-9_-]+)(?:/job/|/?$|/?[?#])"
)

_RENDER_SLEEP_S = 3.0
_API_TIMEOUT_S = 15
_API_WORKERS = 12


class Workday(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.WORKDAY, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input: ScraperInput | None = None
        self.user_agent = user_agent

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        wanted = scraper_input.results_wanted or 15
        start_offset = max(scraper_input.offset or 0, 0)

        try:
            import zendriver as zd  # noqa: F401
        except ImportError:
            log.error(
                "Workday: zendriver is required for Google discovery. "
                "Install with: pip install zendriver"
            )
            return JobResponse(jobs=[])

        query = _build_query(scraper_input)
        log.info(f"Workday: Google query = {query!r}")

        # Stage 1: discover (tenant, datacenter, site) triples
        try:
            tenants = _run_async(_discover_tenants(query, wanted))
        except RuntimeError as e:
            if "asyncio.run" in str(e) or "running event loop" in str(e):
                tenants = _run_on_thread(_discover_tenants(query, wanted))
            else:
                raise
        log.info(f"Workday: discovered {len(tenants)} tenant configs from SERP")
        if not tenants:
            return JobResponse(jobs=[])

        # Stage 2: parallel-fetch each tenant's CXS API
        sess = cc_requests.Session(impersonate="safari17_2_ios")
        search_text = scraper_input.search_term or ""
        per_tenant_target = max(20, (wanted + start_offset) // 2 + 5)

        all_postings: list[tuple[dict, dict]] = []
        with ThreadPoolExecutor(max_workers=_API_WORKERS) as ex:
            futures = {
                ex.submit(_fetch_tenant, sess, t, search_text, per_tenant_target): t
                for t in tenants
            }
            for fut in as_completed(futures):
                tenant_cfg = futures[fut]
                try:
                    postings = fut.result()
                except Exception as e:
                    log.debug(
                        f"Workday: {tenant_cfg['tenant']}/{tenant_cfg['site']} "
                        f"fetch failed: {e!r}"
                    )
                    continue
                for p in postings:
                    all_postings.append((tenant_cfg, p))

        log.info(
            f"Workday: API enrichment hit {len(all_postings)} postings "
            f"across {len(tenants)} tenants"
        )

        # Stage 3: client-side filter — title-substring (Workday's server-side
        # searchText is too lenient: it matches JD body text, so "network
        # engineer" returns Service Delivery Managers etc.). Combine with
        # location + remote filters.
        title_token = (scraper_input.search_term or "").lower().strip()
        location_filter = (scraper_input.location or "").lower().strip()
        is_remote = bool(scraper_input.is_remote)

        filtered: list[tuple[dict, dict]] = []
        for tenant_cfg, p in all_postings:
            title = (p.get("title") or "").lower()
            loc = (p.get("locationsText") or "").lower()
            if title_token and title_token not in title:
                continue
            if location_filter and location_filter not in loc:
                if not (("remote" in loc) and is_remote):
                    continue
            if is_remote and "remote" not in loc:
                continue
            filtered.append((tenant_cfg, p))

        log.info(f"Workday: {len(filtered)} match filters")

        # Stage 4: paginate + build JobPosts (dedup by id at the end since
        # multiple SERP-discovered configs can resolve to the same posting).
        cutoff = _resolve_cutoff(scraper_input)
        seen_ids: set[str] = set()
        jobs: list[JobPost] = []
        for tenant_cfg, p in filtered:
            post = _build_jobpost(tenant_cfg, p, cutoff)
            if post is None or post.id in seen_ids:
                continue
            seen_ids.add(post.id)
            jobs.append(post)
        jobs = jobs[start_offset : start_offset + wanted]

        log.info(f"Workday: returning {len(jobs)} jobs (offset={start_offset})")
        return JobResponse(jobs=jobs)


# ─────────────────────────────────────────────────────────────────────────
# Stage 1 — Google discovery
# ─────────────────────────────────────────────────────────────────────────


def _build_query(si: ScraperInput) -> str:
    parts: list[str] = [f"site:{_HOST_FRAGMENT}"]
    if si.search_term:
        parts.append(f'"{si.search_term}"')
    if si.location:
        city = si.location.split(",")[0].strip()
        if city:
            parts.append(f'"{city}"')
    if si.is_remote:
        parts.append('"remote"')
    return " ".join(parts)


async def _discover_tenants(query: str, wanted: int) -> list[dict]:
    """Walk Google SERPs, return list of {tenant, datacenter, site, host, url}."""
    import zendriver as zd
    encoded = quote_plus(query)
    seen: set[tuple[str, str, str]] = set()
    ordered: list[dict] = []
    browser = await zd.start(
        headless=True, sandbox=False, browser_args=["--window-size=1280,900"],
    )
    try:
        for page_idx in range(5):
            url = _GOOGLE_SEARCH_URL.format(query=encoded, start=page_idx * 10)
            log.info(f"Workday: SERP page {page_idx + 1} → {url[:120]}")
            try:
                tab = await browser.get(url)
            except Exception as e:
                log.error(f"Workday: SERP fetch failed on page {page_idx + 1}: {e}")
                break
            await asyncio.sleep(_RENDER_SLEEP_S)
            try:
                current = await tab.evaluate("location.href")
            except Exception:
                current = url
            if "/sorry/" in str(current):
                log.error(
                    f"Workday: hit Google /sorry/ on page {page_idx + 1}. "
                    "Returning what we have."
                )
                break
            try:
                html = await tab.get_content()
            except Exception:
                html = await tab.evaluate("document.documentElement.outerHTML") or ""

            new_count = 0
            for m in _WD_URL_RE.finditer(html):
                tenant = m.group(1).lower()
                wd = m.group(2).lower()
                site = m.group(3)
                # Skip non-site path elements (job, en-US, etc. caught by greedy regex)
                if site in {"job", "en", "www", "wday", "static"} or len(site) > 100:
                    continue
                key = (tenant, wd, site)
                if key in seen:
                    continue
                seen.add(key)
                host = f"{tenant}.{wd}.{_HOST_FRAGMENT}"
                ordered.append({
                    "tenant": tenant,
                    "datacenter": wd,
                    "site": site,
                    "host": host,
                    "url": f"https://{host}/wday/cxs/{tenant}/{site}",
                })
                new_count += 1

            log.info(
                f"Workday: page {page_idx + 1} added {new_count} tenant configs "
                f"(total {len(ordered)} / wanted {wanted})"
            )
            if len(ordered) >= max(wanted, 20):
                break
            if new_count == 0:
                break
    finally:
        await browser.stop()
    return ordered


# ─────────────────────────────────────────────────────────────────────────
# Stage 2 — CXS API enrichment
# ─────────────────────────────────────────────────────────────────────────


def _fetch_tenant(
    sess: cc_requests.Session, tenant_cfg: dict, search_text: str, limit: int,
) -> list[dict]:
    body = {
        "appliedFacets": {},
        "limit": min(limit, 50),
        "offset": 0,
        "searchText": search_text,
    }
    r = sess.post(
        tenant_cfg["url"] + "/jobs",
        json=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=_API_TIMEOUT_S,
    )
    if not r.ok:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    return data.get("jobPostings") or []


# ─────────────────────────────────────────────────────────────────────────
# Build JobPost
# ─────────────────────────────────────────────────────────────────────────


def _resolve_cutoff(si: ScraperInput) -> datetime | None:
    hours = getattr(si, "hours_old", None)
    if hours and hours > 0:
        return datetime.now() - timedelta(hours=hours)
    return None


def _build_jobpost(tenant_cfg: dict, raw: dict, cutoff: datetime | None) -> JobPost | None:
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    ext_path = raw.get("externalPath") or ""
    job_url = f"https://{tenant_cfg['host']}{ext_path}" if ext_path else ""
    if not job_url:
        return None

    posted_dt = _parse_relative_date(raw.get("postedOn"))
    if cutoff and posted_dt and posted_dt < cutoff:
        return None

    location = _build_location(raw.get("locationsText"))
    is_remote = "remote" in (raw.get("locationsText") or "").lower()

    company_name = _humanize_tenant(tenant_cfg["tenant"])
    return JobPost(
        id=f"workday-{tenant_cfg['tenant']}-{ext_path.rsplit('/', 1)[-1] if ext_path else title}",
        title=title,
        company_name=company_name,
        job_url=job_url,
        location=location,
        is_remote=is_remote,
        date_posted=posted_dt.date() if posted_dt else None,
    )


def _parse_relative_date(text: str | None) -> datetime | None:
    if not text:
        return None
    t = text.lower().strip()
    now = datetime.now()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\+?\s*week", t)
    if m:
        return now - timedelta(weeks=int(m.group(1)))
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return now - timedelta(days=int(m.group(1)) * 30)
    return None


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


def _humanize_tenant(tenant: str) -> str:
    return tenant.replace("-", " ").replace("_", " ").upper() if len(tenant) <= 4 else \
        tenant.replace("-", " ").replace("_", " ").title()


# ─────────────────────────────────────────────────────────────────────────
# Async runner helpers
# ─────────────────────────────────────────────────────────────────────────


def _run_async(coro):
    return asyncio.run(coro)


def _run_on_thread(coro):
    result_box: dict = {}

    def runner():
        try:
            result_box["ok"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            result_box["err"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "err" in result_box:
        raise result_box["err"]
    return result_box.get("ok")
