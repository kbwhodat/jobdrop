from __future__ import annotations

# Apply compiled defaults BEFORE any scraper module runs its module-level
# os.environ reads. User-set env vars are preserved (setdefault semantics).
from jobdrop import _defaults  # noqa: F401

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Tuple

import pandas as pd

# Cross-source dedup priority — lower wins. The same posting often
# appears on a direct ATS (greenhouse/ashby/workday/lever/icims), a
# major board (linkedin/indeed/glassdoor), and an aggregator (built in/
# hiring cafe/trueup) at the same time. We keep the highest-fidelity
# copy: direct ATS > major board > niche/aggregator.
_SOURCE_PRIORITY = {
    "greenhouse": 0, "ashby": 0, "workday": 0, "lever": 0, "icims": 0,
    "linkedin": 1, "indeed": 1, "glassdoor": 1, "google": 1,
    "ziprecruiter": 1, "wellfound": 1, "naukri": 1, "bayt": 1,
    "usajobs": 1, "governmentjobs": 1,
    "remoteok": 2, "weworkremotely": 2,
    "adzuna": 2, "jooble": 2, "findwork": 2, "the_muse": 2,
    "insight_global": 2, "clearance_jobs": 2, "kforce": 2,
    "collab_work": 2,
    "hiring_cafe": 3, "trueup": 3, "builtin": 3,
}
_TITLE_NORM_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def _norm_title(title: str | None) -> str:
    if not title:
        return ""
    t = _TITLE_NORM_RE.sub(" ", title.lower())
    return _WS_RE.sub(" ", t).strip()


