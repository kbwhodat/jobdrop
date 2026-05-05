# Phase 0 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bootstrap the fork with a test/CI/lint/docs foundation so every subsequent scraper plan can ship on rails — TDD with recorded HTTP, ruff-clean, CI green on push.

**Architecture:** Keep upstream's Poetry + per-scraper-directory layout intact. Add `tests/` mirroring `jobspy/`'s structure. Use `pytest-recording` (VCR cassettes) so tests are deterministic and don't hit live sites in CI. Add `ruff` for lint+format (replaces upstream's `black`-only setup with a faster modern toolchain). GitHub Actions runs lint + tests on every push.

**Tech Stack:** Python 3.10+, Poetry, pytest, pytest-recording, vcrpy, ruff, python-dotenv, GitHub Actions.

---

## File Structure

**New files:**
- `tests/__init__.py` — marks `tests/` as a package
- `tests/conftest.py` — pytest + VCR shared fixtures
- `tests/test_imports.py` — smoke test: package imports cleanly
- `tests/scrapers/__init__.py` — marks subpackage
- `tests/scrapers/test_indeed_recorded.py` — first VCR-recorded test (proves the harness works on a known-good scraper)
- `tests/scrapers/cassettes/` — directory for VCR cassettes (auto-populated)
- `.env.example` — API key slots, each with link + scope notes
- `.github/workflows/test.yml` — CI: ruff + pytest on push/PR
- `CONTRIBUTING.md` — dev setup, scraper authoring, upstream sync, commit conventions
- `docs/AUTHORING_SCRAPERS.md` — deep dive on the `Scraper` base class, `ScraperInput`, `JobResponse`, cassette naming

**Modified files:**
- `pyproject.toml` — add dev dependencies, ruff config, pytest config
- `.gitignore` — add `.env`, `.venv`, `.pytest_cache/`, `.ruff_cache/`
- `README.md` — add "Development" section linking to `CONTRIBUTING.md` and roadmap

---

## Task 1: Pin dev dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dev dependencies block to `pyproject.toml`**

Replace the existing `[tool.poetry.group.dev.dependencies]` block (currently jupyter/black/pre-commit) with:

```toml
[tool.poetry.group.dev.dependencies]
pytest = "^8.3.0"
pytest-recording = "^0.13.2"
vcrpy = "^6.0.2"
ruff = "^0.7.0"
python-dotenv = "^1.0.1"
pre-commit = "*"
```

(jupyter and black are dropped — jupyter is unused; black is replaced by ruff format.)

- [ ] **Step 2: Regenerate the lock file**

Run: `cd /Users/katob/Documents/projects/JobSpy && poetry lock --no-update`
Expected: `Resolving dependencies... Writing lock file` with no errors.

- [ ] **Step 3: Install dev dependencies**

Run: `poetry install --with dev`
Expected: `Installing the current project: python-jobspy (1.1.82)` near the end, no resolution errors.

- [ ] **Step 4: Verify the new tools are on PATH**

Run: `poetry run pytest --version && poetry run ruff --version`
Expected: prints pytest 8.x and ruff 0.7.x version lines.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml poetry.lock
git commit -m "chore(deps): add pytest, pytest-recording, ruff, python-dotenv

Replaces black with ruff (lint+format) and jupyter (unused) with pytest
toolchain for the test harness. Phase 0 foundation."
```

---

## Task 2: Pytest config + smoke test

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`
- Create: `tests/test_imports.py`

- [ ] **Step 1: Add pytest config to `pyproject.toml`**

Append to the end of `pyproject.toml`:

```toml
[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["tests"]
addopts = "-ra --strict-markers --strict-config"
markers = [
    "live: tests that hit live sites (skipped by default in CI)",
    "vcr: tests using recorded HTTP cassettes",
]
```

- [ ] **Step 2: Create empty `tests/__init__.py`**

Write to `tests/__init__.py`:

```python
```

(Empty file — just marks the directory as a Python package.)

- [ ] **Step 3: Write smoke test**

Write to `tests/test_imports.py`:

