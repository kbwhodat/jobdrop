# jobdrop

A multi-source job scraper. Hits 20 job boards in one call, normalizes
the results into a pandas DataFrame, and ships with anti-bot bypasses
for the boards that block standard scrapers.

## What's in here

### 17 sources

| `site_name` | Source | Mechanism |
|---|---|---|
| `linkedin` | LinkedIn | Public listing scrape with optional detail-page enrichment |
| `indeed` | Indeed | GraphQL with the `Int!` radius fix + per-company cap + paginate-until-quota |
| `glassdoor` | Glassdoor | selenium-driverless headless to defeat Cloudflare 403; in-page GraphQL fetch |
| `google` | Google Jobs | selenium-driverless headless against `udm=8`; SERP DOM walk |
| `zip_recruiter` | ZipRecruiter | `curl_cffi` + `safari17_2_ios` TLS impersonation against the web HTML endpoint |
| `bayt` | Bayt | Public scrape |
| `naukri` | Naukri | Public scrape |
| `bdjobs` | BDJobs | Public scrape |
| `usajobs` | USAJobs.gov | Federal public API |
| `adzuna` | Adzuna | Public API |
| `jooble` | Jooble | Public API |
| `findwork` | Findwork.dev | Public API |
| `the_muse` | The Muse | Public API |
| `insight_global` | Insight Global staffing | Server-rendered HTML scrape with hidden JSON blob per result |
| `clearance_jobs` | ClearanceJobs (DHI) | Public JSON API + parallel detail-page enrichment for full JD, salary, type, remote bool |
| `kforce` | Kforce staffing | Direct Azure Cognitive Search calls (bypasses Imperva on the public host) |
| `greenhouse` | Greenhouse-hosted boards | Google `site:` dorks via selenium-driverless → public Greenhouse API; 3-layer staleness filter |

### Anti-bot solved

- **Google** — selenium-driverless cold-start headless. Defeats the 2026 CAPTCHA wall that takes out Playwright / undetected-chromedriver / nodriver / patchright.
- **Glassdoor** — selenium-driverless rewrite to bypass Cloudflare 403; URL-encoded location, partial-GraphQL-error tolerance.
- **ZipRecruiter** — `curl_cffi` + `safari17_2_ios` against the web HTML endpoint. The iOS-app API is dead behind Cloudflare.
- **Kforce** — bypasses Imperva on the public host by calling the Azure Cognitive Search backend directly.
- **Greenhouse** — uses the same selenium-driverless infrastructure as Google for `site:` dorks across all greenhouse-hosted boards.

### Other tightening

- **LinkedIn** — salary extraction from description body, optional per-company cap, parallel detail fetches.
- **Indeed** — fixed `radius=25` default after Indeed promoted the GraphQL field to `Int!`; per-company cap to surface diverse employers; pagination loop fixed.
- **ClearanceJobs** — search API gives a 200-char preview; this fork parallel-fetches `/api/v1/jobs/{id}` so you get the full JD, salary range, structured `job_type`, and authoritative `remote` bool.
- **Greenhouse** — three layers of stale-protection (404 drop / past `application_deadline` / `first_published` age with 90-day default that respects `hours_old`).

### Bundled credentials

API keys for USAJobs, Adzuna, Jooble, Findwork, and The Muse are baked
into a positional resolver (`jobdrop/_defaults.py`) so the new sources
work without environment setup. User-set env vars still win via
`setdefault` semantics.

## Installation

```
pip install -U jobdrop
```

Python ≥ 3.10 required.

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
  site_name              list[str] | str — any of the 17 sources above (default: all)
  search_term            str        — keyword query
  google_search_term     str        — Google Jobs override (only filter for `google`)
  location               str        — "City, ST" or ZIP. Each scraper geocodes its own way.
  distance               int        — radius miles, default 50
  is_remote              bool       — remote-only filter (where supported)
  job_type               str        — "fulltime" | "parttime" | "contract" | "internship"
  easy_apply             bool       — direct-board apply only (LinkedIn easy-apply is broken)
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
