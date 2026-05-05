from __future__ import annotations

import math
from datetime import datetime
from typing import Tuple

from jobspy.indeed.constant import job_search_query, api_headers
from jobspy.indeed.util import is_job_remote, get_compensation, get_job_type
from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    Location,
    JobResponse,
    JobType,
    DescriptionFormat,
)
from jobspy.util import (
    extract_emails_from_text,
    markdown_converter,
    create_session,
    create_logger,
)

log = create_logger("Indeed")


class Indeed(Scraper):
    # Per-company result cap. On by default for diversity — observed in
    # the wild: a single employer (Amazon, ARMADA, etc.) can dominate the
    # top results with the same role posted to 5+ near-cities. Capping
    # at 3 keeps a representative sample per employer and frees the
    # quota for unrelated employers. Set to None to disable.
    max_results_per_company: int | None = 3

    def __init__(
        self, proxies: list[str] | str | None = None, ca_cert: str | None = None, user_agent: str | None = None
    ):
        """
        Initializes IndeedScraper with the Indeed API url
        """
        super().__init__(Site.INDEED, proxies=proxies)

        self.session = create_session(
            proxies=self.proxies, ca_cert=ca_cert, is_tls=False
        )
        self.scraper_input = None
        self.jobs_per_page = 100
        self.num_workers = 10
        self.seen_urls = set()
        self.company_counts: dict[str, int] = {}
        self.headers = None
        self.api_country_code = None
        self.base_url = None
        self.api_url = "https://apis.indeed.com/graphql"

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes Indeed for jobs with scraper_input criteria
        :param scraper_input:
        :return: job_response
        """
        self.scraper_input = scraper_input
        domain, self.api_country_code = self.scraper_input.country.indeed_domain_value
        self.base_url = f"https://{domain}.indeed.com"
        self.headers = api_headers.copy()
        self.headers["indeed-co"] = self.scraper_input.country.indeed_domain_value
        # Reset per-scrape state so successive scrape() calls on the same
        # instance don't carry over dedupe / company-cap counts.
        self.seen_urls = set()
        self.company_counts = {}
        job_list = []
        page = 1

        cursor = None

        # Loop on KEPT job count, not seen_urls. The per-company cap drops
        # jobs after they're seen, so seen_urls grows whether we keep them
        # or not — using it as the termination condition would exit early.
        target = scraper_input.results_wanted + scraper_input.offset
        while len(job_list) < target:
            log.info(
                f"search page: {page} / {math.ceil(scraper_input.results_wanted / self.jobs_per_page)}"
            )
            jobs, cursor = self._scrape_page(cursor)
            if not jobs:
                log.info(f"found no jobs on page: {page}")
                break
            job_list += jobs
            page += 1
            # Stop if Indeed has no more pages, regardless of quota.
            if cursor is None:
                break
        return JobResponse(
            jobs=job_list[
                scraper_input.offset : scraper_input.offset
                + scraper_input.results_wanted
            ]
        )

    def _scrape_page(self, cursor: str | None) -> Tuple[list[JobPost], str | None]:
        """
        Scrapes a page of Indeed for jobs with scraper_input criteria
        :param cursor:
        :return: jobs found on page, next page cursor
        """
        jobs = []
        new_cursor = None
        filters = self._build_filters()
        search_term = (
            self.scraper_input.search_term.replace('"', '\\"')
            if self.scraper_input.search_term
            else ""
        )
        # Build location clause. As of mid-2026 Indeed's GraphQL schema
        # promoted JobSearchLocationInput.radius from optional to Int! —
        # `None` rendered into the query produces "Int cannot represent
        # value: None"; omitting radius produces "required type Int! was
        # not provided". Default to 25 miles (Indeed's UI default) when
        # the caller didn't specify a distance.
        if self.scraper_input.location:
            radius = self.scraper_input.distance if self.scraper_input.distance is not None else 25
            location_clause = (
                f'location: {{where: "{self.scraper_input.location}", '
                f"radius: {radius}, radiusUnit: MILES}}"
            )
        else:
            location_clause = ""

        query = job_search_query.format(
            what=(f'what: "{search_term}"' if search_term else ""),
            location=location_clause,
            dateOnIndeed=self.scraper_input.hours_old,
            cursor=f'cursor: "{cursor}"' if cursor else "",
            filters=filters,
        )
        payload = {
            "query": query,
        }
        api_headers_temp = api_headers.copy()
        api_headers_temp["indeed-co"] = self.api_country_code
        response = self.session.post(
            self.api_url,
            headers=api_headers_temp,
            json=payload,
            timeout=10,
            verify=False,
        )
        if not response.ok:
            log.info(
                f"responded with status code: {response.status_code} (submit GitHub issue if this appears to be a bug)"
            )
            return jobs, new_cursor
        data = response.json()
        jobs = data["data"]["jobSearch"]["results"]
        new_cursor = data["data"]["jobSearch"]["pageInfo"]["nextCursor"]

        job_list = []
        for job in jobs:
            processed_job = self._process_job(job["job"])
            if processed_job:
                job_list.append(processed_job)

        return job_list, new_cursor

    def _build_filters(self):
        """
        Builds the filters dict for job type/is_remote. If hours_old is provided, composite filter for job_type/is_remote is not possible.
        IndeedApply: filters: { keyword: { field: "indeedApplyScope", keys: ["DESKTOP"] } }
        """
        filters_str = ""
        if self.scraper_input.hours_old:
            filters_str = """
            filters: {{
                date: {{
                  field: "dateOnIndeed",
                  start: "{start}h"
                }}
            }}
            """.format(
                start=self.scraper_input.hours_old
            )
        elif self.scraper_input.easy_apply:
            filters_str = """
            filters: {
                keyword: {
                  field: "indeedApplyScope",
                  keys: ["DESKTOP"]
                }
            }
            """
        elif self.scraper_input.job_type or self.scraper_input.is_remote:
            job_type_key_mapping = {
                JobType.FULL_TIME: "CF3CP",
                JobType.PART_TIME: "75GKK",
                JobType.CONTRACT: "NJXCK",
                JobType.INTERNSHIP: "VDTG7",
            }

            keys = []
            if self.scraper_input.job_type:
                key = job_type_key_mapping[self.scraper_input.job_type]
                keys.append(key)

            if self.scraper_input.is_remote:
                keys.append("DSQF7")

            if keys:
                keys_str = '", "'.join(keys)
                filters_str = f"""
                filters: {{
                  composite: {{
                    filters: [{{
                      keyword: {{
                        field: "attributes",
                        keys: ["{keys_str}"]
                      }}
                    }}]
                  }}
                }}
                """
        return filters_str

    def _process_job(self, job: dict) -> JobPost | None:
        """
        Parses the job dict into JobPost model
        :param job: dict to parse
        :return: JobPost if it's a new job
        """
        job_url = f'{self.base_url}/viewjob?jk={job["key"]}'
        if job_url in self.seen_urls:
            return

        # Per-company cap — observed pattern: same role posted to 5+
        # near-cities by one employer (e.g. ARMADA × 5 Seattle suburbs,
        # Amazon × 11). Drop excess from any single employer to make
        # room for diversity.
        company_name = job["employer"].get("name") if job.get("employer") else None
        if (
            self.max_results_per_company is not None
            and company_name
        ):
            company_key = company_name.lower()
            if self.company_counts.get(company_key, 0) >= self.max_results_per_company:
                self.seen_urls.add(job_url)
                return
            self.company_counts[company_key] = self.company_counts.get(company_key, 0) + 1

        self.seen_urls.add(job_url)
        description = job["description"]["html"]
        if self.scraper_input.description_format == DescriptionFormat.MARKDOWN:
            description = markdown_converter(description)

        job_type = get_job_type(job["attributes"])
        timestamp_seconds = job["datePublished"] / 1000
        date_posted = datetime.fromtimestamp(timestamp_seconds).strftime("%Y-%m-%d")
        employer = job["employer"].get("dossier") if job["employer"] else None
        employer_details = employer.get("employerDetails", {}) if employer else {}
        rel_url = job["employer"]["relativeCompanyPageUrl"] if job["employer"] else None
        return JobPost(
            id=f'in-{job["key"]}',
            title=job["title"],
            description=description,
            company_name=job["employer"].get("name") if job.get("employer") else None,
            company_url=(f"{self.base_url}{rel_url}" if job["employer"] else None),
            company_url_direct=(
                employer["links"]["corporateWebsite"] if employer else None
            ),
            location=Location(
                city=job.get("location", {}).get("city"),
                state=job.get("location", {}).get("admin1Code"),
                country=job.get("location", {}).get("countryCode"),
            ),
            job_type=job_type,
            compensation=get_compensation(job["compensation"]),
            date_posted=date_posted,
            job_url=job_url,
            job_url_direct=(
                job["recruit"].get("viewJobUrl") if job.get("recruit") else None
            ),
            emails=extract_emails_from_text(description) if description else None,
            is_remote=is_job_remote(job, description),
            company_addresses=(
                employer_details["addresses"][0]
                if employer_details.get("addresses")
                else None
            ),
            company_industry=(
                employer_details["industry"]
                .replace("Iv1", "")
                .replace("_", " ")
                .title()
                .strip()
                if employer_details.get("industry")
                else None
            ),
            company_num_employees=employer_details.get("employeesLocalizedLabel"),
            company_revenue=employer_details.get("revenueLocalizedLabel"),
            company_description=employer_details.get("briefDescription"),
            company_logo=(
                employer["images"].get("squareLogoUrl")
                if employer and employer.get("images")
                else None
            ),
        )
