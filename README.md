# jobdrop

A multi-source job scraper. Hits 20 job boards in one call, normalizes
the results into a pandas DataFrame, and ships with anti-bot handling
for the boards that block standard scrapers.

> **Maintainer**: this project is maintained by **[kbwhodat](https://github.com/kbwhodat)**. Substantially extended from the original [`cullenwatson/JobSpy`](https://github.com/cullenwatson/JobSpy) (MIT licensed) with new sources, an integrated MCP server, salary/seniority filters, and reliability fixes across all scrapers.

## What's in here

### 20 sources

| `site_name` | Source | Notes |
|---|---|---|
| `linkedin` | LinkedIn | Public listings + optional detail-page enrichment |
| `indeed` | Indeed | GraphQL with per-company cap + paginate-until-quota |
| `glassdoor` | Glassdoor | Listings + company reviews + salary data |
| `google` | Google Jobs | SERP aggregation across many sources |
| `zip_recruiter` | ZipRecruiter | US/Canada-focused |
| `hiring_cafe` | Hiring Cafe | AI-curated, ~140 jobs/page with rich tags (seniority, comp, skills, workplace_type) |
| `wellfound` | Wellfound (formerly AngelList) | 50k+ startup roles |
| `collab_work` | CollabWork | Community/newsletter aggregator (~2k curated roles, fastest source) |
| `greenhouse` | Greenhouse-hosted boards | Most YC and Series A+ companies; 3-layer staleness filter |
| `bayt` | Bayt | Middle East focused |
| `naukri` | Naukri | India's largest job portal |
| `bdjobs` | BDJobs | Bangladesh's premier job portal |
| `usajobs` | USAJobs.gov | US federal public API |
| `adzuna` | Adzuna | Public API, 100% salary fill rate |
| `jooble` | Jooble | Public API, 60+ countries |
| `findwork` | Findwork.dev | Developer-focused public API |
| `the_muse` | The Muse | Culture-forward public API |
| `insight_global` | Insight Global staffing | Server-rendered listings |
| `clearance_jobs` | ClearanceJobs (DHI) | Security-cleared roles, full JD + salary + structured job_type |
| `kforce` | Kforce staffing | Direct backend API for fast results |

## Installation

### As a Python library

```
pip install -U jobdrop
```

Python ≥ 3.10 required.

### As an MCP server (Claude Desktop / Claude Code / Cursor / Cline / opencode)

Install the binary once with `uv tool install` (or `pipx install`):

```
uv tool install "jobdrop[mcp]"
# or:  pipx install "jobdrop[mcp]"
```

Then add to your MCP client config.

**Claude Desktop / Claude Code / Cursor / Cline** — `~/Library/Application Support/Claude/claude_desktop_config.json` (or equivalent):

```json
{
  "mcpServers": {
    "jobdrop": {
      "command": "jobdrop-mcp-server"
    }
  }
}
```

**opencode** — `~/.config/opencode/opencode.json` (or `.opencode/opencode.json` in your project):

```json
{
  "mcp": {
    "jobdrop": {
      "type": "local",
      "command": ["jobdrop-mcp-server"],
      "enabled": true
    }
  }
}
```

That's it — the client launches `jobdrop-mcp-server` as a stdio subprocess on demand. No daemon, no port.

> **Note**: prefer the `uv tool install` path so the binary lands in PATH and the client launches it directly — same pattern as reference MCP servers (filesystem, git, etc.).

## Usage

```python
from jobdrop import scrape_jobs

jobs = scrape_jobs(
    site_name=["insight_global", "clearance_jobs", "kforce", "greenhouse",
               "linkedin", "indeed", "google"],
    search_term="site reliability engineer",
    location="Atlanta, GA",
    results_wanted=20,
    hours_old=720,          # 30-day freshness cap
    country_indeed="usa",
)
print(f"Found {len(jobs)} jobs")
print(jobs[["site", "title", "company", "location", "min_amount", "max_amount", "job_url"]].head())
```

## Parameters

```
scrape_jobs(
  site_name              list[str] | str — any of the 20 sources above (default: all)
  search_term            str        — keyword query
  google_search_term     str        — Google Jobs override (only filter for `google`)
  location               str        — "City, ST" or ZIP. Each scraper geocodes its own way.
  distance               int        — radius miles, default 50
  is_remote              bool       — remote-only filter (where supported)
  job_type               str        — "fulltime" | "parttime" | "contract" | "internship"
  easy_apply             bool       — direct-board apply only (where supported)
  results_wanted         int        — per-site target
  offset                 int        — pagination offset
  hours_old              int        — drop postings older than N hours
  country_indeed         str        — Indeed/Glassdoor country (see list below)
  description_format     str        — "markdown" | "html"
  enforce_annual_salary  bool       — convert hourly/monthly to yearly
  linkedin_fetch_description  bool  — full JD + direct URL (slower)
  linkedin_company_ids   list[int]  — filter LinkedIn by company IDs
  proxies                list[str]  — round-robin proxies, "user:pass@host:port"
  ca_cert                str        — CA cert path for proxies
  user_agent             str        — override the default UA
  verbose                int        — 0 errors / 1 warnings / 2 all
)
```

### Per-scraper limitations

- **Indeed** — only one of `hours_old` / (`job_type`+`is_remote`) / `easy_apply` per call.
- **LinkedIn** — only one of `hours_old` / `easy_apply` per call.
- **ClearanceJobs** — location/remote filters require facet IDs from the dropdown endpoints (not implemented). Filter client-side or scope by keyword.
- **InsightGlobal** — does not expose client-company name (it's the staffing firm). `is_remote` is not available in their data.
- **Greenhouse** — Google indexes some postings after they're filled. Stale 404s are filtered out; the freshness cutoff filters "live but ancient" postings (default 90 days, override with `hours_old`).

## JobPost schema

```
JobPost
├── id, title, company_name, company_url, job_url
├── location { country, city, state }
├── description
├── is_remote
├── date_posted
├── job_type        fulltime | parttime | contract | internship
├── compensation
│   ├── interval   yearly | monthly | weekly | daily | hourly
│   ├── min_amount, max_amount, currency
│   └── salary_source
├── job_level                                  (LinkedIn, ClearanceJobs)
├── company_industry                           (LinkedIn, Indeed, Greenhouse, Kforce)
├── company_country, company_addresses,
│   company_employees_label, company_revenue_label,
│   company_description, company_logo          (Indeed)
├── skills, experience_range,
│   company_rating, company_reviews_count,
│   vacancy_count, work_from_home_type         (Naukri)
└── emails
```

## Indeed / Glassdoor country list

Pass `country_indeed` (use the exact name; `*` = also supported on Glassdoor):

| | | | |
|---|---|---|---|
| Argentina | Australia* | Austria* | Bahrain |
| Belgium* | Brazil* | Canada* | Chile |
| China | Colombia | Costa Rica | Czech Republic |
| Denmark | Ecuador | Egypt | Finland |
| France* | Germany* | Greece | Hong Kong* |
| Hungary | India* | Indonesia | Ireland* |
| Israel | Italy* | Japan | Kuwait |
| Luxembourg | Malaysia | Mexico* | Morocco |
| Netherlands* | New Zealand* | Nigeria | Norway |
| Oman | Pakistan | Panama | Peru |
| Philippines | Poland | Portugal | Qatar |
| Romania | Saudi Arabia | Singapore* | South Africa |
| South Korea | Spain* | Sweden | Switzerland* |
| Taiwan | Thailand | Turkey | Ukraine |
| United Arab Emirates | UK* | USA* | Uruguay |
| Venezuela | Vietnam* | | |

LinkedIn searches globally and uses only `location`. ZipRecruiter is US/Canada and uses only `location`. Bayt searches internationally with only `search_term`.

## Notes

- Most boards cap a single search at ~1000 results.
- LinkedIn rate-limits aggressively around the 10th page of pagination on a single IP. Use `proxies`.
- For Indeed search-term tuning: it searches the description too. Use `-foo` to exclude, `"exact phrase"` for exact match. Example:
  ```python
  search_term='"site reliability engineer" (kubernetes OR terraform) -recruiter'
  ```
- For Google: copy the exact filter syntax from a real Google Jobs search and pass it as `google_search_term`.
- For Greenhouse: keyword + location are passed straight to a Google `site:greenhouse.io` query, so Boolean operators and quotes work. Don't quote the full `"City, ST"` — quote the city alone, leave the state bare.

## License

MIT. See `LICENSE`.