```python
"""Smoke tests verifying the package imports cleanly.

If any of these fail, the package is broken at the import level and
no other test will run reliably. These are the canary tests.
"""


def test_jobspy_imports():
    import jobspy
    assert hasattr(jobspy, "scrape_jobs")


def test_all_site_scrapers_import():
    from jobspy.indeed import Indeed
    from jobspy.linkedin import LinkedIn
    from jobspy.glassdoor import Glassdoor
    from jobspy.google import Google
    from jobspy.ziprecruiter import ZipRecruiter
    from jobspy.bayt import BaytScraper
    from jobspy.bdjobs import BDJobs
    from jobspy.naukri import Naukri

    for scraper in (Indeed, LinkedIn, Glassdoor, Google, ZipRecruiter, BaytScraper, BDJobs, Naukri):
        assert callable(scraper)


def test_model_exports():
    from jobspy.model import Site, JobPost, JobResponse, ScraperInput, Location

    assert Site.INDEED.value == "indeed"
    assert hasattr(JobPost, "model_fields")
```

- [ ] **Step 4: Verify the imports the test references actually exist**

Run: `poetry run python -c "from jobspy.bayt import BaytScraper; from jobspy.bdjobs import BDJobs; from jobspy.naukri import Naukri; print('ok')"`
Expected: `ok`. If any class name is wrong, fix the test (don't fix the import) — the upstream class names are authoritative.

- [ ] **Step 5: Run the smoke tests**

Run: `poetry run pytest tests/test_imports.py -v`
Expected: 3 tests, all pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tests/__init__.py tests/test_imports.py
git commit -m "test: add pytest config and import smoke tests

Establishes tests/ as the canonical test root. Smoke tests catch
import-level breakage (e.g., missing __init__.py, broken module
top-level imports) before any scraper-specific test runs."
```

---

## Task 3: VCR fixture + first recorded scraper test

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/scrapers/__init__.py`
- Create: `tests/scrapers/test_indeed_recorded.py`

- [ ] **Step 1: Write `tests/conftest.py`**

Write to `tests/conftest.py`:

```python
"""Shared pytest fixtures.

VCR cassettes live under `tests/scrapers/cassettes/<test_module>/<test_name>.yaml`
and are matched on (method, scheme, host, path, query). On first run with no
cassette, pytest-recording will hit the live site and write the cassette.
On subsequent runs, the cassette is replayed — no network traffic.

Re-record cassettes when a scraper is updated:
    poetry run pytest tests/scrapers/test_indeed_recorded.py --record-mode=rewrite
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def vcr_config():
    return {
        "match_on": ["method", "scheme", "host", "path", "query"],
        "filter_headers": [
            "authorization",
            "cookie",
            "x-api-key",
            "user-agent",
        ],
        "filter_query_parameters": [
            "api_key",
            "app_id",
            "app_key",
        ],
        "decode_compressed_response": True,
    }
```

- [ ] **Step 2: Create empty `tests/scrapers/__init__.py`**

Write to `tests/scrapers/__init__.py`:

```python
```

- [ ] **Step 3: Write the first recorded scraper test**

Write to `tests/scrapers/test_indeed_recorded.py`:

```python
"""Recorded-HTTP smoke test for the Indeed scraper.

Indeed is the most reliable scraper in jobspy and the de-facto
reference implementation. If this passes against a recorded
cassette, the test harness itself is healthy. New scrapers should
follow the same pattern: import the scraper class, build a
`ScraperInput`, call `.scrape()`, assert on the `JobResponse`.
"""
from __future__ import annotations

import pytest

from jobspy.indeed import Indeed
from jobspy.model import ScraperInput, Site


@pytest.mark.vcr
def test_indeed_search_returns_jobs():
    scraper = Indeed()
    response = scraper.scrape(
        ScraperInput(
            site_type=[Site.INDEED],
            search_term="software engineer",
            location="Atlanta, GA",
            results_wanted=5,
            country="usa",
        )
    )

    assert response.jobs is not None
    assert len(response.jobs) >= 1
    first = response.jobs[0]
    assert first.title
    assert first.company_name
    assert first.job_url.startswith("https://")
```

- [ ] **Step 4: Record the cassette by running the test live**

Run: `poetry run pytest tests/scrapers/test_indeed_recorded.py --record-mode=once -v`
Expected: 1 test passes. A new file `tests/scrapers/cassettes/test_indeed_recorded/test_indeed_search_returns_jobs.yaml` is created (~50–500KB).

If Indeed rate-limits or returns zero results, retry once; if it persistently fails, narrow `search_term` to `python` (more results).

- [ ] **Step 5: Re-run with cassette to confirm replay works**

Run: `poetry run pytest tests/scrapers/test_indeed_recorded.py -v`
Expected: 1 test passes, **without network traffic**. (Verify by running with `--disable-recording` if curious — should still pass replaying the cassette.)

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/scrapers/__init__.py tests/scrapers/test_indeed_recorded.py tests/scrapers/cassettes/
git commit -m "test(indeed): add first VCR-recorded scraper test

Establishes the pytest-recording pattern for all future scraper
tests. Cassettes live under tests/scrapers/cassettes/ and are
replayed in CI — no live HTTP. Re-record with --record-mode=rewrite."
```

---

## Task 4: Ruff lint + format config

**Files:**
- Modify: `pyproject.toml`
- Modify: `.gitignore`

- [ ] **Step 1: Add ruff config to `pyproject.toml`**

Append to the end of `pyproject.toml`:

```toml
[tool.ruff]
line-length = 88
target-version = "py310"
extend-exclude = ["poetry.lock"]

[tool.ruff.lint]
select = [
    "E",    # pycodestyle errors
    "F",    # pyflakes
    "W",    # pycodestyle warnings
    "I",    # isort
    "B",    # flake8-bugbear
    "UP",   # pyupgrade
]
ignore = [
    "E501",  # line too long — match upstream's looser style
    "B008",  # function call in default arg — pydantic uses this pattern
]

[tool.ruff.format]
quote-style = "double"
```

(Drop the existing `[tool.black]` block — ruff format replaces it.)

- [ ] **Step 2: Update `.gitignore`**

Append to `.gitignore`:

```
.env
.venv/
.pytest_cache/
.ruff_cache/
tests/scrapers/cassettes/.cache/
```

- [ ] **Step 3: Run ruff on the codebase to find baseline issues**

Run: `poetry run ruff check . --statistics`
Expected: prints a count summary. Don't be alarmed if there are 50–200 issues — the upstream codebase predates strict linting.

- [ ] **Step 4: Auto-fix the safe ones**

Run: `poetry run ruff check . --fix --unsafe-fixes=false`
Expected: prints `Found X errors (N fixed, M remaining)`. Look at the diff — if any "fix" looks behavior-changing, revert that file with `git checkout -- <file>` and add the rule to `ignore` in `pyproject.toml`.

- [ ] **Step 5: Verify tests still pass after auto-fix**

Run: `poetry run pytest -v`
Expected: same number of tests as before (4 tests), all pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore $(git diff --name-only | grep '\.py$')
git commit -m "chore: add ruff lint/format config and auto-fix safe issues

Replaces upstream black-only setup. Ignores E501 to match upstream
line-length tolerance. Auto-fixes from ruff's safe ruleset only —
no behavior changes."
```

---

## Task 5: Document API keys in `.env.example`

**Files:**
- Create: `.env.example`

- [ ] **Step 1: Write `.env.example`**

Write to `.env.example`:

```bash
# JobSpy API Keys — copy this file to `.env` and fill in your keys.
# `.env` is gitignored; never commit real keys.
#
# Phase 2 scrapers that need keys. Each link is the registration page —
# all are free as of 2026-05-05.

# Adzuna — large aggregator, free dev key (250 calls/month base tier).
# https://developer.adzuna.com/
ADZUNA_APP_ID=
ADZUNA_APP_KEY=

# USAJobs.gov — federal jobs, official free API, generous rate limits.
# Email is sent as the User-Agent identifier per their ToS.
# https://developer.usajobs.gov/apirequest/
USAJOBS_USER_AGENT_EMAIL=
USAJOBS_API_KEY=

# Jooble — large aggregator, free key.
# https://jooble.org/api/about
JOOBLE_API_KEY=

# Findwork — free key.
# https://findwork.dev/developers/
FINDWORK_API_KEY=

# The Muse — keyless for read endpoints, but a key raises rate limits.
# https://www.themuse.com/developers/api/v2
THE_MUSE_API_KEY=

# RemoteOK — keyless public JSON feed. No registration required.
# https://remoteok.com/api

# Remotive — keyless public JSON feed. No registration required.
# https://remotive.com/api/remote-jobs
```

- [ ] **Step 2: Verify `.env` is in `.gitignore` (added in Task 4)**

Run: `grep -E '^\.env$' .gitignore`
Expected: prints `.env`. If missing, add it before committing.

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs: add .env.example with all anticipated API key slots

Each slot links to the registration page. All keys are free as of
2026-05-05. .env itself is gitignored — never commit real keys."
```

---

## Task 6: GitHub Actions CI

**Files:**
- Create: `.github/workflows/test.yml`

- [ ] **Step 1: Write the CI workflow**

Write to `.github/workflows/test.yml`:

```yaml
name: Test

on:
  push:
  workflow_dispatch:

jobs:
  lint:
    name: Ruff lint + format check
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
      - name: Install ruff
        run: pip install ruff==0.7.0
      - name: Lint
        run: ruff check .
      - name: Format check
        run: ruff format --check .

  test:
    name: Pytest (Python ${{ matrix.python-version }})
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install Poetry
        run: pipx install poetry
      - name: Cache Poetry venv
        uses: actions/cache@v4
        with:
          path: ~/.cache/pypoetry
          key: poetry-${{ runner.os }}-py${{ matrix.python-version }}-${{ hashFiles('poetry.lock') }}
      - name: Install dependencies
        run: poetry install --with dev
      - name: Run tests with cassettes (no live HTTP)
        run: poetry run pytest -v --record-mode=none
```

(`--record-mode=none` makes CI fail loudly if a test tries to hit a live site without a cassette — this is what we want.)

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/test.yml
git commit -m "ci: add ruff lint + pytest matrix on push/PR

Three Python versions (3.10/3.11/3.12). Tests run with
--record-mode=none — any unrecorded HTTP fails CI loudly,
ensuring scraper tests stay deterministic."
```

- [ ] **Step 3: Push the branch and watch CI**

Run: `git push -u origin setup/foundation-plan`
Then: `gh run watch`
Expected: workflow appears, lint passes, all 3 test jobs pass. If a job fails, fix and amend (or push a follow-up commit) until green.

---

## Task 7: CONTRIBUTING.md — fork-specific dev workflow

**Files:**
- Create: `CONTRIBUTING.md`

- [ ] **Step 1: Write `CONTRIBUTING.md`**

Write to `CONTRIBUTING.md`:

```markdown
# Contributing to this JobSpy fork

Soft fork of [Bunsly/JobSpy](https://github.com/Bunsly/JobSpy). See
[`docs/superpowers/plans/2026-05-05-roadmap.md`](docs/superpowers/plans/2026-05-05-roadmap.md)
for the fork's direction and per-phase plans.

## Dev setup

\`\`\`bash
git clone https://github.com/kbwhodat/JobSpy.git
cd JobSpy
poetry install --with dev
cp .env.example .env  # then fill in keys you have
poetry run pytest -v  # smoke test
\`\`\`

## Adding a new scraper

See [`docs/AUTHORING_SCRAPERS.md`](docs/AUTHORING_SCRAPERS.md) for the deep dive.
TL;DR per scraper:

1. Create `jobspy/<source>/` with `__init__.py`, `constant.py`, `util.py` (mirror `jobspy/indeed/`)
2. Add `<SOURCE>` to the `Site` enum in `jobspy/model.py`
3. Wire the scraper into `jobspy/__init__.py`'s site dispatcher
4. Write a VCR-recorded test in `tests/scrapers/test_<source>_recorded.py`
5. Run `poetry run pytest tests/scrapers/test_<source>_recorded.py --record-mode=once -v` to record the cassette
6. Commit cassette + scraper + test together

## Test discipline

- **Every scraper has at least one VCR-recorded test.** No exceptions.
- Tests in CI run with `--record-mode=none` — any unrecorded HTTP fails CI.
- Re-record cassettes when a scraper is intentionally updated:
  `poetry run pytest tests/scrapers/test_<source>_recorded.py --record-mode=rewrite`

## Linting

`ruff check .` and `ruff format .` must pass. Run before every commit.

## Commit conventions

[Conventional Commits](https://www.conventionalcommits.org/):
- `feat(<source>):` new scraper or new capability
- `fix(<source>):` bug fix
- `chore:` deps, tooling, non-source changes
- `test:` test-only changes
- `docs:` doc-only changes
- `ci:` workflow changes

## Syncing with upstream

\`\`\`bash
git fetch upstream
git checkout main
git merge upstream/main  # resolve any conflicts
git push origin main
\`\`\`

Avoid editing upstream-touched files (`jobspy/indeed/`, `jobspy/linkedin/`, `jobspy/model.py`)
without a clear reason — keeps merges easy. New scrapers go in their own directories.
```

(The implementer should write the file with literal triple-backticks where shown as `\`\`\`` above — escaping is only for embedding the file contents inside this plan.)

- [ ] **Step 2: Commit**

```bash
git add CONTRIBUTING.md
git commit -m "docs: add CONTRIBUTING.md with fork-specific dev workflow

Documents poetry+pytest setup, scraper-authoring TL;DR, VCR
discipline, ruff requirements, conventional-commit format, and
upstream-sync procedure."
```

---

## Task 8: Scraper authoring deep-dive doc

**Files:**
- Create: `docs/AUTHORING_SCRAPERS.md`

- [ ] **Step 1: Write `docs/AUTHORING_SCRAPERS.md`**

Write to `docs/AUTHORING_SCRAPERS.md`:

```markdown
# Authoring a New JobSpy Scraper

Reference: read `jobspy/indeed/__init__.py` end-to-end — it's the cleanest
existing scraper and the de-facto template.

## Interfaces (jobspy/model.py)

### `Scraper` (base class)

\`\`\`python
class Scraper(ABC):
    site: Site
    proxies: list[str] | None

    def __init__(self, site, proxies=None, ca_cert=None): ...

    @abstractmethod
    def scrape(self, scraper_input: ScraperInput) -> JobResponse: ...
\`\`\`

Subclass it, set `site = Site.YOURSOURCE`, implement `scrape()`.

### `ScraperInput`

The search request. Key fields:
- `site_type: list[Site]` — which sites to query (filtered by dispatcher)
- `search_term: str` — keyword query
- `location: str | None` — location string ("Atlanta, GA")
- `results_wanted: int` — soft target
- `hours_old: int | None` — freshness filter
- `country: str | None` — for region-aware sources
- `is_remote: bool` — remote-only filter
- `job_type: JobType | None` — fulltime/parttime/contract/internship
- `easy_apply: bool` — quick-apply filter
- `offset: int` — pagination cursor

### `JobResponse`

The search response: `JobResponse(jobs: list[JobPost])`.

### `JobPost` (key fields, all optional unless noted)

- `id: str` (required) — globally-unique within source, prefix with source code: `"di-12345"` for Dice, `"go-67890"` for Google
- `title: str` (required)
- `company_name: str | None`
- `job_url: str` (required, must be `https://`)
- `location: Location | None`
- `description: str | None` — markdown-converted body
- `date_posted: date | None`
- `is_remote: bool | None`
- `job_type: list[JobType] | None`
- `compensation: Compensation | None` — has `min_amount`, `max_amount`, `currency`, `interval`

## Per-scraper file structure

\`\`\`
jobspy/<source>/
├── __init__.py    # the Scraper subclass
├── constant.py    # headers, base URLs, params, fixed lookup tables
└── util.py        # parsing helpers, response normalization
\`\`\`

## API-based scraper template

\`\`\`python
# jobspy/<source>/__init__.py
import requests
from jobspy.model import Scraper, Site, ScraperInput, JobResponse, JobPost, Location
from jobspy.<source>.constant import API_BASE, headers
from jobspy.<source>.util import parse_job, log


class YourSource(Scraper):
    def __init__(self, proxies=None, ca_cert=None, user_agent=None):
        super().__init__(Site.YOURSOURCE, proxies=proxies, ca_cert=ca_cert)
        self.session = requests.Session()
        self.session.headers.update(headers)

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        params = self._build_params(scraper_input)
        response = self.session.get(API_BASE, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        jobs = [parse_job(j) for j in data.get("results", [])]
        return JobResponse(jobs=[j for j in jobs if j is not None])

    def _build_params(self, si: ScraperInput) -> dict:
        return {
            "q": si.search_term,
            "location": si.location or "",
            "limit": si.results_wanted,
        }
\`\`\`

## HTML scraper template

Follow `jobspy/indeed/__init__.py` more directly. Use `tls_client.Session` (already a dep)
when the site fingerprints requests; otherwise plain `requests.Session`.

## VCR cassette naming

\`\`\`
tests/scrapers/cassettes/
└── test_<source>_recorded/
    ├── test_<source>_search_returns_jobs.yaml
    └── test_<source>_filters_by_location.yaml
\`\`\`

One YAML per test function. Cassettes are plaintext-ish — review before committing,
**redact any credentials** that pytest-recording's `filter_headers` missed.

## Wiring into the dispatcher

In `jobspy/__init__.py`, find the site→Scraper map (search for `Site.INDEED:`) and add
your class. Mirror the existing pattern.

## Site enum

In `jobspy/model.py`:

\`\`\`python
class Site(Enum):
    LINKEDIN = "linkedin"
    INDEED = "indeed"
    # ...
    YOURSOURCE = "yoursource"  # add at the end
\`\`\`

## API key handling

Read keys from `os.environ` inside the scraper class, not at import time. Allow
`None` and either raise a clear error or skip silently with a `log.warning`. Never
hardcode keys, never commit `.env`, never log keys (they leak into pytest stdout).
```

(Same triple-backtick escaping convention as Task 7.)

- [ ] **Step 2: Commit**

```bash
git add docs/AUTHORING_SCRAPERS.md
git commit -m "docs: add AUTHORING_SCRAPERS.md scraper-authoring guide

Documents the Scraper/ScraperInput/JobResponse contract, per-scraper
file layout, API-vs-HTML templates, cassette naming, and dispatcher
wiring. Reference for every Phase 2 scraper plan."
```

---

## Task 9: Update README to point at fork docs

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Locate where to insert the Development section**

Run: `grep -n '^##' README.md`
Expected: prints all top-level headings. Pick the position immediately after the introductory section and before any "Installation" section. Typically line 30–60.

- [ ] **Step 2: Insert the Development section**

Edit `README.md` to add this block immediately after the first `## Installation` or `## Usage` heading (whichever comes first), as a new sibling section:

```markdown
## Development (this fork)

This is a soft fork of [Bunsly/JobSpy](https://github.com/Bunsly/JobSpy) maintained
by [@kbwhodat](https://github.com/kbwhodat). See:

- **[Roadmap](docs/superpowers/plans/2026-05-05-roadmap.md)** — fork direction and per-phase plans
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — dev setup and conventions
- **[Authoring guide](docs/AUTHORING_SCRAPERS.md)** — how to add a new scraper

```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): link fork roadmap, CONTRIBUTING, authoring guide"
```

---

## Task 10: Final push + CI green-check + merge to main

This is a solo fork — no PR ceremony. We push the branch, verify CI green, then fast-forward `main`.

- [ ] **Step 1: Push all commits**

Run: `git push origin setup/foundation-plan`
Expected: all 9 commits pushed.

- [ ] **Step 2: Watch CI**

Run: `gh run watch`
Expected: lint job ✓, test jobs (3.10, 3.11, 3.12) all ✓. If anything red, fix and re-push until green before merging.

- [ ] **Step 3: Mark Phase 0 done in the roadmap**

Edit `docs/superpowers/plans/2026-05-05-roadmap.md`: in the "Phases" section, change `### Phase 0 — Foundation` to `### Phase 0 — Foundation ✅` and add a "Completed: 2026-05-XX" line below the plan link.

