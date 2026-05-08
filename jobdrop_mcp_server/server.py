#!/usr/bin/env python3
"""
Jobdrop MCP Server

An MCP server that provides job scraping capabilities using the Jobdrop library.
Built with FastMCP for modern MCP protocol compliance.
"""

import difflib
import json
import logging
import re
from typing import Optional, List
import pandas as pd

# Modern MCP imports (2025)
from mcp.server.fastmcp import FastMCP, Context

# Jobdrop imports
from jobdrop import scrape_jobs
from jobdrop.model import Site, JobType, Country

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jobdrop-mcp")

# Create FastMCP server instance
mcp = FastMCP("Jobdrop Job Search Server")


# ─────────────────────────────────────────────────────────────────────────
# Defensive input normalization helpers
# ─────────────────────────────────────────────────────────────────────────

# Common site-name aliases / typos that smaller models hallucinate
_SITE_ALIASES = {
    "angel_list": "wellfound",
    "angellist": "wellfound",
    "angel": "wellfound",
    "ziprecruiter": "zip_recruiter",
    "zip-recruiter": "zip_recruiter",
    "linked_in": "linkedin",
    "linked-in": "linkedin",
    "the-muse": "the_muse",
    "themuse": "the_muse",
    "indeed.com": "indeed",
    "google_jobs": "google",
    "googlejobs": "google",
    "hiring-cafe": "hiring_cafe",
    "hiringcafe": "hiring_cafe",
    "collab-work": "collab_work",
    "collabwork": "collab_work",
    "us_jobs": "usajobs",
    "us-jobs": "usajobs",
    "clearance-jobs": "clearance_jobs",
    "insight-global": "insight_global",
    "ashbyhq": "ashby",
    "ashby-hq": "ashby",
    "workday-jobs": "workday",
    "myworkdayjobs": "workday",
    "lever-jobs": "lever",
    "leverhq": "lever",
}

_REMOTE_LOCATION_ALIASES = {"remote", "anywhere", "wfh", "work from home", "us-remote", "remote-us"}


