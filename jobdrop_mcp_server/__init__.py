"""jobdrop MCP Server package.

This package provides job scraping capabilities using the jobdrop library.
Built with FastMCP for modern MCP protocol compliance.
"""

# Import from the server module within the package
from .server import main, scrape_jobs_tool, get_supported_countries, get_supported_sites, get_job_search_tips

__all__ = [
    "main",
    "scrape_jobs_tool",
    "get_supported_countries",
    "get_supported_sites",
    "get_job_search_tips",
]
