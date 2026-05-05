import re

from bs4 import BeautifulSoup

from jobspy.model import Compensation, CompensationInterval, JobType, Location
from jobspy.util import get_enum_from_job_type


def job_type_code(job_type_enum: JobType) -> str:
    return {
        JobType.FULL_TIME: "F",
        JobType.PART_TIME: "P",
        JobType.INTERNSHIP: "I",
        JobType.CONTRACT: "C",
        JobType.TEMPORARY: "T",
    }.get(job_type_enum, "")


def parse_job_type(soup_job_type: BeautifulSoup) -> list[JobType] | None:
    """
    Gets the job type from job page
    :param soup_job_type:
    :return: JobType
    """
    h3_tag = soup_job_type.find(
        "h3",
        class_="description__job-criteria-subheader",
        string=lambda text: "Employment type" in text,
    )
    employment_type = None
    if h3_tag:
        employment_type_span = h3_tag.find_next_sibling(
            "span",
            class_="description__job-criteria-text description__job-criteria-text--criteria",
        )
        if employment_type_span:
            employment_type = employment_type_span.get_text(strip=True)
            employment_type = employment_type.lower()
            employment_type = employment_type.replace("-", "")

    return [get_enum_from_job_type(employment_type)] if employment_type else []


def parse_job_level(soup_job_level: BeautifulSoup) -> str | None:
    """
    Gets the job level from job page
    :param soup_job_level:
    :return: str
    """
    h3_tag = soup_job_level.find(
        "h3",
        class_="description__job-criteria-subheader",
        string=lambda text: "Seniority level" in text,
    )
    job_level = None
    if h3_tag:
        job_level_span = h3_tag.find_next_sibling(
            "span",
            class_="description__job-criteria-text description__job-criteria-text--criteria",
        )
        if job_level_span:
            job_level = job_level_span.get_text(strip=True)

    return job_level


def parse_company_industry(soup_industry: BeautifulSoup) -> str | None:
    """
    Gets the company industry from job page
    :param soup_industry:
    :return: str
    """
    h3_tag = soup_industry.find(
        "h3",
        class_="description__job-criteria-subheader",
        string=lambda text: "Industries" in text,
    )
    industry = None
    if h3_tag:
        industry_span = h3_tag.find_next_sibling(
            "span",
            class_="description__job-criteria-text description__job-criteria-text--criteria",
        )
        if industry_span:
            industry = industry_span.get_text(strip=True)

    return industry


def is_job_remote(title: dict, description: str, location: Location) -> bool:
    """
    Searches the title, location, and description to check if job is remote
    """
    remote_keywords = ["remote", "work from home", "wfh"]
    location = location.display_location()
    full_string = f'{title} {description} {location}'.lower()
    is_remote = any(keyword in full_string for keyword in remote_keywords)
    return is_remote


# Salary extraction from description bodies — LinkedIn's guest cards do NOT
# expose salary, but descriptions usually do (verified ~50-70% fill rate
# on Seattle network-engineer queries). Three patterns observed:
#
#   1. "$85,000 - $110,000"                    (range, no interval)
#   2. "$26.27 - $30.07 per hour"              (range with interval)
#   3. "Pay Range Minimum: $X annual Pay Range Maximum $Y annual"
#                                              (labeled min/max)
#
# False-positive guard: reject ranges where the annualized minimum
# is below $20K (catches incidentals like "$50/month wellness benefit").
_SAL_RANGE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([KkMm]?)"
    r"\s*(?:[-–—]+|to)\s*"
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*([KkMm]?)"
    r"\s*(?:per\s+|a\s+|an\s+|/)?"
    r"(year|yr|annual|annually|hour|hr|hourly|month|monthly|week|weekly|day|daily)?",
    re.I,
)
_SAL_LABELED_RE = re.compile(
    r"(?:Pay\s+Range\s+)?Minimum[:\s]+\$\s*([\d,]+(?:\.\d+)?)\s*([KkMm]?)\s*"
    r"(year|yr|annual|annually|hour|hr|hourly|month|monthly|week|weekly|day|daily)?"
    r".{0,80}?"
    r"(?:Pay\s+Range\s+)?Maximum[:\s]+\$\s*([\d,]+(?:\.\d+)?)\s*([KkMm]?)\s*"
    r"(year|yr|annual|annually|hour|hr|hourly|month|monthly|week|weekly|day|daily)?",
    re.I | re.S,
)
_SAL_SINGLE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([KkMm]?)\s*"
    r"(?:per|/|a|an)\s+"
    r"(year|yr|annual|annually|hour|hr|hourly|month|monthly|week|weekly|day|daily)",
    re.I,
)
_INTERVAL_LOOKUP = {
    "year": CompensationInterval.YEARLY,
    "yr": CompensationInterval.YEARLY,
    "annual": CompensationInterval.YEARLY,
    "annually": CompensationInterval.YEARLY,
    "hour": CompensationInterval.HOURLY,
    "hr": CompensationInterval.HOURLY,
    "hourly": CompensationInterval.HOURLY,
    "month": CompensationInterval.MONTHLY,
    "monthly": CompensationInterval.MONTHLY,
    "week": CompensationInterval.WEEKLY,
    "weekly": CompensationInterval.WEEKLY,
    "day": CompensationInterval.DAILY,
    "daily": CompensationInterval.DAILY,
}


