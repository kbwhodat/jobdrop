"""jobdrop TUI — search 33 job boards from your terminal."""

from __future__ import annotations

import sys
import re
import os
import logging
import tempfile
import shutil
from pathlib import Path
from typing import Optional

# ── Suppress jobdrop's noisy logging ───────────────────────────────
# Must happen before jobdrop import
logging.getLogger("Jobdrop").setLevel(logging.WARNING)
logging.getLogger("jobdrop").setLevel(logging.WARNING)
# Also silence noisy third-party loggers
for name in ["selenium", "urllib3", "httpx", "websockets", "asyncio", "trio"]:
    logging.getLogger(name).setLevel(logging.WARNING)

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static, Switch
from textual.widget import Widget
from textual.css.query import NoMatches
from textual.message import Message

# Ensure jobdrop's venv is on path
_jobdrop_venv = Path.home() / ".local" / "share" / "jobdrop-venv" / "lib"
_venv_site = next(_jobdrop_venv.glob("python3*/site-packages"), None)
if _venv_site and str(_venv_site) not in sys.path:
    sys.path.insert(0, str(_venv_site))

from jobdrop import scrape_jobs


# ── Source categories (torlink-style: short labels, no counts) ────

CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("All", "All sources", []),  # special — populated at init
    ("Major Boards", "LinkedIn, Indeed, Glassdoor, Google, ZipRecruiter, CareerBuilder", [
        "linkedin", "indeed", "glassdoor", "google", "zip_recruiter", "careerbuilder",
    ]),
    ("Tech / Startup", "Wellfound, HiringCafe, TrueUp, Dice, BuiltIn, Findwork", [
        "wellfound", "hiring_cafe", "trueup", "dice", "builtin", "findwork",
    ]),
    ("ATS / Direct", "Greenhouse, Lever, Ashby, Workday, iCIMS", [
        "greenhouse", "lever", "ashby", "workday", "icims",
    ]),
    ("Remote", "RemoteOK, WeWorkRemotely, CollabWork", [
        "remoteok", "weworkremotely", "collab_work",
    ]),
    ("Government", "USAJobs, GovernmentJobs, ClearanceJobs", [
        "usajobs", "governmentjobs", "clearance_jobs",
    ]),
    ("Staffing", "InsightGlobal, Kforce, Snagajob", [
        "insight_global", "kforce", "snagajob",
    ]),
    ("International", "Naukri, Bayt, Adzuna, Jooble", [
        "naukri", "bayt", "adzuna", "jooble",
    ]),
    ("Mission-Driven", "Idealist, The Muse, Handshake", [
        "idealist", "the_muse", "handshake",
    ]),
]

# Build "All" sources list
ALL_SOURCES: list[str] = []
for _, _, sources in CATEGORIES:
    if sources:
        ALL_SOURCES.extend(sources)
CATEGORIES[0] = ("All", "Every source", ALL_SOURCES)


# ── Theme ──────────────────────────────────────────────────────────

# White theme, clean and high-contrast
ACCENT = "#4f46e5"       # Indigo
ACCENT_BRIGHT = "#6366f1"
TEXT = "#1e1b2e"
TEXT_MUTED = "#6b6577"
BG = "#faf9fc"
BG_ALT = "#f0edf7"
BG_HOVER = "#e8e3f5"
BORDER = "#d4cfdf"
GOOD = "#059669"
WARN = "#d97706"
BAD = "#dc2626"
HINT = "#9c97ad"

