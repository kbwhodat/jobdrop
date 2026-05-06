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
    """
    Search for jobs across multiple job boards including LinkedIn, Indeed, Glassdoor, 
    ZipRecruiter, Google Jobs, Bayt, Naukri, and BDJobs.
    
    Args:
        search_term: Job search keywords (e.g., 'software engineer', 'data scientist')
        ctx: MCP context for progress reporting
        location: Job location (e.g., 'San Francisco, CA', 'New York', 'Remote')
        site_name: Job boards to search from available options
        results_wanted: Number of job results to retrieve (1-1000)
        job_type: Type of employment ('fulltime', 'parttime', 'internship', 'contract')
        is_remote: Filter for remote jobs only
        hours_old: Filter jobs posted within the last N hours
        distance: Search radius in miles from location (1-100)
        easy_apply: Filter for jobs with easy apply options
        country_indeed: Country for Indeed/Glassdoor searches
        linkedin_fetch_description: Fetch full job descriptions from LinkedIn (slower)
        offset: Number of results to skip (for pagination)
        verbose: Logging verbosity (0=errors only, 1=warnings, 2=all logs)
    
    Returns:
        Formatted job search results with detailed job information
    """
    try:
        logger.info(f"Starting job search for: {search_term}")
        
        # Send progress update
        await ctx.info(f"Searching for '{search_term}' jobs...")
        
        # Validate site names
        valid_sites = ["linkedin", "indeed", "glassdoor", "zip_recruiter", "google", "bayt", "naukri", "bdjobs", "usajobs", "adzuna", "jooble", "findwork", "the_muse", "insight_global", "clearance_jobs", "kforce", "greenhouse", "collab_work", "wellfound", "hiring_cafe"]
        invalid_sites = [site for site in site_name if site not in valid_sites]
        if invalid_sites:
            return f"Error: Invalid site names: {invalid_sites}. Valid sites: {valid_sites}"
        
        # Report progress
        await ctx.report_progress(
            progress=0.1,
            total=1.0,
            message="Initializing job search..."
        )
        
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
        
        await ctx.report_progress(
            progress=0.8,
            total=1.0,
            message="Processing job results..."
        )
        
        if jobs_df.empty:
            await ctx.warning("No jobs found matching the search criteria")
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
        
        # Report completion
        await ctx.report_progress(
            progress=1.0,
            total=1.0,
            message="Job search completed!"
        )
        
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
        
        await ctx.info(f"Successfully found {len(jobs_df)} jobs")
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
            "linkedin": "LinkedIn - Professional networking platform with job listings (requires careful rate limiting)",
            "indeed": "Indeed - One of the largest job search engines globally (most reliable)",
            "glassdoor": "Glassdoor - Job listings with company reviews and salary information",
            "zip_recruiter": "ZipRecruiter - Job matching platform for US/Canada",
            "google": "Google Jobs - Aggregated job listings from Google (use specific search terms)",
            "bayt": "Bayt - Middle East focused job portal",
            "naukri": "Naukri - India's leading job portal with detailed job information",
            "bdjobs": "BDJobs - Bangladesh's premier job portal"
        }
        
        response = "## 🔗 Supported Job Board Sites\n\n"
        for site, description in sites_info.items():
            response += f"- **{site}**: {description}\n"
        
        response += "\n## 💡 Usage Tips\n"
        response += "- **Best for beginners**: Start with `[\'indeed\', \'zip_recruiter\']`\n"
        response += "- **For comprehensive search**: Use `[\'indeed\', \'linkedin\', \'glassdoor\', \'google\']`\n"
        response += "- **For specific regions**: Include regional sites like 'bayt', 'naukri', 'bdjobs'\n"
        response += "- **Rate limiting**: LinkedIn is most restrictive, Indeed is most reliable\n"
        
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

### 🏢 **Site Selection Guide**
- **Start small**: Begin with 2-3 sites to test your search
- **Indeed**: Most reliable, least rate limiting
- **LinkedIn**: Best quality but strict rate limits
- **ZipRecruiter**: Good for US/Canada
- **Google**: Use very specific search terms

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