def _dedup_cross_source(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Collapse (company, normalized_title) collisions across sources.

    Rows missing either company or title are passed through untouched —
    we only dedup pairs we can confidently match. Returns (deduped_df,
    rows_dropped).
    """
    if df.empty or "company" not in df.columns or "title" not in df.columns:
        return df, 0
    company_norm = df["company"].fillna("").astype(str).str.lower().str.strip()
    title_norm = df["title"].fillna("").astype(str).map(_norm_title)
    has_key = (company_norm != "") & (title_norm != "")
    if not has_key.any():
        return df, 0
    keyed = df[has_key].copy()
    unkeyed = df[~has_key]
    keyed["_dup_key"] = list(zip(company_norm[has_key], title_norm[has_key]))
    keyed["_priority"] = keyed["site"].map(
        lambda s: _SOURCE_PRIORITY.get(s, 5)
    )
    before = len(keyed)
    keyed = (
        keyed.sort_values("_priority", kind="stable")
        .drop_duplicates("_dup_key", keep="first")
        .drop(columns=["_dup_key", "_priority"])
    )
    dropped = before - len(keyed)
    out = pd.concat([keyed, unkeyed], ignore_index=True)
    return out, dropped

from jobdrop.adzuna import Adzuna
from jobdrop.ashby import Ashby
from jobdrop.bayt import BaytScraper
from jobdrop.builtin import BuiltIn
from jobdrop.clearancejobs import ClearanceJobs
from jobdrop.collabwork import CollabWork
from jobdrop.findwork import Findwork
from jobdrop.glassdoor import Glassdoor
from jobdrop.google import Google
from jobdrop.governmentjobs import GovernmentJobs
from jobdrop.greenhouse import Greenhouse
from jobdrop.hiring_cafe import HiringCafe
from jobdrop.icims import ICIMS
from jobdrop.indeed import Indeed
from jobdrop.insightglobal import InsightGlobal
from jobdrop.jooble import Jooble
from jobdrop.kforce import Kforce
from jobdrop.lever import Lever
from jobdrop.linkedin import LinkedIn
from jobdrop.naukri import Naukri
from jobdrop.remoteok import RemoteOK
from jobdrop.the_muse import TheMuse
from jobdrop.trueup import TrueUp
from jobdrop.usajobs import USAJobs
from jobdrop.wellfound import Wellfound
from jobdrop.weworkremotely import WeWorkRemotely
from jobdrop.workday import Workday
from jobdrop.model import JobType, Location, JobResponse, Country
from jobdrop.model import SalarySource, ScraperInput, Site
from jobdrop.util import (
    set_logger_level,
    extract_salary,
    create_logger,
    get_enum_from_value,
    map_str_to_site,
    convert_to_annual,
    desired_order,
)
from jobdrop.ziprecruiter import ZipRecruiter


# Update the SCRAPER_MAPPING dictionary in the scrape_jobs function

def scrape_jobs(
    site_name: str | list[str] | Site | list[Site] | None = None,
    search_term: str | None = None,
    google_search_term: str | None = None,
    location: str | None = None,
    distance: int | None = 50,
    is_remote: bool = False,
    job_type: str | None = None,
    easy_apply: bool | None = None,
    results_wanted: int = 15,
    country_indeed: str = "usa",
    proxies: list[str] | str | None = None,
    ca_cert: str | None = None,
    description_format: str = "markdown",
    linkedin_fetch_description: bool | None = False,
    linkedin_company_ids: list[int] | None = None,
    offset: int | None = 0,
    hours_old: int = None,
    enforce_annual_salary: bool = False,
    verbose: int = 0,
    user_agent: str = None,
    **kwargs,
) -> pd.DataFrame:
    """
    Scrapes job data from job boards concurrently
    :return: Pandas DataFrame containing job data
    """
    SCRAPER_MAPPING = {
        Site.LINKEDIN: LinkedIn,
        Site.INDEED: Indeed,
        Site.ZIP_RECRUITER: ZipRecruiter,
        Site.GLASSDOOR: Glassdoor,
        Site.GOOGLE: Google,
        Site.BAYT: BaytScraper,
        Site.NAUKRI: Naukri,
        # API-based sources added in the kbwhodat fork
        Site.USAJOBS: USAJobs,
        Site.ADZUNA: Adzuna,
        Site.JOOBLE: Jooble,
        Site.FINDWORK: Findwork,
        Site.THE_MUSE: TheMuse,
        Site.INSIGHT_GLOBAL: InsightGlobal,
        Site.CLEARANCE_JOBS: ClearanceJobs,
        Site.KFORCE: Kforce,
        Site.GREENHOUSE: Greenhouse,
        Site.COLLAB_WORK: CollabWork,
        Site.WELLFOUND: Wellfound,
        Site.HIRING_CAFE: HiringCafe,
        Site.TRUEUP: TrueUp,
        Site.ASHBY: Ashby,
        Site.WORKDAY: Workday,
        Site.LEVER: Lever,
        Site.REMOTEOK: RemoteOK,
        Site.WEWORKREMOTELY: WeWorkRemotely,
        Site.GOVERNMENTJOBS: GovernmentJobs,
        Site.BUILTIN: BuiltIn,
        Site.ICIMS: ICIMS,
    }
    set_logger_level(verbose)
    job_type = get_enum_from_value(job_type) if job_type else None

    def get_site_type():
        site_types = list(Site)
        if isinstance(site_name, str):
            site_types = [map_str_to_site(site_name)]
        elif isinstance(site_name, Site):
            site_types = [site_name]
        elif isinstance(site_name, list):
            site_types = [
                map_str_to_site(site) if isinstance(site, str) else site
                for site in site_name
            ]
        return site_types

    country_enum = Country.from_string(country_indeed)

    scraper_input = ScraperInput(
        site_type=get_site_type(),
        country=country_enum,
        search_term=search_term,
        google_search_term=google_search_term,
        location=location,
        distance=distance,
        is_remote=is_remote,
        job_type=job_type,
        easy_apply=easy_apply,
        description_format=description_format,
        linkedin_fetch_description=linkedin_fetch_description,
        results_wanted=results_wanted,
        linkedin_company_ids=linkedin_company_ids,
        offset=offset,
        hours_old=hours_old,
    )

    def scrape_site(site: Site) -> Tuple[str, JobResponse]:
        scraper_class = SCRAPER_MAPPING[site]
        scraper = scraper_class(proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        scraped_data: JobResponse = scraper.scrape(scraper_input)
        cap_name = site.value.capitalize()
        site_name = "ZipRecruiter" if cap_name == "Zip_recruiter" else cap_name
        site_name = "LinkedIn" if cap_name == "Linkedin" else cap_name
        create_logger(site_name).info(f"finished scraping")
        return site.value, scraped_data

    site_to_jobs_dict = {}
    # Per-source telemetry — count / error / timing_ms — surfaced via
    # df.attrs so callers (e.g., the MCP server) can show transparency
    # about which sources actually contributed vs failed silently.
    import time as _time
    per_source_stats: dict[str, dict] = {}
    total_t0 = _time.perf_counter()

    def worker(site):
        t0 = _time.perf_counter()
        try:
            site_val, scraped_info = scrape_site(site)
            return site_val, scraped_info, None, _time.perf_counter() - t0
        except Exception as e:  # noqa: BLE001
            return site.value, JobResponse(jobs=[]), f"{type(e).__name__}: {e}", _time.perf_counter() - t0

    with ThreadPoolExecutor() as executor:
        future_to_site = {
            executor.submit(worker, site): site for site in scraper_input.site_type
        }

        for future in as_completed(future_to_site):
            site_value, scraped_data, error, elapsed = future.result()
            site_to_jobs_dict[site_value] = scraped_data
            per_source_stats[site_value] = {
                "count": len(scraped_data.jobs),
                "error": error,
                "timing_ms": int(elapsed * 1000),
            }

    total_elapsed_ms = int((_time.perf_counter() - total_t0) * 1000)

    jobs_dfs: list[pd.DataFrame] = []

    for site, job_response in site_to_jobs_dict.items():
        for job in job_response.jobs:
            job_data = job.dict()
            job_url = job_data["job_url"]
            job_data["site"] = site
            job_data["company"] = job_data["company_name"]
            job_data["job_type"] = (
                ", ".join(job_type.value[0] for job_type in job_data["job_type"])
                if job_data["job_type"]
                else None
            )
            job_data["emails"] = (
                ", ".join(job_data["emails"]) if job_data["emails"] else None
            )
            if job_data["location"]:
                job_data["location"] = Location(
                    **job_data["location"]
                ).display_location()

            # Handle compensation
            compensation_obj = job_data.get("compensation")
            if compensation_obj and isinstance(compensation_obj, dict):
                job_data["interval"] = (
                    compensation_obj.get("interval").value
                    if compensation_obj.get("interval")
                    else None
                )
                job_data["min_amount"] = compensation_obj.get("min_amount")
                job_data["max_amount"] = compensation_obj.get("max_amount")
                job_data["currency"] = compensation_obj.get("currency", "USD")
                job_data["salary_source"] = SalarySource.DIRECT_DATA.value
                if enforce_annual_salary and (
                    job_data["interval"]
                    and job_data["interval"] != "yearly"
                    and job_data["min_amount"]
                    and job_data["max_amount"]
                ):
                    convert_to_annual(job_data)
            else:
                if country_enum == Country.USA:
                    (
                        job_data["interval"],
                        job_data["min_amount"],
                        job_data["max_amount"],
                        job_data["currency"],
                    ) = extract_salary(
                        job_data["description"],
                        enforce_annual_salary=enforce_annual_salary,
                    )
                    job_data["salary_source"] = SalarySource.DESCRIPTION.value

            job_data["salary_source"] = (
                job_data["salary_source"]
                if "min_amount" in job_data and job_data["min_amount"]
                else None
            )

            #naukri-specific fields
            job_data["skills"] = (
                ", ".join(job_data["skills"]) if job_data["skills"] else None
            )
            job_data["experience_range"] = job_data.get("experience_range")
            job_data["company_rating"] = job_data.get("company_rating")
            job_data["company_reviews_count"] = job_data.get("company_reviews_count")
            job_data["vacancy_count"] = job_data.get("vacancy_count")
            job_data["work_from_home_type"] = job_data.get("work_from_home_type")

            job_df = pd.DataFrame([job_data])
            jobs_dfs.append(job_df)

    if jobs_dfs:
        # Step 1: Filter out all-NA columns from each DataFrame before concatenation
        filtered_dfs = [df.dropna(axis=1, how="all") for df in jobs_dfs]

        # Step 2: Concatenate the filtered DataFrames
        jobs_df = pd.concat(filtered_dfs, ignore_index=True)

        # Step 3: Ensure all desired columns are present, adding missing ones as empty
        for column in desired_order:
            if column not in jobs_df.columns:
                jobs_df[column] = None  # Add missing columns as empty

        # Reorder the DataFrame according to the desired order
        jobs_df = jobs_df[desired_order]

        # Step 4: Cross-source dedup before final sort. Built In + iCIMS
        # heavily overlap with LinkedIn/Indeed; this collapses dupes to
        # the highest-fidelity source (direct ATS > board > aggregator).
        jobs_df, dedup_dropped = _dedup_cross_source(jobs_df)

        # Step 5: Sort the DataFrame as required
        result = jobs_df.sort_values(
            by=["site", "date_posted"], ascending=[True, False]
        ).reset_index(drop=True)
    else:
        result = pd.DataFrame()
        dedup_dropped = 0

    # Attach per-source telemetry to the DataFrame for downstream inspection
    # (the MCP server surfaces these in its summary block).
    result.attrs["per_source"] = per_source_stats
    result.attrs["total_timing_ms"] = total_elapsed_ms
    result.attrs["dedup_dropped"] = dedup_dropped
    return result