SOURCE_COLORS: dict[str, str] = {
    "linkedin": "#0a66c2", "indeed": "#2164f3", "glassdoor": "#0caa41",
    "google": "#4285f4", "greenhouse": "#23a55e", "lever": "#ec4e3d",
    "wellfound": "#1a1a1a", "remoteok": "#f59e0b", "weworkremotely": "#3b82f6",
    "zip_recruiter": "#7c3aed", "usajobs": "#1a4480", "dice": "#e53e3e",
    "careerbuilder": "#ea580c", "naukri": "#2563eb", "bayt": "#0891b2",
    "adzuna": "#059669", "jooble": "#d97706", "the_muse": "#db2777",
    "idealist": "#7c3aed", "snagajob": "#dc2626", "handshake": "#4f46e5",
    "clearance_jobs": "#6366f1", "kforce": "#0d9488", "insight_global": "#9333ea",
    "collab_work": "#f97316", "hiring_cafe": "#14b8a6", "trueup": "#8b5cf6",
    "ashby": "#22c55e", "workday": "#f59e0b", "icims": "#06b6d4",
    "governmentjobs": "#1e40af", "builtin": "#ef4444", "findwork": "#10b981",
}

def source_tag(site: str) -> tuple[str, str]:
    """Return (short_tag, color) for a source."""
    color = SOURCE_COLORS.get(site.lower(), ACCENT)
    tag = site.upper().replace("_", "")[:4] if site else "?"
    return tag, color


# ── CSS ────────────────────────────────────────────────────────────

CSS = """
Screen {
    background: #faf9fc;
    color: #1e1b2e;
}

Header {
    background: #ffffff;
    color: #1e1b2e;
    border-bottom: solid #d4cfdf;
}

#sidebar {
    width: 22;
    background: #f0edf7;
    border-right: solid #d4cfdf;
    padding: 1 2;
}

.sidebar-title {
    color: #9c97ad;
    text-style: bold;
    margin: 1 0 0 0;
    padding: 0 0 0 0;
}

.sidebar-spacer {
    height: 1;
}

#sidebar .category {
    color: #6b6577;
    padding: 0 0 0 3;
    height: 1;
}

#sidebar .category.-selected {
    color: #4f46e5;
    text-style: bold;
    background: #e8e3f5;
}

#sidebar .category:hover {
    background: #e8e3f5;
    color: #1e1b2e;
}

#main {
    height: 100%;
}

#logo {
    color: #4f46e5;
    text-style: bold;
    content-align: center middle;
    padding: 1 0 0 0;
}

#search-container {
    padding: 1 2;
    border-bottom: solid #d4cfdf;
    background: #ffffff;
}

#search-input {
    width: 100%;
    background: #faf9fc;
    border: solid #4f46e5;
    color: #1e1b2e;
    padding: 0 1;
    height: 3;
}
#search-input:focus {
    border: solid #6366f1;
    background: #ffffff;
}
#search-input > .input--placeholder {
    color: #9c97ad;
}
#search-input > .input--cursor {
    background: #4f46e5;
    color: #ffffff;
}

#filter-row {
    height: 1;
    margin: 0 0 1 0;
    padding: 0 2;
}

#filter-row Label {
    color: #6b6577;
    margin: 0 1 0 0;
}

#filter-row Label.active {
    color: #4f46e5;
    text-style: bold;
}

#status-bar {
    dock: bottom;
    height: 1;
    background: #4f46e5;
    color: #ffffff;
    padding: 0 2;
}

#status-bar.error {
    background: #dc2626;
}

#results-table {
    height: 1fr;
    background: #faf9fc;
}

DataTable {
    background: #faf9fc;
}

DataTable > .datatable--header {
    background: #f0edf7;
    color: #6b6577;
    text-style: bold;
    border-bottom: solid #d4cfdf;
}

DataTable > .datatable--cursor {
    background: #4f46e5 15%;
    color: #1e1b2e;
    text-style: bold;
}

DataTable > .datatable--hover {
    background: #4f46e5 8%;
}

/* Detail + Help */
#detail-container {
    padding: 2 3;
    overflow-y: auto;
    background: #faf9fc;
}
#detail-title {
    color: #4f46e5;
    text-style: bold;
    padding: 0 0 1 0;
}
#detail-company {
    color: #1e1b2e;
    text-style: bold;
}
.detail-meta {
    color: #6b6577;
    margin: 0;
}
.detail-divider {
    color: #d4cfdf;
    margin: 1 0;
}
.detail-section {
    color: #4f46e5;
    text-style: bold;
    margin: 1 0 0 0;
}
.detail-body {
    color: #1e1b2e;
    margin: 1 0;
}
.detail-link {
    color: #4f46e5;
    text-style: underline;
}

.help-overlay {
    background: #faf9fc 97%;
    align: center middle;
    width: 60;
    height: auto;
    max-height: 90%;
    border: solid #4f46e5;
    padding: 1 2;
}
.help-title {
    color: #4f46e5;
    text-style: bold;
    content-align: center middle;
    padding: 1;
}
.help-key {
    color: #4f46e5;
    text-style: bold;
    width: 18;
}
.help-desc {
    color: #6b6577;
}

Footer {
    background: #f0edf7;
    border-top: solid #d4cfdf;
    color: #6b6577;
}
Footer > .footer--key {
    background: #e8e3f5;
    color: #4f46e5;
}
Footer > .footer--highlight {
    color: #4f46e5;
}
"""

