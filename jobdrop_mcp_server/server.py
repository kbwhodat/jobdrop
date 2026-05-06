#!/usr/bin/env python3
"""
Jobdrop MCP Server

An MCP server that provides job scraping capabilities using the Jobdrop library.
Built with FastMCP for modern MCP protocol compliance.
"""

import logging
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


@mcp.tool()
async def scrape_jobs_tool(
    search_term: str,
    ctx: Context,
    location: Optional[str] = None,
    site_name: List[str] = ["indeed", "linkedin", "zip_recruiter", "google"],
    results_wanted: int = 15,
    job_type: Optional[str] = None,
    is_remote: bool = False,
    hours_old: Optional[int] = None,
    distance: int = 50,
    easy_apply: bool = False,
    country_indeed: str = "usa",
    linkedin_fetch_description: bool = False,
    offset: int = 0,
    verbose: int = 1
) -> str:
    """Search 20 job boards in one call. Returns normalized results
    (title, company, location, salary, job_type, date_posted) as a
    markdown report.

    ## Available sites — pick what fits the query

    The default `site_name` is a conservative subset. **For most
    queries, override it explicitly** to get the right coverage.

    - **Broad mainstream**: `linkedin`, `indeed`, `glassdoor`, `google`,
      `zip_recruiter`
    - **AI-curated broad** (best general-purpose, ~140 jobs/page,
      AI-tagged with seniority/comp/skills): `hiring_cafe`
    - **Startup jobs** (50k+ AngelList-era startup roles): `wellfound`
    - **Community/newsletter aggregator** (curated, fastest):
      `collab_work`
    - **Company-direct** (any Greenhouse-hosted board via Google
      site: dorks): `greenhouse`
    - **Government/federal** (US): `usajobs`
    - **Cleared roles** (security clearance required): `clearance_jobs`
    - **Staffing agencies**: `kforce`, `insight_global`
    - **Free aggregator APIs**: `adzuna`, `jooble`, `findwork`,
      `the_muse`
    - **Regional**: `bayt` (Middle East), `naukri` (India), `bdjobs`
      (Bangladesh)

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

    Returns:
        Markdown-formatted job listings with title, company, location,
        salary range, job type, post date, apply URL, and description
        snippet.
    """
    try:
        logger.info(f"Starting job search for: {search_term}")

        # Validate site names
        valid_sites = ["linkedin", "indeed", "glassdoor", "zip_recruiter", "google", "bayt", "naukri", "bdjobs", "usajobs", "adzuna", "jooble", "findwork", "the_muse", "insight_global", "clearance_jobs", "kforce", "greenhouse", "collab_work", "wellfound", "hiring_cafe"]
        invalid_sites = [site for site in site_name if site not in valid_sites]
        if invalid_sites:
            return f"Error: Invalid site names: {invalid_sites}. Valid sites: {valid_sites}"

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
        
        if jobs_df.empty:
            return "No jobs found matching your criteria. Try adjusting your search parameters."
        
        # Format results
        results_summary = f"🎯 Found {len(jobs_df)} jobs for '{search_term}'"
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
                job_info.append("🏠 **Remote work available**")
            
            # Job URL
            if pd.notna(job.get('job_url')):
                job_info.append(f"**Apply:** {job.get('job_url')}")
            
            # Description preview
            if pd.notna(job.get('description')):
                desc = str(job.get('description'))
                # Limit description to 300 characters for readability
                if len(desc) > 300:
                    desc = desc[:300] + "..."
                job_info.append(f"**Description:** {desc}")
            
            # Additional fields for specific sites
            if pd.notna(job.get('company_industry')):
                job_info.append(f"**Industry:** {job.get('company_industry')}")
            
            if pd.notna(job.get('job_level')):
                job_info.append(f"**Level:** {job.get('job_level')}")
            
            # Naukri-specific fields
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
        full_response += f"\n\n---\n\n## 📊 Search Summary\n"
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
            "glassdoor": "Glassdoor — listings + company reviews + salary data. selenium-driverless used to defeat Cloudflare.",
            "zip_recruiter": "ZipRecruiter — US/Canada-focused. curl_cffi safari17_2_ios TLS impersonation.",
            "google": "Google Jobs — SERP `udm=8` aggregation. Use very specific search terms.",
            # AI-enriched + curated
            "hiring_cafe": "Hiring Cafe — AI-curated, ~140 jobs/page with rich tags (seniority, comp, skills, workplace_type). Best general-purpose broad search. selenium-driverless to defeat Cloudflare.",
            "wellfound": "Wellfound (formerly AngelList) — 50k+ startup roles. Camoufox engine to defeat DataDome on /role/* per-route block.",
            "collab_work": "CollabWork — community/newsletter aggregator, ~2k curated roles, fastest source (~280ms).",
            # Company-direct + government
            "greenhouse": "Greenhouse — any greenhouse-hosted board (most YC-stage and Series A+ companies). Google site: dorks via selenium-driverless. 3-layer staleness filter (404 / past deadline / 90-day age cap).",
            "usajobs": "USAJobs — US federal government roles. Public API.",
            # Staffing
            "clearance_jobs": "ClearanceJobs (DHI) — security-cleared roles. JSON API + parallel detail-page enrichment.",
            "kforce": "Kforce — staffing agency. Direct Azure Cognitive Search calls (bypasses Imperva on the public host).",
            "insight_global": "Insight Global — staffing agency. Server-rendered HTML with hidden JSON blob per result.",
            # Free aggregator APIs
            "adzuna": "Adzuna — free aggregator API, 100% salary fill rate (predicted when missing).",
            "jooble": "Jooble — free aggregator API, 60+ countries.",
            "findwork": "Findwork.dev — developer-focused aggregator API.",
            "the_muse": "The Muse — culture-forward aggregator API.",
            # Regional
            "bayt": "Bayt — Middle East focused job portal.",
            "naukri": "Naukri — India's leading job portal. Includes skills, experience_range, company_rating.",
            "bdjobs": "BDJobs — Bangladesh's premier job portal.",
        }

        response = "## 🔗 Supported Job Board Sites (20 total)\n\n"
        for site, description in sites_info.items():
            response += f"- **`{site}`**: {description}\n"

        response += "\n## 💡 Usage Tips\n"
        response += "- **General broad search**: `[\"hiring_cafe\", \"indeed\"]` — least overlap, most coverage.\n"
        response += "- **Startup roles**: `[\"wellfound\", \"hiring_cafe\"]`.\n"
        response += "- **Government/cleared**: `[\"usajobs\", \"clearance_jobs\"]`.\n"
        response += "- **Specific company**: `[\"greenhouse\"]` with the company name in `search_term`.\n"
        response += "- **Fastest single-source**: `[\"collab_work\"]` (~280ms/call).\n"
        response += "- **Regional**: include `bayt` (Middle East), `naukri` (India), `bdjobs` (Bangladesh) as needed.\n"
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

### 🏢 **Site Selection Guide** (20 sites total — see `get_supported_sites`)
- **Start small**: 2-3 sites is plenty for a good query
- **Best general-purpose**: `hiring_cafe` (~140 AI-tagged jobs/page) + `indeed` (broadest mainstream)
- **Startup roles**: `wellfound` + `hiring_cafe`
- **Federal / cleared**: `usajobs` + `clearance_jobs`
- **Specific company**: `greenhouse` with company name in `search_term`
- **Fastest single-source**: `collab_work` (~280ms/call)
- **Regional**: `bayt` (Middle East), `naukri` (India), `bdjobs` (Bangladesh)
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