- [ ] **Step 4: Commit and push the roadmap update**

```bash
git add docs/superpowers/plans/2026-05-05-roadmap.md
git commit -m "docs(roadmap): mark Phase 0 complete"
git push origin setup/foundation-plan
```

- [ ] **Step 5: Fast-forward `main` to the work branch**

```bash
git checkout main
git merge --ff-only setup/foundation-plan
git push origin main
```

- [ ] **Step 6: Delete the work branch (local + remote)**

```bash
git branch -d setup/foundation-plan
git push origin --delete setup/foundation-plan
```

---

## Self-review checklist (run after Task 10)

- [ ] **Spec coverage:** Every item in the "What this builds" header is implemented in some task above.
- [ ] **No placeholders:** No "TODO", "TBD", "appropriate error handling" left anywhere in the plan.
- [ ] **Type consistency:** `Scraper`, `ScraperInput`, `JobResponse`, `Site`, `JobPost` names match across all task descriptions.
- [ ] **CI green:** All three Python versions pass on the pushed branch.
- [ ] **No secrets in cassettes:** Spot-check `tests/scrapers/cassettes/test_indeed_recorded/test_indeed_search_returns_jobs.yaml` — no API keys, cookies, or auth headers leaked.
- [ ] **Roadmap current:** Phase 0 marked complete; next plan (1.1 Glassdoor fix) noted as the next thing to plan.

When all boxes ticked: Phase 0 is done. Next plan to write: `2026-05-05-phase-1-1-glassdoor-fix.md`.