# ── Logo ────────────────────────────────────────────────────────────

LOGO = r"""
   ██╗ ██████╗ ██████╗   ██████╗ ██████╗  ██████╗ ██████╗ 
   ██║██╔═══██╗██╔══██╗  ██╔══██╗██╔══██╗██╔═══██╗██╔══██╗
   ██║██║   ██║██████╔╝  ██║  ██║██████╔╝██║   ██║██████╔╝
  ██║ ██║   ██║██╔══██╗  ██║  ██║██╔══██╗██║   ██║██╔═══╝ 
  ██║ ╚██████╔╝██████╔╝  ██████╔╝██║  ██║╚██████╔╝██║     
  ╚═╝  ╚═════╝ ╚═════╝   ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝     
"""

# ── Help Overlay ────────────────────────────────────────────────────

HELP = """
[bold $accent]Keyboard Shortcuts[/]

[$accent]/[/] or [$accent]s[/]          Focus search — just start typing
[$accent]Enter[/]        Run search with typed query
[$accent]Tab[/]          Jump between sidebar, search, results
[$accent]↑↓[/]          Navigate results
[$accent]→[/] or [$accent]Enter[/]  View job details (on a result)
[$accent]←[/] or [$accent]Esc[/]    Back from detail / Close help
[$accent]1-9[/]         Switch source category (1=All, 2=Major, 3=Tech…)
[$accent]a[/]           All sources  |  [$accent]n[/]  None
[$accent]r[/]           Toggle remote only
[$accent]t[/]           Toggle fulltime only
[$accent]f[/]           Open full filters panel
[$accent]o[/]           Open job URL in browser
[$accent]?[/]           Show this help  |  [$accent]q[/]  Quit

[$dim]33 job boards • type to search • arrows to navigate[/]
"""


class HelpOverlay(ModalScreen[None]):
    BINDINGS = [Binding("escape,question_mark,q,left", "dismiss", "Close")]
    def compose(self) -> ComposeResult:
        yield Container(
            Label("📋  jobdrop", classes="help-title"),
            Static(HELP),
            Label("Press Esc or any key to close", classes="help-desc"),
            classes="help-overlay",
        )
    def action_dismiss(self) -> None:
        self.dismiss()


# ── Detail Screen ───────────────────────────────────────────────────

class DetailScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape,q,left", "dismiss", "Back"),
        Binding("o", "open_url", "Open URL"),
        Binding("up", "scroll(-1)", "", show=False),
        Binding("down", "scroll(1)", "", show=False),
    ]

    def __init__(self, job: dict) -> None:
        super().__init__()
        self.job = job

    def compose(self) -> ComposeResult:
        j = self.job
        tag, color = source_tag(j.get("site", ""))
        title = j.get("title", "Untitled")
        company = j.get("company_name", j.get("company", "?"))
        loc = j.get("location", {})
        loc_str = f"{loc.get('city','')}, {loc.get('state','')}".strip(", ") if isinstance(loc, dict) else str(loc or "")

        # Salary
        sal = ""
        mn = j.get("min_amount")
        mx = j.get("max_amount")
        if mn is not None and mn > 0:
            sal = f"${mn:,.0f}" + (f" – ${mx:,.0f}" if mx and mx > 0 else "+")
            if j.get("interval"):
                sal += f" / {j.get('interval')}"

        posted = str(j.get("date_posted", ""))
        remote = "🌐 Remote" if j.get("is_remote") else ""
        jt = str(j.get("job_type", ""))
        level = str(j.get("job_level", ""))
        url = j.get("job_url_direct") or j.get("job_url", "")

        desc = str(j.get("description", ""))
        if desc:
            desc = re.sub(r'<[^>]+>', ' ', desc)
            desc = re.sub(r'\s+', ' ', desc)

        comp_desc = str(j.get("company_description", ""))
        comp_ind = j.get("company_industry", "")
        comp_url = j.get("company_url", "")
        comp_emp = j.get("company_num_employees", "")
        skills = str(j.get("skills", ""))
        exp = str(j.get("experience_range", ""))
        salary_src = str(j.get("salary_source", ""))

        with ScrollableContainer(id="detail-container"):
            yield Label(f"[{color}]{tag}[/]  {title}", id="detail-title")
            yield Label(f"{company}", id="detail-company")
            meta = "  ·  ".join(filter(None, [loc_str, sal, jt, remote, level]))
            yield Label(meta, classes="detail-meta")
            if posted:
                yield Label(f"Posted {posted}" + (f"  ·  salary: {salary_src}" if salary_src else ""), classes="detail-meta")
            if url:
                yield Label(f"🔗  {url}", classes="detail-link")

            if desc:
                yield Label("", classes="detail-divider")
                yield Label("Description", classes="detail-section")
                yield Label(desc[:6000], classes="detail-body")

            if comp_desc or comp_ind or comp_url:
                yield Label("", classes="detail-divider")
                yield Label("Company", classes="detail-section")
                if comp_desc:
                    yield Label(str(comp_desc)[:2000], classes="detail-body")
                parts = []
                if comp_ind: parts.append(f"Industry: {comp_ind}")
                if comp_emp: parts.append(f"Size: {comp_emp}")
                if parts:
                    yield Label("  ·  ".join(parts), classes="detail-meta")
                if comp_url:
                    yield Label(f"🔗  {comp_url}", classes="detail-link")

            if skills or exp:
                yield Label("", classes="detail-divider")
                yield Label("Details", classes="detail-section")
                if skills: yield Label(f"Skills: {skills}", classes="detail-meta")
                if exp: yield Label(f"Experience: {exp}", classes="detail-meta")

    def action_dismiss(self) -> None:
        self.dismiss()

    def action_open_url(self) -> None:
        url = self.job.get("job_url_direct") or self.job.get("job_url", "")
        if url:
            import webbrowser
            webbrowser.open(url)
            self.notify(f"Opened", title="Browser")

    def action_scroll(self, amount: str) -> None:
        d = int(amount)
        try:
            c = self.query_one("#detail-container", ScrollableContainer)
            if d < 0: c.scroll_up()
            else: c.scroll_down()
        except Exception:
            pass


# ── Main Screen ─────────────────────────────────────────────────────