def _coerce_bool(v):
    """Accept "true"/"false"/1/0/etc. as booleans (small-model commonly send strings)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "y", "t")
    if isinstance(v, (int, float)):
        return bool(v)
    return v


def _coerce_int(v):
    """Accept stringified ints (small models commonly send "5" instead of 5)."""
    if v is None or isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v.strip())
        except (ValueError, AttributeError):
            return v
    if isinstance(v, float):
        return int(v)
    return v


# ─────────────────────────────────────────────────────────────────────────
# Post-filter helpers
# ─────────────────────────────────────────────────────────────────────────

# Salaries above this threshold are almost certainly upstream-feed
# glitches (e.g. CollabWork's Dover Fueling Solutions $8B/yr listing,
# from numeric overflow in their source data). Drop silently — keeps
# downstream agents from averaging absurd numbers and hallucinating.
_SUSPICIOUS_SALARY_THRESHOLD = 5_000_000  # USD/yr cash, conservative

_SENIORITY_PATTERNS = [
    ("executive", re.compile(r"\b(director|vp|vice president|head of|chief|cto|ceo|cio|cfo)\b", re.I)),
    ("staff", re.compile(r"\b(staff|principal|tech lead|architect|distinguished|fellow)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|iii)\b", re.I)),
    ("entry", re.compile(r"\b(intern|entry|junior|jr\.?|trainee|associate|graduate|new[\s-]grad)\b", re.I)),
    ("mid", re.compile(r"\b(mid|intermediate|ii)\b", re.I)),
]


def _infer_seniority(title: str) -> Optional[str]:
    """Heuristic title→canonical-seniority mapping. Returns None when unsure."""
    if not isinstance(title, str):
        return None
    for level, pattern in _SENIORITY_PATTERNS:
        if pattern.search(title):
            return level
    return None


def _is_salary_sane(row) -> bool:
    """Drop jobs with absurd salary values (upstream-feed bugs)."""
    min_amt = row.get("min_amount")
    if pd.notna(min_amt) and float(min_amt) > _SUSPICIOUS_SALARY_THRESHOLD:
        return False
    max_amt = row.get("max_amount")
    if pd.notna(max_amt) and float(max_amt) > _SUSPICIOUS_SALARY_THRESHOLD:
        return False
    return True


def _apply_post_filters(
    jobs_df: pd.DataFrame,
    *,
    min_salary: Optional[int],
    max_salary: Optional[int],
    seniority_level: Optional[List[str]],
) -> pd.DataFrame:
    """Apply post-scrape filters. Returns filtered DataFrame."""
    if jobs_df.empty:
        return jobs_df

    # Always: drop suspicious salaries
    jobs_df = jobs_df[jobs_df.apply(_is_salary_sane, axis=1)]

    # Salary floor
    if min_salary is not None:
        # Keep rows where min_amount >= min_salary OR salary unknown
        # (don't drop salary-less postings — they may match)
        mask = jobs_df["min_amount"].isna() | (jobs_df["min_amount"] >= min_salary)
        jobs_df = jobs_df[mask]

    # Salary ceiling
    if max_salary is not None:
        mask = jobs_df["max_amount"].isna() | (jobs_df["max_amount"] <= max_salary)
        jobs_df = jobs_df[mask]

    # Seniority — keep matching levels OR unknown (don't drop unknown by default)
    if seniority_level:
        wanted = {lv.lower().strip() for lv in seniority_level}
        def _row_matches(row):
            inferred = _infer_seniority(row.get("title", ""))
            if inferred is None:
                return True  # don't filter what we can't classify
            return inferred in wanted
        jobs_df = jobs_df[jobs_df.apply(_row_matches, axis=1)]

    return jobs_df.reset_index(drop=True)


def _format_jobs_json(
    jobs_df: pd.DataFrame,
    *,
    search_term: str,
    location: Optional[str],
    site_name: List[str],
    offset: int,
    results_wanted: int,
    concise: bool = False,
) -> str:
    """Serialize results as a structured JSON string for agent consumption."""
    jobs_list = []
    for _, job in jobs_df.iterrows():
        job_dict = {
            "title": _safe(job.get("title")),
            "company": _safe(job.get("company")),
            "location": _safe(job.get("location")),
            "site": _safe(job.get("site")),
            "job_url": _safe(job.get("job_url")),
            "job_type": _safe(job.get("job_type")),
            "is_remote": bool(job.get("is_remote")) if pd.notna(job.get("is_remote")) else None,
            "date_posted": str(job.get("date_posted")) if pd.notna(job.get("date_posted")) else None,
            "salary": {
                "min": _num(job.get("min_amount")),
                "max": _num(job.get("max_amount")),
                "currency": _safe(job.get("currency")),
                "interval": _safe(job.get("interval")),
            } if pd.notna(job.get("min_amount")) or pd.notna(job.get("max_amount")) else None,
        }
        # Verbose-only fields (omitted in concise mode)
        if not concise:
            job_dict["description"] = _safe(job.get("description"))
            job_dict["company_industry"] = _safe(job.get("company_industry"))
            job_dict["job_level"] = _safe(job.get("job_level"))
            job_dict["skills"] = _safe(job.get("skills"))
            job_dict["experience_range"] = _safe(job.get("experience_range"))
        # Drop null values for compactness
        job_dict = {k: v for k, v in job_dict.items() if v is not None}
        jobs_list.append(job_dict)

    salary_jobs = jobs_df[pd.notna(jobs_df.get("min_amount", pd.Series(dtype=float)))]
    summary = {
        "total_returned": len(jobs_list),
        "search_term": search_term,
        "location": location,
        "sites_searched": list(site_name),
        "remote_count": int((jobs_df.get("is_remote", pd.Series(dtype=bool)) == True).sum()),
        "salary_known_count": len(salary_jobs),
    }
    if len(salary_jobs) > 0:
        summary["avg_min_salary"] = float(salary_jobs["min_amount"].mean())
        summary["avg_max_salary"] = float(salary_jobs["max_amount"].mean())

    return json.dumps({
        "jobs": jobs_list,
        "summary": summary,
        "pagination": {
            "offset": offset,
            "results_returned": len(jobs_list),
            "next_offset_hint": offset + max(len(jobs_list), results_wanted),
        },
    }, default=str, indent=2)


def _safe(v):
    """Return v or None if pandas NaN/null."""
    if v is None:
        return None
    if pd.isna(v):
        return None
    return v


def _num(v):
    """Return v as float or None."""
    if v is None or pd.isna(v):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None



@mcp.tool()
async def scrape_jobs_tool(
    search_term: str,
    ctx: Context,
    location: Optional[str] = None,
    site_name: List[str] = [
        "linkedin", "indeed", "glassdoor", "zip_recruiter", "google",
        "hiring_cafe", "wellfound", "collab_work", "trueup",
        "greenhouse", "ashby", "workday", "lever", "usajobs",
        "clearance_jobs", "kforce", "insight_global",
        "adzuna", "jooble", "findwork", "the_muse",
        "bayt", "naukri",
    ],
    results_wanted: int = 15,
    job_type: Optional[str] = None,
    is_remote: bool = False,
    hours_old: Optional[int] = None,
    distance: int = 50,
    easy_apply: bool = False,
    country_indeed: str = "usa",
    linkedin_fetch_description: bool = False,
    offset: int = 0,
    verbose: int = 1,
    min_salary: Optional[int] = None,
    max_salary: Optional[int] = None,
    seniority_level: Optional[List[str]] = None,
    output_format: str = "markdown",
    concise: bool = False,
) -> str:
    """Search 23 job boards in one call. Returns normalized results
    (title, company, location, salary, job_type, date_posted) as a
    markdown report or JSON.

    ## Quick start

    ```
    scrape_jobs_tool(
        search_term="senior python engineer",
        location="New York, NY",
        site_name=["hiring_cafe", "indeed"],
        results_wanted=10,
        min_salary=180000,
        seniority_level=["senior", "staff"],
        output_format="json",
    )
    ```

    ## Available sites — all 23 hit by default

    `site_name` defaults to all 23 sources for max coverage. Override it
    only when you specifically want to narrow the search (faster /
    region-specific / niche).

    - **Broad mainstream**: `linkedin`, `indeed`, `glassdoor`, `google`,
      `zip_recruiter`
    - **AI-curated broad** (best general-purpose, ~140 jobs/page,
      AI-tagged with seniority/comp/skills): `hiring_cafe`
    - **Startup jobs** (50k+ AngelList-era startup roles): `wellfound`
    - **Community/newsletter aggregator** (curated, fastest):
      `collab_work`
    - **Tech-startup curated** (with company-trajectory + valuation +
      layoff signals): `trueup`
    - **Company-direct** (any Greenhouse-hosted board via Google
      site: dorks): `greenhouse`
    - **Government/federal** (US): `usajobs`
    - **Cleared roles** (security clearance required): `clearance_jobs`
    - **Staffing agencies**: `kforce`, `insight_global`
    - **Free aggregator APIs**: `adzuna`, `jooble`, `findwork`,
      `the_muse`
    - **Regional**: `bayt` (Middle East), `naukri` (India)

    ## Picking sites for common queries

    - "remote startup engineer" → `["wellfound", "hiring_cafe"]`
    - "software engineer in [city]" → `["indeed", "linkedin",
      "hiring_cafe", "greenhouse"]`
    - "federal / cleared role" → `["usajobs", "clearance_jobs"]`
    - "general broad search, max coverage" → `["hiring_cafe",
      "indeed"]` (least overlap)
    - "fastest results" → `["collab_work"]` (~280 ms/call)
    - "specific company hiring" → `["greenhouse"]` with company in
      `search_term`

    Args:
        search_term: Job keywords (e.g., "site reliability engineer").
            Required.
        site_name: List of sites from the catalog above. Override the
            default for better coverage on niche queries.
        location: City/state, "Remote", or country (e.g.,
            "Atlanta, GA"). Optional.
        results_wanted: Number of jobs to return (default 15).
        job_type: One of "fulltime", "parttime", "internship",
            "contract".
        is_remote: True to filter remote-only.
        hours_old: Only return jobs posted in last N hours.
        distance: Search radius in miles from location.
        easy_apply: True to filter to easy-apply only.
        country_indeed: Country for indeed/glassdoor (default "usa").
        linkedin_fetch_description: True for full LinkedIn descriptions
            (slower).
        offset: Skip first N results (pagination).
        verbose: 0=errors, 1=warnings, 2=info.
        min_salary: Drop jobs with min_amount below this. Jobs with
            unknown salary are kept (not all postings list comp).
        max_salary: Drop jobs with max_amount above this.
        seniority_level: Filter by inferred seniority. Any of
            ["entry", "mid", "senior", "staff", "executive"]. Jobs
            whose title doesn't classify cleanly are kept (don't drop
            ambiguous titles).
        output_format: "markdown" (default, human-readable) or "json"
            (structured `{jobs:[...], summary:{...}, pagination:{...}}`,
            recommended for agent/automation use).
        concise: True drops description previews, industry, job_level,
            skills, experience_range, company_rating, and emoji from
            the output. Saves ~70% of output tokens — recommended for
            agents with smaller context windows. Same job results are
            returned (only the formatting changes); no filtering effect.

    Returns:
        Markdown report (default) or JSON string (when output_format
        is "json"). Both include jobs, summary stats, and pagination
        hint to call again with offset=next_offset.
    """
    try:
        logger.info(f"Starting job search for: {search_term}")

        # Defensive coercion — smaller models commonly pass single strings
        # where a list is expected, stringified booleans/ints, or
        # capitalized values. Accept all rather than fail with a confusing
        # schema error.
        if isinstance(site_name, str):
            site_name = [site_name]
        if isinstance(seniority_level, str):
            seniority_level = [seniority_level]
        if seniority_level:
            seniority_level = [s.lower().strip() if isinstance(s, str) else s for s in seniority_level]

        # Coerce stringified booleans + ints
        is_remote = _coerce_bool(is_remote)
        easy_apply = _coerce_bool(easy_apply)
        linkedin_fetch_description = _coerce_bool(linkedin_fetch_description)
        concise = _coerce_bool(concise)
        results_wanted = _coerce_int(results_wanted) or 15
        offset = _coerce_int(offset) or 0
        distance = _coerce_int(distance) or 50
        verbose = _coerce_int(verbose) if verbose is not None else 1
        if hours_old is not None:
            hours_old = _coerce_int(hours_old)
        if min_salary is not None:
            min_salary = _coerce_int(min_salary)
        if max_salary is not None:
            max_salary = _coerce_int(max_salary)

        # Auto-detect remote when location says so — common natural-language
        # pattern ("Find remote SWE jobs" → location='Remote'). Set the flag
        # on the user's behalf so the right scraper paths fire.
        if location and isinstance(location, str) and location.strip().lower() in _REMOTE_LOCATION_ALIASES:
            is_remote = True
            location = None  # don't pass "Remote" as a city to scrapers

        # Apply site-name aliases (angel_list → wellfound, ziprecruiter → zip_recruiter, etc.)
        site_name = [_SITE_ALIASES.get(s.lower().strip(), s) for s in site_name if isinstance(s, str)]

        # Validate site names
        valid_sites = ["linkedin", "indeed", "glassdoor", "zip_recruiter", "google", "bayt", "naukri", "usajobs", "adzuna", "jooble", "findwork", "the_muse", "insight_global", "clearance_jobs", "kforce", "greenhouse", "ashby", "workday", "lever", "collab_work", "wellfound", "hiring_cafe", "trueup"]
        invalid_sites = [site for site in site_name if site not in valid_sites]
        if invalid_sites:
            # Fuzzy-match suggestions help the model recover on retry instead
            # of giving up after a typo.
            suggestions = {}
            for bad in invalid_sites:
                matches = difflib.get_close_matches(str(bad).lower(), valid_sites, n=1, cutoff=0.55)
                if matches:
                    suggestions[bad] = matches[0]
            msg = f"Error: Invalid site names: {invalid_sites}."
            if suggestions:
                msg += f" Did you mean: {suggestions}?"
            msg += f" Valid sites: {valid_sites}"
            return msg

        # Call jobdrop scrape_jobs function
        jobs_df = scrape_jobs(
            site_name=site_name,
            search_term=search_term,
            location=location,
            results_wanted=results_wanted,
            job_type=job_type,
            is_remote=is_remote,
            hours_old=hours_old,
            distance=distance,
            easy_apply=easy_apply,
            country_indeed=country_indeed,
            linkedin_fetch_description=linkedin_fetch_description,
            offset=offset,
            verbose=verbose,
            description_format="markdown"
        )
        
        # Apply post-scrape filters (sanity check + salary + seniority)
        original_count = len(jobs_df)
        jobs_df = _apply_post_filters(
            jobs_df,
            min_salary=min_salary,
            max_salary=max_salary,
            seniority_level=seniority_level,
        )
        filtered_count = len(jobs_df)
        if filtered_count < original_count:
            logger.info(
                f"Post-filters dropped {original_count - filtered_count} of "
                f"{original_count} jobs (sanity/salary/seniority)"
            )

        if jobs_df.empty:
            msg = "No jobs found matching your criteria. Try adjusting your search parameters."
            if original_count > 0:
                msg += f" (Note: {original_count} raw results dropped by post-filters.)"
            if output_format == "json":
                return json.dumps({"jobs": [], "summary": {"total_returned": 0, "note": msg}, "pagination": {"offset": offset, "results_returned": 0}})
            return msg

        # JSON output for agent/automation use
        if output_format == "json":
            return _format_jobs_json(
                jobs_df,
                search_term=search_term,
                location=location,
                site_name=site_name,
                offset=offset,
                results_wanted=results_wanted,
                concise=concise,
            )

        # Format results (markdown — default)
        prefix = "" if concise else "🎯 "
        results_summary = f"{prefix}Found {len(jobs_df)} jobs for '{search_term}'"
        if location:
            results_summary += f" in {location}"
        
        # Create detailed job listings
        job_listings = []
        for i, (_, job) in enumerate(jobs_df.iterrows(), 1):
            job_info = []
            
            # Basic job info
            job_info.append(f"## {i}. {job.get('title', 'N/A')}")
            job_info.append(f"**Company:** {job.get('company', 'N/A')}")
            job_info.append(f"**Location:** {job.get('location', 'N/A')}")
            job_info.append(f"**Source:** {job.get('site', 'N/A').title()}")
            
            # Job details
            if pd.notna(job.get('job_type')):
                job_info.append(f"**Type:** {job.get('job_type')}")
            
            if pd.notna(job.get('date_posted')):
                job_info.append(f"**Posted:** {job.get('date_posted')}")
            
            # Salary information
            if pd.notna(job.get('min_amount')) and pd.notna(job.get('max_amount')):
                currency = job.get('currency', 'USD')
                interval = job.get('interval', 'yearly')
                salary_range = f"${job.get('min_amount'):,.0f} - ${job.get('max_amount'):,.0f} {currency} ({interval})"
                job_info.append(f"**Salary:** {salary_range}")
            
            # Remote work
            if job.get('is_remote'):
                marker = "**Remote**" if concise else "🏠 **Remote work available**"
                job_info.append(marker)

            # Job URL
            if pd.notna(job.get('job_url')):
                job_info.append(f"**Apply:** {job.get('job_url')}")

            # Verbose-only fields (skipped in concise mode for ~70% token reduction)
            if not concise:
                # Description preview
                if pd.notna(job.get('description')):
                    desc = str(job.get('description'))
                    if len(desc) > 300:
                        desc = desc[:300] + "..."
                    job_info.append(f"**Description:** {desc}")

                if pd.notna(job.get('company_industry')):
                    job_info.append(f"**Industry:** {job.get('company_industry')}")

                if pd.notna(job.get('job_level')):
                    job_info.append(f"**Level:** {job.get('job_level')}")

                if pd.notna(job.get('skills')):
                    job_info.append(f"**Skills:** {job.get('skills')}")

                if pd.notna(job.get('experience_range')):
                    job_info.append(f"**Experience:** {job.get('experience_range')}")

                if pd.notna(job.get('company_rating')):
                    job_info.append(f"**Company Rating:** {job.get('company_rating')}/5")

            job_listings.append("\n".join(job_info))
        
        # Combine everything
        full_response = f"{results_summary}\n\n" + "\n\n---\n\n".join(job_listings)
        
        # Add summary statistics
        summary_marker = "## Search Summary" if concise else "## 📊 Search Summary"
        full_response += f"\n\n---\n\n{summary_marker}\n"
        full_response += f"- **Total jobs found:** {len(jobs_df)}\n"
        full_response += f"- **Sites searched:** {', '.join(site_name)}\n"
        full_response += f"- **Remote jobs:** {len(jobs_df[jobs_df.get('is_remote', False) == True])}\n"
        
        # Salary statistics
        salary_jobs = jobs_df[pd.notna(jobs_df.get('min_amount', pd.Series([None] * len(jobs_df))))]
        if len(salary_jobs) > 0:
            avg_min = salary_jobs['min_amount'].mean()
            avg_max = salary_jobs['max_amount'].mean()
            full_response += f"- **Jobs with salary info:** {len(salary_jobs)}\n"
            full_response += f"- **Average salary range:** ${avg_min:,.0f} - ${avg_max:,.0f}\n"

        # Pagination hint — agents commonly fail to paginate without explicit guidance
        next_offset = offset + max(len(jobs_df), results_wanted)
        hint_marker = "**More results?**" if concise else "💡 **More results?**"
        full_response += (
            f"\n{hint_marker} Call again with `offset={next_offset}` "
            f"(currently at offset={offset}, returned {len(jobs_df)} jobs).\n"
        )

        logger.info(f"Successfully found {len(jobs_df)} jobs")
        return full_response
        
    except Exception as e:
        logger.error(f"Error scraping jobs: {e}")
        await ctx.error(f"Job search failed: {str(e)}")
        return f"Error scraping jobs: {str(e)}"


@mcp.tool()
def get_supported_countries() -> str:
    """
    Get list of supported countries for job searches.
    
    Returns:
        Formatted list of all supported countries with their identifiers
    """
    try:
        countries = []
        for country in Country:
            country_names = country.value[0]
            countries.append(f"- **{country.name}**: {country_names}")
        
        response = "## 🌍 Supported Countries for Job Searches\n\n"
        response += "\n".join(sorted(countries))
        response += "\n\n**Note:** Use the country name or code as shown above for the `country_indeed` parameter."
        response += "\n\n**Popular Options:**\n"
        response += "- usa, us, united states\n"
        response += "- uk, united kingdom\n" 
        response += "- canada\n"
        response += "- australia\n"
        response += "- germany\n"
        response += "- france\n"
        response += "- india\n"
        response += "- singapore\n"
        
        return response
    except Exception as e:
        logger.error(f"Error getting supported countries: {e}")
        return f"Error getting supported countries: {str(e)}"


@mcp.tool()
def get_supported_sites() -> str:
    """
    Get list of supported job board sites with descriptions.
    
    Returns:
        Formatted list of all supported job boards with descriptions
    """
    try:
        sites_info = {
            # Broad mainstream
            "linkedin": "LinkedIn — professional network. High-quality listings, strict rate limits. Set linkedin_fetch_description=true for full JDs (slower).",
            "indeed": "Indeed — global aggregator. Most reliable + highest volume. Best general-purpose source.",
            "glassdoor": "Glassdoor — listings + company reviews + salary data.",
            "zip_recruiter": "ZipRecruiter — US/Canada-focused.",
            "google": "Google Jobs — SERP aggregation. Use very specific search terms.",
            # AI-enriched + curated
            "hiring_cafe": "Hiring Cafe — AI-curated, ~140 jobs/page with rich tags (seniority, comp, skills, workplace_type). Best general-purpose broad search.",
            "wellfound": "Wellfound (formerly AngelList) — 50k+ startup roles.",
            "collab_work": "CollabWork — community/newsletter aggregator, ~2k curated roles, fastest source (~280ms).",
            "trueup": "TrueUp — tech-startup-focused. Adds company-trajectory score, valuation, funding stage, and layoff/health flags into job description. Direct ATS apply URLs. Pure HTTP, sub-second.",
            # Company-direct + government
            "greenhouse": "Greenhouse — any greenhouse-hosted board (most YC-stage and Series A+ companies). 3-layer staleness filter (404 / past deadline / 90-day age cap).",
            "ashby": "Ashby — any Ashby-hosted board (OpenAI, Notion, Linear, Ramp, Mercury, Vercel, etc.). Google-dorked discovery + GraphQL enrichment.",
            "workday": "Workday — Fortune-500-heavy ATS (NVIDIA, Salesforce, Disney, Comcast, JPMorgan, Lockheed, etc.). Google-dorked discovery + CXS API enrichment.",
            "lever": "Lever — any Lever-hosted board (Plaid, HashiCorp, Kraken, Spotify, etc.). Google-dorked discovery + REST enrichment.",
            "usajobs": "USAJobs — US federal government roles. Public API.",
            # Staffing
            "clearance_jobs": "ClearanceJobs (DHI) — security-cleared roles. Full JD, salary, structured job_type.",
            "kforce": "Kforce — staffing agency. Fast direct backend.",
            "insight_global": "Insight Global — staffing agency. Server-rendered listings.",
            # Free aggregator APIs
            "adzuna": "Adzuna — free aggregator API, 100% salary fill rate (predicted when missing).",
            "jooble": "Jooble — free aggregator API, 60+ countries.",
            "findwork": "Findwork.dev — developer-focused aggregator API.",
            "the_muse": "The Muse — culture-forward aggregator API.",
            # Regional
            "bayt": "Bayt — Middle East focused job portal.",
            "naukri": "Naukri — India's leading job portal. Includes skills, experience_range, company_rating.",
        }

        response = "## 🔗 Supported Job Board Sites (23 total)\n\n"
        for site, description in sites_info.items():
            response += f"- **`{site}`**: {description}\n"

        response += "\n## 💡 Usage Tips\n"
        response += "- **General broad search**: `[\"hiring_cafe\", \"indeed\"]` — least overlap, most coverage.\n"
        response += "- **Startup roles**: `[\"wellfound\", \"hiring_cafe\"]`.\n"
        response += "- **Government/cleared**: `[\"usajobs\", \"clearance_jobs\"]`.\n"
        response += "- **Specific company**: `[\"greenhouse\"]` with the company name in `search_term`.\n"
        response += "- **Fastest single-source**: `[\"collab_work\"]` (~280ms/call).\n"
        response += "- **Regional**: include `bayt` (Middle East), `naukri` (India) as needed.\n"
        response += "- **Rate limiting**: LinkedIn most restrictive; Indeed most reliable; collab_work fastest.\n"

        return response
    except Exception as e:
        logger.error(f"Error getting supported sites: {e}")
        return f"Error getting supported sites: {str(e)}"


@mcp.tool()
def get_job_search_tips() -> str:
    """
    Get helpful tips and best practices for job searching with jobdrop.
    
    Returns:
        Comprehensive guide with tips for effective job searching
    """
    return """## 🎯 jobdrop Job Search Tips & Best Practices

