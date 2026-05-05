from __future__ import annotations

import math
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, urlunparse, unquote

import regex as re
from bs4 import BeautifulSoup
from bs4.element import Tag

from jobspy.exception import LinkedInException
from jobspy.linkedin.constant import headers
from jobspy.linkedin.util import (
    extract_salary_from_description,
    is_job_remote,
    job_type_code,
    parse_job_type,
    parse_job_level,
    parse_company_industry,
)
from jobspy.model import (
    JobPost,
    Location,
    JobResponse,
    Country,
    Compensation,
    DescriptionFormat,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.util import (
    extract_emails_from_text,
    currency_parser,
    markdown_converter,
    plain_converter,
    create_session,
    remove_attributes,
    create_logger,
)

log = create_logger("LinkedIn")


class LinkedIn(Scraper):
    base_url = "https://www.linkedin.com"
    delay = 3
    band_delay = 4
    jobs_per_page = 25

    def __init__(
        self, proxies: list[str] | str | None = None, ca_cert: str | None = None, user_agent: str | None = None
    ):
        """
        Initializes LinkedInScraper with the LinkedIn job search url
        """
        super().__init__(Site.LINKEDIN, proxies=proxies, ca_cert=ca_cert)
        self.session = create_session(
            proxies=self.proxies,
            ca_cert=ca_cert,
            is_tls=False,
            has_retry=True,
            delay=5,
            clear_cookies=True,
        )
        self.session.headers.update(headers)
        self.scraper_input = None
        self.country = "worldwide"
        self.job_url_direct_regex = re.compile(r'(?<=\?url=)[^"]+')

    # Concurrency cap for description-detail fetches. Higher fan-out triggers
    # LinkedIn 429s, which cascade into exponential backoff (delay=5,
    # 5s/10s/20s) and end up SLOWER than serial. 3 overlaps network I/O
    # without crossing the rate-limit threshold.
    description_fetch_workers = 3

    # Per-company result cap. LinkedIn's guest search returns the same
    # listing posted to N different cities (Reboot Monkey × 5+ TX suburbs
    # observed) — each gets a slightly different title (the trailing
    # city-code differs), so (title, company) dedupe doesn't catch them.
    # Capping at 3 per company gives the user a representative sample
    # without burying real diversity. Set to None to disable.
    max_results_per_company = 3

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes LinkedIn for jobs with scraper_input criteria
        :param scraper_input:
        :return: job_response
        """
        self.scraper_input = scraper_input
        job_list: list[JobPost] = []
        seen_ids: set[str] = set()
        # Dedupe key: (lower-cased title, lower-cased company). Catches exact
        # duplicate postings returned by LinkedIn's pagination overlap.
        seen_signatures: set[tuple[str, str]] = set()
        # Per-company tally — Reboot Monkey-style spam (same role to N cities,
        # title differs by trailing letter) is a different problem from exact
        # duplicates and needs a count-based cap.
        company_counts: dict[str, int] = {}
        start = scraper_input.offset // 10 * 10 if scraper_input.offset else 0
        request_count = 0
        seconds_old = (
            scraper_input.hours_old * 3600 if scraper_input.hours_old else None
        )
        continue_search = (
            lambda: len(job_list) < scraper_input.results_wanted and start < 1000
        )
        while continue_search():
            request_count += 1
            log.info(
                f"search page: {request_count} / {math.ceil(scraper_input.results_wanted / 10)}"
            )
            params = {
                "keywords": scraper_input.search_term,
                "location": scraper_input.location,
                "distance": scraper_input.distance,
                "f_WT": 2 if scraper_input.is_remote else None,
                "f_JT": (
                    job_type_code(scraper_input.job_type)
                    if scraper_input.job_type
                    else None
                ),
                "pageNum": 0,
                "start": start,
                "f_AL": "true" if scraper_input.easy_apply else None,
                "f_C": (
                    ",".join(map(str, scraper_input.linkedin_company_ids))
                    if scraper_input.linkedin_company_ids
                    else None
                ),
            }
            if seconds_old is not None:
                params["f_TPR"] = f"r{seconds_old}"

            params = {k: v for k, v in params.items() if v is not None}
            try:
                response = self.session.get(
                    f"{self.base_url}/jobs-guest/jobs/api/seeMoreJobPostings/search?",
                    params=params,
                    timeout=10,
                )
                if response.status_code not in range(200, 400):
                    if response.status_code == 429:
                        err = (
                            f"429 Response - Blocked by LinkedIn for too many requests"
                        )
                    else:
                        err = f"LinkedIn response status code {response.status_code}"
                        err += f" - {response.text}"
                    log.error(err)
                    return JobResponse(jobs=job_list)
            except Exception as e:
                if "Proxy responded with" in str(e):
                    log.error(f"LinkedIn: Bad proxy")
                else:
                    log.error(f"LinkedIn: {str(e)}")
                return JobResponse(jobs=job_list)

            soup = BeautifulSoup(response.text, "html.parser")
            job_cards = soup.find_all("div", class_="base-search-card")
            if len(job_cards) == 0:
                return JobResponse(jobs=job_list)

            # Phase 1: parse cards + dedupe BEFORE any detail fetch.
            shallow_pairs: list[tuple[Tag, str]] = []
            for job_card in job_cards:
                href_tag = job_card.find("a", class_="base-card__full-link")
                if not href_tag or "href" not in href_tag.attrs:
                    continue
                href = href_tag.attrs["href"].split("?")[0]
                job_id = href.split("-")[-1]
                if job_id in seen_ids:
                    continue

                # Cheap dedupe by (title, company) — kills the same-job-spammed-
                # to-many-cities pattern without paying for description fetches.
                title_tag = job_card.find("span", class_="sr-only")
                title_norm = (title_tag.get_text(strip=True) if title_tag else "").lower()
                company_tag = job_card.find("h4", class_="base-search-card__subtitle")
                company_a = company_tag.find("a") if company_tag else None
                company_norm = (
                    company_a.get_text(strip=True) if company_a else ""
                ).lower()
                signature = (title_norm, company_norm)
                if signature and signature in seen_signatures:
                    continue
                if title_norm or company_norm:
                    seen_signatures.add(signature)

                # Per-company cap — kills the company-spam pattern
                # (same role, different city, slightly different title).
                if (
                    self.max_results_per_company is not None
                    and company_norm
                    and company_counts.get(company_norm, 0)
                    >= self.max_results_per_company
                ):
                    continue
                if company_norm:
                    company_counts[company_norm] = (
                        company_counts.get(company_norm, 0) + 1
                    )

                seen_ids.add(job_id)
                shallow_pairs.append((job_card, job_id))

            # Phase 2: enrich (in parallel when description fetch is on).
            fetch_desc = scraper_input.linkedin_fetch_description
            page_posts: list[JobPost] = []
            if fetch_desc and shallow_pairs:
                with ThreadPoolExecutor(
                    max_workers=self.description_fetch_workers
                ) as executor:
                    futures = {
                        executor.submit(self._process_job, card, jid, True): jid
                        for card, jid in shallow_pairs
                    }
                    for fut in as_completed(futures):
                        try:
                            post = fut.result()
                            if post:
                                page_posts.append(post)
                        except Exception as e:
                            raise LinkedInException(str(e))
            else:
                for card, jid in shallow_pairs:
                    try:
                        post = self._process_job(card, jid, False)
                        if post:
                            page_posts.append(post)
                    except Exception as e:
                        raise LinkedInException(str(e))

            job_list.extend(page_posts)

            if continue_search():
                time.sleep(random.uniform(self.delay, self.delay + self.band_delay))
                start += len(job_cards)

        job_list = job_list[: scraper_input.results_wanted]
        return JobResponse(jobs=job_list)

    def _process_job(
        self, job_card: Tag, job_id: str, full_descr: bool
    ) -> Optional[JobPost]:
        salary_tag = job_card.find("span", class_="job-search-card__salary-info")

        compensation = description = None
        if salary_tag:
            salary_text = salary_tag.get_text(separator=" ").strip()
            salary_values = [currency_parser(value) for value in salary_text.split("-")]
            salary_min = salary_values[0]
            salary_max = salary_values[1]
            currency = salary_text[0] if salary_text[0] != "$" else "USD"

            compensation = Compensation(
                min_amount=int(salary_min),
                max_amount=int(salary_max),
                currency=currency,
            )

        title_tag = job_card.find("span", class_="sr-only")
        title = title_tag.get_text(strip=True) if title_tag else "N/A"

        company_tag = job_card.find("h4", class_="base-search-card__subtitle")
        company_a_tag = company_tag.find("a") if company_tag else None
        company_url = (
            urlunparse(urlparse(company_a_tag.get("href"))._replace(query=""))
            if company_a_tag and company_a_tag.has_attr("href")
            else ""
        )
        company = company_a_tag.get_text(strip=True) if company_a_tag else "N/A"

        metadata_card = job_card.find("div", class_="base-search-card__metadata")
        location = self._get_location(metadata_card)

        datetime_tag = (
            metadata_card.find("time", class_="job-search-card__listdate")
            if metadata_card
            else None
        )
        if not datetime_tag and metadata_card:
            datetime_tag = metadata_card.find(
                "time", class_="job-search-card__listdate--new"
            )
        date_posted = None
        if datetime_tag and "datetime" in datetime_tag.attrs:
            datetime_str = datetime_tag["datetime"]
            try:
                date_posted = datetime.strptime(datetime_str, "%Y-%m-%d")
            except:
                date_posted = None
        job_details = {}
        if full_descr:
            job_details = self._get_job_details(job_id)
            description = job_details.get("description")
            # LinkedIn's guest-cards have salary stripped, but description
            # bodies usually surface it (verified ~50-60% fill rate). Only
            # populate when the search-card didn't already provide one.
            if compensation is None and description:
                derived = extract_salary_from_description(description)
                if derived is not None:
                    compensation = derived
        is_remote = is_job_remote(title, description, location)

        return JobPost(
            id=f"li-{job_id}",
            title=title,
            company_name=company,
            company_url=company_url,
            location=location,
            is_remote=is_remote,
            date_posted=date_posted,
            job_url=f"{self.base_url}/jobs/view/{job_id}",
            compensation=compensation,
            job_type=job_details.get("job_type"),
            job_level=job_details.get("job_level", "").lower(),
            company_industry=job_details.get("company_industry"),
            description=job_details.get("description"),
            job_url_direct=job_details.get("job_url_direct"),
            emails=extract_emails_from_text(description),
            company_logo=job_details.get("company_logo"),
            job_function=job_details.get("job_function"),
        )

    def _get_job_details(self, job_id: str) -> dict:
        """
        Retrieves job description and other job details by going to the job page url
        :param job_page_url:
        :return: dict
        """
        try:
            response = self.session.get(
                f"{self.base_url}/jobs/view/{job_id}", timeout=5
            )
            response.raise_for_status()
        except:
            return {}
        if "linkedin.com/signup" in response.url:
            return {}

        soup = BeautifulSoup(response.text, "html.parser")
        div_content = soup.find(
            "div", class_=lambda x: x and "show-more-less-html__markup" in x
        )
        description = None
        if div_content is not None:
            div_content = remove_attributes(div_content)
            description = div_content.prettify(formatter="html")
            if self.scraper_input.description_format == DescriptionFormat.MARKDOWN:
                description = markdown_converter(description)
            elif self.scraper_input.description_format == DescriptionFormat.PLAIN:
                description = plain_converter(description)
        h3_tag = soup.find(
            "h3", text=lambda text: text and "Job function" in text.strip()
        )

        job_function = None
        if h3_tag:
            job_function_span = h3_tag.find_next(
                "span", class_="description__job-criteria-text"
            )
            if job_function_span:
                job_function = job_function_span.text.strip()

        company_logo = (
            logo_image.get("data-delayed-url")
            if (logo_image := soup.find("img", {"class": "artdeco-entity-image"}))
            else None
        )
        return {
            "description": description,
            "job_level": parse_job_level(soup),
            "company_industry": parse_company_industry(soup),
            "job_type": parse_job_type(soup),
            "job_url_direct": self._parse_job_url_direct(soup),
            "company_logo": company_logo,
            "job_function": job_function,
        }

    def _get_location(self, metadata_card: Optional[Tag]) -> Location:
        """
        Extracts the location data from the job metadata card.
        :param metadata_card
        :return: location
        """
        location = Location(country=Country.from_string(self.country))
        if metadata_card is not None:
            location_tag = metadata_card.find(
                "span", class_="job-search-card__location"
            )
            location_string = location_tag.text.strip() if location_tag else "N/A"
            parts = location_string.split(", ")
            if len(parts) == 2:
                city, state = parts
                location = Location(
                    city=city,
                    state=state,
                    country=Country.from_string(self.country),
                )
            elif len(parts) == 3:
                city, state, country = parts
                country = Country.from_string(country)
                location = Location(city=city, state=state, country=country)
        return location

    def _parse_job_url_direct(self, soup: BeautifulSoup) -> str | None:
        """
        Gets the job url direct from job page
        :param soup:
        :return: str
        """
        job_url_direct = None
        job_url_direct_content = soup.find("code", id="applyUrl")
        if job_url_direct_content:
            job_url_direct_match = self.job_url_direct_regex.search(
                job_url_direct_content.decode_contents().strip()
            )
            if job_url_direct_match:
                job_url_direct = unquote(job_url_direct_match.group())

        return job_url_direct