class JobDropScreen(Screen[None]):
    """Main search screen."""

    BINDINGS = [
        Binding("/", "focus_search", "Search"),
        Binding("s", "focus_search", "", show=False),
        Binding("tab", "cycle_focus", "Next"),
        Binding("up", "cursor_up", "", show=False),
        Binding("down", "cursor_down", "", show=False),
        Binding("right", "open_detail", "", show=False),
        Binding("enter", "open_detail", "Detail"),
        Binding("escape", "focus_search", "", show=False),
        Binding("r", "toggle_remote", "Remote"),
        Binding("t", "toggle_fulltime", "Fulltime"),
        Binding("f", "open_filters", "Filters"),
        Binding("a", "select_all", "All Srcs"),
        Binding("n", "select_none", "None"),
        Binding("1", "select_category(0)", "", show=False),
        Binding("2", "select_category(1)", "", show=False),
        Binding("3", "select_category(2)", "", show=False),
        Binding("4", "select_category(3)", "", show=False),
        Binding("5", "select_category(4)", "", show=False),
        Binding("6", "select_category(5)", "", show=False),
        Binding("7", "select_category(6)", "", show=False),
        Binding("8", "select_category(7)", "", show=False),
        Binding("9", "select_category(8)", "", show=False),
        Binding("o", "open_url", "Open URL"),
        Binding("question_mark", "show_help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    selected_sources: set[str] = set()
    search_results: list[dict] = []
    _selected_category: int = 0  # 0 = All
    _searching: bool = False
    _is_remote: bool = False
    _is_fulltime: bool = False

    def __init__(self) -> None:
        super().__init__()
        self.selected_sources = set(ALL_SOURCES)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Horizontal():
            # Left sidebar — torlink style
            with Vertical(id="sidebar"):
                yield Label("", classes="sidebar-title")  # spacer
                for i, (label, desc, sources) in enumerate(CATEGORIES):
                    yield Label(label, classes="category", id=f"cat-{i}")

                yield Label("", classes="sidebar-spacer")
                yield Label("", classes="sidebar-title")  # spacer before shortcuts
                yield Label("  / search", classes="category muted")
                yield Label("  ↑↓ navigate", classes="category muted")
                yield Label("  → detail   o open", classes="category muted")
                yield Label("  ? help     q quit", classes="category muted")

            # Main area
            with Vertical(id="main"):
                yield Label(LOGO, id="logo")

                with Container(id="search-container"):
                    yield Input(
                        placeholder="Search jobs…  (e.g. 'software engineer')",
                        id="search-input",
                    )

                # Quick filter toggles
                with Horizontal(id="filter-row"):
                    yield Label("Remote", id="fl-remote")
                    yield Label("Fulltime", id="fl-fulltime")

                # Status
                yield Label("", id="status-bar")

                # Results
                yield DataTable(id="results-table")

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#results-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("", "Title", "Company", "Location", "Salary", "Type", "Source")
        self._highlight_category(0)
        self._update_filter_labels()
        self.query_one("#status-bar", Label).display = False
        # Focus search immediately
        self.query_one("#search-input", Input).focus()

    # ── Sidebar highlighting ─────────────────────────────────────

    def _highlight_category(self, idx: int) -> None:
        for i in range(len(CATEGORIES)):
            try:
                w = self.query_one(f"#cat-{i}", Label)
                if i == idx:
                    w.add_class("-selected")
                else:
                    w.remove_class("-selected")
            except NoMatches:
                pass
        self._selected_category = idx

    def _update_filter_labels(self) -> None:
        try:
            self.query_one("#fl-remote", Label).set_class(self._is_remote, "active")
            self.query_one("#fl-fulltime", Label).set_class(self._is_fulltime, "active")
        except NoMatches:
            pass

    # ── Actions ──────────────────────────────────────────────────

    def action_focus_search(self) -> None:
        """Focus the search input so user can type."""
        try:
            inp = self.query_one("#search-input", Input)
            inp.focus()
        except NoMatches:
            pass

    def action_cycle_focus(self) -> None:
        """Tab: cycle sidebar → search → results → sidebar."""
        try:
            f = self.focused
            if f and f.id == "search-input":
                table = self.query_one("#results-table", DataTable)
                if table.row_count > 0:
                    table.focus()
                else:
                    self.action_focus_search()
            elif f and isinstance(f, DataTable):
                self.action_focus_search()
            else:
                self.action_focus_search()
        except Exception:
            self.action_focus_search()

    def action_cursor_up(self) -> None:
        """Move cursor up in results (only when results focused)."""
        try:
            table = self.query_one("#results-table", DataTable)
            if table.row_count > 0 and table.has_focus:
                table.action_cursor_up()
        except Exception:
            pass

    def action_cursor_down(self) -> None:
        """Move cursor down in results (only when results focused)."""
        try:
            table = self.query_one("#results-table", DataTable)
            if table.row_count > 0 and table.has_focus:
                table.action_cursor_down()
        except Exception:
            pass

    def action_open_detail(self) -> None:
        """Open detail for selected result."""
        table = self.query_one("#results-table", DataTable)
        if table.row_count == 0:
            # If no results and enter pressed on search, run search
            inp = self.query_one("#search-input", Input)
            if inp.value.strip():
                self._run_search(inp.value.strip())
            return
        try:
            cursor = table.cursor_coordinate
            row_idx = cursor.row if cursor else None
        except Exception:
            row_idx = None
        if row_idx is not None and row_idx < len(self.search_results):
            self.app.push_screen(DetailScreen(self.search_results[row_idx]))

    def action_open_url(self) -> None:
        table = self.query_one("#results-table", DataTable)
        if table.row_count == 0:
            return
        try:
            cursor = table.cursor_coordinate
            row_idx = cursor.row if cursor else None
        except Exception:
            return
        if row_idx is not None and row_idx < len(self.search_results):
            url = self.search_results[row_idx].get("job_url_direct") or \
                  self.search_results[row_idx].get("job_url", "")
            if url:
                import webbrowser
                webbrowser.open(url)
                self._show_status("Opened in browser", "")

    def action_toggle_remote(self) -> None:
        self._is_remote = not self._is_remote
        self._update_filter_labels()
        self._rerun_if_query()

    def action_toggle_fulltime(self) -> None:
        self._is_fulltime = not self._is_fulltime
        self._update_filter_labels()
        self._rerun_if_query()

    def action_select_all(self) -> None:
        self.selected_sources = set(ALL_SOURCES)
        self._highlight_category(0)
        self.notify("All sources selected")
        self._rerun_if_query()

    def action_select_none(self) -> None:
        self.selected_sources = set()
        self._highlight_category(-1)
        self.notify("No sources selected")

    def action_select_category(self, idx_str: str) -> None:
        idx = int(idx_str)
        if 0 <= idx < len(CATEGORIES):
            _, _, sources = CATEGORIES[idx]
            if idx == 0:
                self.selected_sources = set(ALL_SOURCES)
            else:
                self.selected_sources = set(sources)
            self._highlight_category(idx)
            self._rerun_if_query()

    def action_show_help(self) -> None:
        self.app.push_screen(HelpOverlay())

    def action_open_filters(self) -> None:
        self.notify("Full filters: hours_old, distance, country, job_type etc. coming in next release", title="Filters")

    # ── Click sidebar ────────────────────────────────────────────

    def on_label_click(self, event: Label.Click) -> None:
        lid = event.label.id or ""
        if lid.startswith("cat-"):
            idx = int(lid.split("-")[1])
            self.action_select_category(str(idx))
        elif lid == "fl-remote":
            self.action_toggle_remote()
        elif lid == "fl-fulltime":
            self.action_toggle_fulltime()

    # ── Search ───────────────────────────────────────────────────

    @on(Input.Changed, "#search-input")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Live feedback: show what's being typed."""
        val = event.value.strip()
        if val:
            self._show_status(f"Search: \"{val[:60]}\" — press Enter to run", "hint")
        else:
            self._show_status("Type to search, Enter to run", "hint")

    @on(Input.Submitted, "#search-input")
    def on_search_submit(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self._run_search(query)
        else:
            self._show_status("Type a search term first", "warn")

    def _rerun_if_query(self) -> None:
        """Re-run search if there's text in the input."""
        val = self.query_one("#search-input", Input).value.strip()
        if val and self._selected_category >= 0:
            self._run_search(val)

    @work(exclusive=True, thread=True)
    async def _run_search(self, query: str) -> None:
        if not query or not self.selected_sources:
            self._show_status("Select at least one source category first", "warn")
            return

        self._searching = True
        self._show_status(f"Searching {len(self.selected_sources)} sources for \"{query[:50]}\"...", "loading")

        table = self.query_one("#results-table", DataTable)
        table.clear()
        self.search_results = []

        # Suppress all jobdrop/third-party stdout noise during scrape
        import io
        import contextlib
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()

        try:
            kwargs: dict = {
                "site_name": list(self.selected_sources),
                "search_term": query,
                "results_wanted": 20,
            }
            if self._is_remote:
                kwargs["is_remote"] = True
            if self._is_fulltime:
                kwargs["job_type"] = "fulltime"

            result = scrape_jobs(**kwargs)
        finally:
            # Restore stdout/stderr
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        try:
            if result is not None and not result.empty:
                self.search_results = result.to_dict("records")
                self._populate_table(table, self.search_results)
                self._show_status(
                    f"{len(self.search_results)} jobs from {len(self.selected_sources)} sources", "ok"
                )
                table.focus()
            else:
                self._show_status(f"No results for \"{query}\". Try broader terms or more sources.", "warn")
        except Exception as e:
            self._show_status(f"Error: {str(e)[:100]}", "error")
        finally:
            self._searching = False

    def _populate_table(self, table: DataTable, jobs: list[dict]) -> None:
        for i, job in enumerate(jobs):
            title = str(job.get("title", ""))[:50]
            company = str(job.get("company_name", job.get("company", "")))[:22]
            loc = job.get("location", {})
            loc_str = f"{loc.get('city','')}, {loc.get('state','')}"[:16].strip(", ") if isinstance(loc, dict) else str(loc or "")[:16]
            mn = job.get("min_amount")
            mx = job.get("max_amount")
            sal = f"${mn:,.0f}" + (f"-{mx:,.0f}" if mx and mx > 0 else "") if mn and mn > 0 else ""
            jt = str(job.get("job_type", ""))[:10] if job.get("job_type") else "—"
            tag, color = source_tag(job.get("site", ""))
            idx = str(i + 1)
            table.add_row(idx, title, company, loc_str, sal, jt, f"[{color}]{tag}[/]")

        if jobs:
            table.move_cursor(row=0)

    def _show_status(self, msg: str, kind: str = "") -> None:
        """Show a status message."""
        bar = self.query_one("#status-bar", Label)
        bar.display = True
        bar.update(msg)
        bar.remove_class("error")
        if kind == "error":
            bar.add_class("error")

    def _clear_status(self) -> None:
        self.query_one("#status-bar", Label).display = False


# ── App ─────────────────────────────────────────────────────────────

class JobDropApp(App[None]):
    CSS = CSS
    TITLE = "jobdrop"
    SUB_TITLE = "33 job boards"

    SCREENS = {"main": JobDropScreen}

    def on_mount(self) -> None:
        self.push_screen("main")

    def on_unmount(self) -> None:
        """Clean up scraped data on exit."""
        # Clear jobdrop cache dirs if they exist
        import shutil
        for pattern in ["jobspy", "jobdrop", "scraper_cache"]:
            for base in [Path.home() / ".cache", Path(tempfile.gettempdir())]:
                p = base / pattern
                if p.exists():
                    try:
                        shutil.rmtree(p, ignore_errors=True)
                    except Exception:
                        pass
        # Clear any temp csv/json dumps
        for f in Path.cwd().glob("jobdrop_*.csv"):
            try: f.unlink()
            except Exception: pass
        for f in Path.cwd().glob("jobdrop_*.json"):
            try: f.unlink()
            except Exception: pass


def main() -> None:
    app = JobDropApp()
    app.run()


if __name__ == "__main__":
    main()