### 🔍 **Search Term Optimization**
- **Be specific**: "Python developer" vs "developer"
- **Use quotes for exact phrases**: "machine learning engineer"
- **Try variations**: "software engineer", "software developer", "programmer"
- **Include technologies**: "React developer", "AWS engineer"
- **Consider levels**: "senior", "junior", "lead", "principal"

### 📍 **Location Strategies**
- **Remote jobs**: Use `is_remote=true` or location="Remote"
- **Specific cities**: "San Francisco, CA", "New York, NY"
- **State/Country**: "California", "Texas", "United Kingdom"
- **Multiple locations**: Run separate searches for different cities

### 🏢 **Site Selection Guide** (23 sites total — see `get_supported_sites`)
- **Start small**: 2-3 sites is plenty for a good query
- **Best general-purpose**: `hiring_cafe` (~140 AI-tagged jobs/page) + `indeed` (broadest mainstream)
- **Startup roles**: `wellfound` + `hiring_cafe`
- **Federal / cleared**: `usajobs` + `clearance_jobs`
- **Specific company**: `greenhouse` with company name in `search_term`
- **Fastest single-source**: `collab_work` (~280ms/call)
- **Regional**: `bayt` (Middle East), `naukri` (India)
- **LinkedIn**: best quality but strict rate limits