def _amt(num: str, mult: str | None) -> float:
    val = float(num.replace(",", ""))
    if mult and mult.lower() == "k":
        val *= 1000.0
    elif mult and mult.lower() == "m":
        val *= 1_000_000.0
    return val


def _annualize(amount: float, interval: CompensationInterval | None) -> float:
    """Rough annualized value for the false-positive guard.
    Uses 2080 work hours, 52 weeks, 250 work days, 12 months."""
    if interval is None:
        return amount  # treat as already-yearly when unknown
    return {
        CompensationInterval.YEARLY: amount,
        CompensationInterval.MONTHLY: amount * 12,
        CompensationInterval.WEEKLY: amount * 52,
        CompensationInterval.DAILY: amount * 250,
        CompensationInterval.HOURLY: amount * 2080,
    }.get(interval, amount)


_MIN_REASONABLE_ANNUAL = 20_000.0


def extract_salary_from_description(description: str | None) -> Compensation | None:
    """Pull a salary range from a job description body.

    Tries three patterns in order: standard range ("$X - $Y"), labeled
    min/max ("Pay Range Minimum: $X ... Maximum: $Y"), single value
    with interval ("$X per year"). Rejects matches whose annualized min
    is implausibly low (< $20K) — those are almost always incidental
    "$50/month gym benefit" or "$25/hr stipend" mentions.
    """
    if not description:
        return None

    # Strategy 1: explicit range. Walk all matches; prefer first that
    # passes the false-positive guard.
    for m in _SAL_RANGE_RE.finditer(description):
        mn = _amt(m.group(1), m.group(2))
        mx = _amt(m.group(3), m.group(4))
        interval_word = (m.group(5) or "").lower()
        interval = _INTERVAL_LOOKUP.get(interval_word)
        if mx < mn:
            continue  # backwards: probably matched two unrelated $ values
        # When no interval keyword followed the range (e.g. "$85,000 -
        # $110,000"), infer yearly for amounts that are clearly salary-shaped
        # and hourly for small amounts (< $1K). Anything in between we leave
        # as None and let the caller decide.
        if interval is None:
            if mn >= 10_000:
                interval = CompensationInterval.YEARLY
            elif mn < 500 and mx < 500:
                interval = CompensationInterval.HOURLY
        if _annualize(mn, interval) < _MIN_REASONABLE_ANNUAL:
            continue
        return Compensation(
            interval=interval,
            min_amount=mn,
            max_amount=mx,
            currency="USD",
        )

    # Strategy 2: "Pay Range Minimum ... Maximum" labeled form.
    m = _SAL_LABELED_RE.search(description)
    if m:
        mn = _amt(m.group(1), m.group(2))
        mx = _amt(m.group(4), m.group(5))
        interval = _INTERVAL_LOOKUP.get((m.group(3) or m.group(6) or "").lower())
        if _annualize(mn, interval) >= _MIN_REASONABLE_ANNUAL and mx >= mn:
            return Compensation(
                interval=interval,
                min_amount=mn,
                max_amount=mx,
                currency="USD",
            )

    # Strategy 3: single value with explicit interval.
    m = _SAL_SINGLE_RE.search(description)
    if m:
        amt = _amt(m.group(1), m.group(2))
        interval = _INTERVAL_LOOKUP.get(m.group(3).lower())
        if _annualize(amt, interval) >= _MIN_REASONABLE_ANNUAL:
            return Compensation(
                interval=interval,
                min_amount=amt,
                max_amount=amt,
                currency="USD",
            )
    return None