### ⚡ **Performance Tips**
- **Start with 10-20 results** then increase if needed
- **Use `hours_old` parameter** to find recent postings (24, 48, 72 hours)
- **Enable `linkedin_fetch_description=true`** only when needed (slower)
- **Use `offset` parameter** for pagination through large result sets

### 🎛️ **Advanced Filtering**
- **Job types**: fulltime, parttime, internship, contract
- **Easy apply**: `easy_apply=true` for quick applications
- **Distance**: Adjust radius for location-based searches
- **Country**: Specify country for Indeed/Glassdoor searches

### 🚨 **Common Issues & Solutions**
- **No results**: Try broader search terms or different sites
- **Rate limiting**: Reduce results_wanted, add delays between searches
- **LinkedIn blocks**: Use fewer requests, try different proxies
- **Slow searches**: Disable LinkedIn description fetching

### 📊 **Sample Search Strategies**

**For Remote Work:**
```
search_term="software engineer"
location="Remote"
is_remote=true
site_name=["indeed", "zip_recruiter"]
```

**For Local Jobs:**
```
search_term="marketing manager"
location="Austin, TX" 
distance=25
site_name=["indeed", "glassdoor"]
```

**For Recent Postings:**
```
search_term="data scientist"
hours_old=48
site_name=["linkedin", "indeed"]
linkedin_fetch_description=true
```

**For Entry Level:**
```
search_term="junior developer OR entry level programmer"
job_type="fulltime"
easy_apply=true
```

### 🔄 **Iterative Search Process**
1. Start with broad terms and few sites
2. Analyze initial results
3. Refine search terms based on findings
4. Expand to more sites if needed
5. Use different job boards for comparison

Happy job hunting! 🚀"""


# Entry point for running the server
def main():
    """Run the jobdrop MCP server.

    Default transport is stdio (single-client subprocess mode used by most
    MCP clients). Set JOBDROP_TRANSPORT=sse to run as a long-lived HTTP/SSE
    server suitable for a launchd / systemd daemon — host and port are
    configurable via JOBDROP_HOST (default 127.0.0.1) and JOBDROP_PORT
    (default 9090).
    """
    import os

    logger.info("Starting jobdrop MCP Server...")
    logger.info("Server is ready and waiting for MCP client connections...")
    logger.info("Use Ctrl+C to stop the server")
    try:
        transport = os.environ.get("JOBDROP_TRANSPORT", "stdio")
        logger.info(f"Using transport: {transport}")
        if transport == "sse":
            mcp.settings.host = os.environ.get("JOBDROP_HOST", "127.0.0.1")
            mcp.settings.port = int(os.environ.get("JOBDROP_PORT", "9090"))
            mcp.run(transport="sse")
        else:
            mcp.run(transport="stdio")
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")


if __name__ == "__main__":
    main()
