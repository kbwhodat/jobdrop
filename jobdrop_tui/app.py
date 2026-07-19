"""jobdrop TUI — search 33 job boards from your terminal."""

from __future__ import annotations

import sys
import re
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

# ── Suppress jobdrop's noisy logging ───────────────────────────────
# Must happen before jobdrop import
logging.getLogger("Jobdrop").setLevel(logging.WARNING)
logging.getLogger("jobdrop").setLevel(logging.WARNING)
# Also silence noisy third-party loggers
for name in ["selenium", "urllib3", "httpx", "websockets", "asyncio", "trio"]:
    logging.getLogger(name).setLevel(logging.WARNING)

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, SelectionList, Static, Switch
from textual.widgets.selection_list import Selection
from textual.widget import Widget
from textual.css.query import NoMatches
from textual.message import Message
from textual.theme import Theme
from textual.worker import get_current_worker

# Ensure jobdrop's venv is on path
_jobdrop_venv = Path.home() / ".local" / "share" / "jobdrop-venv" / "lib"
_venv_site = next(_jobdrop_venv.glob("python3*/site-packages"), None)
if _venv_site and str(_venv_site) not in sys.path:
    sys.path.insert(0, str(_venv_site))

from jobdrop import scrape_jobs, _norm_title


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


# ── Themes ─────────────────────────────────────────────────────────

ACCENT = "#4f46e5"       # Indigo — fallback for unknown source tags

# The original white theme, registered as a proper Textual theme so the
# CSS can use theme variables and built-in themes work too.
JOBDROP_LIGHT = Theme(
    name="jobdrop-light",
    primary="#4f46e5",
    secondary="#6366f1",
    accent="#6366f1",
    foreground="#1e1b2e",
    background="#faf9fc",
    surface="#ffffff",
    panel="#f0edf7",
    success="#059669",
    warning="#d97706",
    error="#dc2626",
    dark=False,
)

# ctrl+t cycles through these; the command palette (ctrl+p) can set any
# registered theme.
THEMES = [
    "jobdrop-light",
    "textual-dark",
    "nord",
    "gruvbox",
    "tokyo-night",
    "dracula",
    "solarized-light",
]

_THEME_FILE = Path.home() / ".config" / "jobdrop" / "tui-theme"

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
    background: $background;
    color: $foreground;
}

Header {
    background: $surface;
    color: $foreground;
    border-bottom: solid $foreground 15%;
}

#sidebar {
    width: 22;
    background: $panel;
    border-right: solid $foreground 15%;
    padding: 1 2;
}

.sidebar-title {
    color: $text-muted;
    text-style: bold;
    margin: 1 0 0 0;
    padding: 0 0 0 0;
}

.sidebar-spacer {
    height: 1;
}

#sidebar .category {
    color: $text-muted;
    padding: 0 0 0 3;
    height: 1;
}

#sidebar .category.-selected {
    color: $primary;
    text-style: bold;
    background: $primary 12%;
}

#sidebar .category:hover {
    background: $primary 12%;
    color: $foreground;
}

#main {
    height: 100%;
}

#logo {
    color: $primary;
    text-style: bold;
    content-align: center middle;
    padding: 1 0 0 0;
}

#search-container {
    padding: 1 2;
    border-bottom: solid $foreground 15%;
    background: $surface;
}

#search-row {
    height: 3;
}

#search-input {
    width: 1fr;
    background: $background;
    border: solid $primary;
    color: $foreground;
    padding: 0 1;
    height: 3;
}
#location-input {
    width: 34;
    background: $background;
    border: solid $foreground 25%;
    color: $foreground;
    padding: 0 1;
    height: 3;
    margin: 0 0 0 1;
}
#search-input:focus, #location-input:focus {
    border: solid $secondary;
    background: $surface;
}
#search-input > .input--placeholder, #location-input > .input--placeholder {
    color: $text-muted;
}
#search-input > .input--cursor, #location-input > .input--cursor {
    background: $primary;
    color: auto;
}

#filter-row {
    height: 1;
    margin: 0 0 1 0;
    padding: 0 2;
}

#filter-row Label {
    color: $text-muted;
    margin: 0 1 0 0;
}

#filter-row Label.active {
    color: $primary;
    text-style: bold;
}

#status-bar {
    dock: bottom;
    height: 1;
    background: $primary;
    color: auto;
    padding: 0 2;
}

#status-bar.error {
    background: $error;
}

#results-table {
    height: 1fr;
    background: $background;
}

DataTable {
    background: $background;
}

DataTable > .datatable--header {
    background: $panel;
    color: $text-muted;
    text-style: bold;
}

DataTable > .datatable--cursor {
    background: $primary 15%;
    color: $foreground;
    text-style: bold;
}

DataTable > .datatable--hover {
    background: $primary 8%;
}

/* Detail + Help */
#detail-container {
    padding: 2 3;
    overflow-y: auto;
    background: $background;
}
#detail-title {
    color: $primary;
    text-style: bold;
    padding: 0 0 1 0;
}
#detail-company {
    color: $foreground;
    text-style: bold;
}
.detail-meta {
    color: $text-muted;
    margin: 0;
}
.detail-divider {
    color: $foreground 15%;
    margin: 1 0;
}
.detail-section {
    color: $primary;
    text-style: bold;
    margin: 1 0 0 0;
}
.detail-body {
    color: $foreground;
    margin: 1 0;
}
.detail-link {
    color: $primary;
    text-style: underline;
}

.help-overlay {
    background: $background 97%;
    align: center middle;
    width: 60;
    height: auto;
    max-height: 90%;
    border: solid $primary;
    padding: 1 2;
}
.help-title {
    color: $primary;
    text-style: bold;
    content-align: center middle;
    padding: 1;
}
.help-key {
    color: $primary;
    text-style: bold;
    width: 18;
}
.help-desc {
    color: $text-muted;
}

.filter-overlay {
    background: $background 97%;
    align: center middle;
    width: 46;
    height: auto;
    max-height: 80%;
    border: solid $primary;
    padding: 1 2;
}
.filter-overlay SelectionList {
    background: transparent;
    height: auto;
    max-height: 20;
}

Footer {
    background: $panel;
    border-top: solid $foreground 15%;
    color: $text-muted;
}
Footer > .footer--key {
    background: $primary 12%;
    color: $primary;
}
Footer > .footer--highlight {
    color: $primary;
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
[$accent]↑↓[/] / [$accent]j k[/]    Navigate results ([$accent]g[/]/[$accent]G[/] top/bottom, [$accent]^D[/]/[$accent]^U[/] page)
[$accent]→[/] or [$accent]Enter[/]  View job details (on a result)
[$accent]S[/]           Filter results by source
[$accent]←[/] or [$accent]Esc[/]    Back from detail / Close help
[$accent]1-9[/]         Switch source category (1=All, 2=Major, 3=Tech…)
[$accent]a[/]           All sources  |  [$accent]n[/]  None
[$accent]l[/]           Set location (blank = anywhere)
[$accent]L[/]           Strict location (searched city + remote only)
[$accent]r[/]           Toggle remote only
[$accent]t[/]           Toggle fulltime only
[$accent]f[/]           Open full filters panel
[$accent]o[/]           Open job URL in browser
[$accent]Ctrl+T[/]      Cycle theme
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
        Binding("up,k", "scroll(-1)", "", show=False),
        Binding("down,j", "scroll(1)", "", show=False),
        Binding("g", "scroll_top", "", show=False),
        Binding("G", "scroll_bottom", "", show=False),
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

    def action_scroll_top(self) -> None:
        try:
            self.query_one("#detail-container", ScrollableContainer).scroll_home()
        except NoMatches:
            pass

    def action_scroll_bottom(self) -> None:
        try:
            self.query_one("#detail-container", ScrollableContainer).scroll_end()
        except NoMatches:
            pass


# ── Source Filter ───────────────────────────────────────────────────

class SourceFilterScreen(ModalScreen[Optional[set]]):
    """Filter already-fetched results by source. Returns the selected
    source set, or None for cancel (no change)."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "apply", "Apply", priority=True),
        Binding("j", "list_down", "", show=False),
        Binding("k", "list_up", "", show=False),
        Binding("a", "select_all_sources", "All"),
        Binding("n", "select_no_sources", "None"),
    ]

    def __init__(self, counts: dict[str, int], active: Optional[set]) -> None:
        super().__init__()
        self.counts = counts
        self.active = active

    def compose(self) -> ComposeResult:
        options = []
        for site, count in sorted(self.counts.items(), key=lambda kv: -kv[1]):
            selected = self.active is None or site in self.active
            options.append(Selection(f"{site}  ({count})", site, selected))
        yield Container(
            Label("Filter by source", classes="help-title"),
            SelectionList(*options, id="source-list"),
            Label("Space toggle · a all · n none · Enter apply · Esc cancel", classes="help-desc"),
            classes="filter-overlay",
        )

    def on_mount(self) -> None:
        self.query_one("#source-list", SelectionList).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_apply(self) -> None:
        self.dismiss(set(self.query_one("#source-list", SelectionList).selected))

    def action_list_down(self) -> None:
        self.query_one("#source-list", SelectionList).action_cursor_down()

    def action_list_up(self) -> None:
        self.query_one("#source-list", SelectionList).action_cursor_up()

    def action_select_all_sources(self) -> None:
        self.query_one("#source-list", SelectionList).select_all()

    def action_select_no_sources(self) -> None:
        self.query_one("#source-list", SelectionList).deselect_all()


# ── Main Screen ─────────────────────────────────────────────────────

class JobDropScreen(Screen[None]):
    """Main search screen."""

    BINDINGS = [
        Binding("/", "focus_search", "Search"),
        Binding("s", "focus_search", "", show=False),
        Binding("tab", "cycle_focus", "Next"),
        Binding("up,k", "cursor_up", "", show=False),
        Binding("down,j", "cursor_down", "", show=False),
        Binding("g", "cursor_top", "", show=False),
        Binding("G", "cursor_bottom", "", show=False),
        Binding("ctrl+d", "page_down", "", show=False),
        Binding("ctrl+u", "page_up", "", show=False),
        Binding("right", "open_detail", "", show=False),
        Binding("enter", "open_detail", "Detail"),
        Binding("S", "filter_sources", "Src Filter"),
        Binding("escape", "focus_search", "", show=False),
        Binding("l", "focus_location", "Location"),
        Binding("L", "toggle_strict_location", "Strict Loc"),
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
    search_results: list[dict] = []      # visible (post source-filter)
    _all_results: list[dict] = []        # everything fetched this search
    _source_filter: Optional[set] = None  # None = show all
    _selected_category: int = 0  # 0 = All
    _searching: bool = False
    _is_remote: bool = False
    _is_fulltime: bool = False
    _location: str = ""
    _searched_location: str = ""   # location the current results were fetched with
    _strict_location: bool = False

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
                    with Horizontal(id="search-row"):
                        yield Input(
                            placeholder="Search jobs…  (e.g. 'software engineer')",
                            id="search-input",
                        )
                        yield Input(
                            placeholder="Location  (e.g. 'Austin, TX')",
                            id="location-input",
                        )

                # Quick filter toggles
                with Horizontal(id="filter-row"):
                    yield Label("Remote", id="fl-remote")
                    yield Label("Fulltime", id="fl-fulltime")
                    yield Label("Strict Loc", id="fl-strict")

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
            self.query_one("#fl-strict", Label).set_class(self._strict_location, "active")
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

    def action_focus_location(self) -> None:
        try:
            self.query_one("#location-input", Input).focus()
        except NoMatches:
            pass

    def action_cycle_focus(self) -> None:
        """Tab: cycle search → location → results → search."""
        try:
            f = self.focused
            if f and f.id == "search-input":
                self.query_one("#location-input", Input).focus()
            elif f and f.id == "location-input":
                table = self.query_one("#results-table", DataTable)
                if table.row_count > 0:
                    table.focus()
                else:
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

    def action_cursor_top(self) -> None:
        table = self.query_one("#results-table", DataTable)
        if table.row_count > 0 and table.has_focus:
            table.move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        table = self.query_one("#results-table", DataTable)
        if table.row_count > 0 and table.has_focus:
            table.move_cursor(row=table.row_count - 1)

    def action_page_down(self) -> None:
        table = self.query_one("#results-table", DataTable)
        if table.row_count > 0 and table.has_focus:
            table.action_page_down()

    def action_page_up(self) -> None:
        table = self.query_one("#results-table", DataTable)
        if table.row_count > 0 and table.has_focus:
            table.action_page_up()

    def action_filter_sources(self) -> None:
        if not self._all_results:
            self.notify("Run a search first", title="Source filter")
            return
        counts: dict[str, int] = {}
        for job in self._all_results:
            site = job.get("site", "?")
            counts[site] = counts.get(site, 0) + 1

        def applied(selected: Optional[set]) -> None:
            if selected is None:
                return
            if not selected:
                self.notify("Nothing selected — showing all sources", title="Source filter")
                selected = None
            elif selected >= set(counts):
                selected = None  # everything selected = no filter
            self._source_filter = selected
            self._refresh_visible()

        self.app.push_screen(SourceFilterScreen(counts, self._source_filter), applied)

    def action_toggle_strict_location(self) -> None:
        self._strict_location = not self._strict_location
        self._update_filter_labels()
        if self._strict_location and not self._searched_location:
            self.notify("Needs a location — set one and search first", title="Strict location")
        if self._all_results:
            self._refresh_visible()

    def _matches_filter(self, job: dict) -> bool:
        if self._source_filter and job.get("site") not in self._source_filter:
            return False
        return self._matches_location(job)

    def _matches_location(self, job: dict) -> bool:
        """Strict mode: keep jobs in the searched city, plus remote jobs.

        Boards pad location searches with nearby/anywhere roles; this
        verifies against the location each job actually reports."""
        if not self._strict_location or not self._searched_location:
            return True
        loc = str(job.get("location") or "").lower()
        rem = job.get("is_remote")
        if rem is True or (isinstance(rem, str) and rem.lower() == "true") or "remote" in loc:
            return True
        city = self._searched_location.split(",")[0].strip().lower()
        return bool(city) and city in loc

    def _refresh_visible(self) -> None:
        """Rebuild the table from _all_results with the source filter applied."""
        table = self.query_one("#results-table", DataTable)
        table.clear()
        visible = [j for j in self._all_results if self._matches_filter(j)]
        self.search_results = visible
        self._populate_table(table, visible)
        if visible:
            table.move_cursor(row=0)
        active = []
        if self._source_filter:
            active.append(f"{len(self._source_filter)} sources")
        if self._strict_location and self._searched_location:
            active.append(f"in {self._searched_location.split(',')[0].strip()[:20]} + remote")
        if active:
            self._show_status(
                f"{len(visible)}/{len(self._all_results)} jobs  ·  " + "  ·  ".join(active), "ok"
            )
        else:
            self._show_status(f"{len(visible)} jobs  ·  no filters", "ok")

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

    def on_click(self, event: events.Click) -> None:
        try:
            widget, _ = self.get_widget_at(event.screen_x, event.screen_y)
        except NoMatches:
            return
        lid = getattr(widget, "id", "") or ""
        if lid.startswith("cat-"):
            self.action_select_category(lid.split("-")[1])
        elif lid == "fl-remote":
            self.action_toggle_remote()
        elif lid == "fl-fulltime":
            self.action_toggle_fulltime()
        elif lid == "fl-strict":
            self.action_toggle_strict_location()

    # ── Search ───────────────────────────────────────────────────

    @on(Input.Changed, "#search-input")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Live feedback: show what's being typed."""
        val = event.value.strip()
        if val:
            self._show_status(f"Search: \"{val[:60]}\" — press Enter to run", "hint")
        else:
            self._show_status("Type to search, Enter to run", "hint")

    @on(Input.Changed, "#location-input")
    def on_location_changed(self, event: Input.Changed) -> None:
        self._location = event.value.strip()

    @on(Input.Submitted, "#search-input")
    @on(Input.Submitted, "#location-input")
    def on_search_submit(self, event: Input.Submitted) -> None:
        query = self.query_one("#search-input", Input).value.strip()
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
    def _run_search(self, query: str) -> None:
        """Scrape each source in its own thread and stream results into the
        table as sources complete, instead of blocking on the slowest one.

        Runs in a worker thread — all UI updates must go through
        call_from_thread.
        """
        call = self.app.call_from_thread
        if not query or not self.selected_sources:
            call(self._show_status, "Select at least one source category first", "warn")
            return

        worker = get_current_worker()
        sources = sorted(self.selected_sources)
        kwargs: dict = {"search_term": query, "results_wanted": 20}
        if self._location:
            kwargs["location"] = self._location
        if self._is_remote:
            kwargs["is_remote"] = True
        if self._is_fulltime:
            kwargs["job_type"] = "fulltime"

        self._searching = True
        call(self._start_search_ui, query, self._location, len(sources))

        # Streaming loses scrape_jobs' cross-source dedup (it only sees one
        # source per call), so dedup incrementally here with the same key.
        seen: set[tuple[str, str]] = set()
        done = 0
        pool = ThreadPoolExecutor(max_workers=len(sources))
        try:
            futures = {
                pool.submit(self._scrape_one, site, dict(kwargs)): site
                for site in sources
            }
            for future in as_completed(futures):
                if worker.is_cancelled:
                    return
                done += 1
                try:
                    records = future.result()
                except Exception:
                    records = []
                fresh = []
                for job in records:
                    key = (
                        str(job.get("company") or job.get("company_name") or "").lower().strip(),
                        _norm_title(str(job.get("title") or "")),
                    )
                    if all(key):
                        if key in seen:
                            continue
                        seen.add(key)
                    fresh.append(job)
                call(self._add_results, fresh, done, len(sources))
        finally:
            # Don't block on stragglers; leaked scrapes finish harmlessly.
            pool.shutdown(wait=False, cancel_futures=True)
            self._searching = False

    @staticmethod
    def _scrape_one(site: str, kwargs: dict) -> list[dict]:
        result = scrape_jobs(site_name=[site], **kwargs)
        if result is None or result.empty:
            return []
        return result.to_dict("records")

    def _start_search_ui(self, query: str, location: str, total: int) -> None:
        table = self.query_one("#results-table", DataTable)
        table.clear()
        self.search_results = []
        self._all_results = []
        self._source_filter = None
        self._searched_location = location
        where = f" in {location[:30]}" if location else ""
        self._show_status(f"Searching {total} sources for \"{query[:50]}\"{where}…  0/{total} done", "loading")

    def _add_results(self, jobs: list[dict], done: int, total: int) -> None:
        table = self.query_one("#results-table", DataTable)
        self._all_results.extend(jobs)
        visible = [j for j in jobs if self._matches_filter(j)]
        first_batch = not self.search_results and visible
        start = len(self.search_results)
        self.search_results.extend(visible)
        self._populate_table(table, visible, start=start)
        n = len(self.search_results)
        if done < total:
            self._show_status(f"{n} jobs  ·  {done}/{total} sources done…", "loading")
        elif n:
            self._show_status(f"{n} jobs from {total} sources", "ok")
        else:
            self._show_status("No results. Try broader terms or more sources.", "warn")
        if first_batch:
            table.move_cursor(row=0)
            table.focus()

    @on(DataTable.RowSelected, "#results-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a focused DataTable is consumed by the table itself and
        surfaces as RowSelected — the screen-level enter binding never fires."""
        idx = event.cursor_row
        if idx is not None and 0 <= idx < len(self.search_results):
            self.app.push_screen(DetailScreen(self.search_results[idx]))

    def _populate_table(self, table: DataTable, jobs: list[dict], start: int = 0) -> None:
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
            idx = str(start + i + 1)
            table.add_row(idx, title, company, loc_str, sal, jt, f"[{color}]{tag}[/]")

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
    BINDINGS = [Binding("ctrl+t", "cycle_theme", "Theme")]

    def on_mount(self) -> None:
        self.register_theme(JOBDROP_LIGHT)
        saved = None
        try:
            saved = _THEME_FILE.read_text().strip()
        except OSError:
            pass
        self.theme = saved if saved in self.available_themes else "jobdrop-light"
        # Fires for ctrl+t and command-palette changes alike
        self.theme_changed_signal.subscribe(self, self._save_theme)
        self.push_screen("main")

    def _save_theme(self, theme: Theme) -> None:
        try:
            _THEME_FILE.parent.mkdir(parents=True, exist_ok=True)
            _THEME_FILE.write_text(theme.name)
        except OSError:
            pass

    def action_cycle_theme(self) -> None:
        try:
            idx = THEMES.index(self.theme)
        except ValueError:
            idx = -1
        self.theme = THEMES[(idx + 1) % len(THEMES)]
        self.notify(self.theme, title="Theme")


def main() -> None:
    # Scraper log handlers bind the real stderr at import time, and the
    # browser-based scrapers spawn subprocesses that write to fd 2 directly —
    # both bypass sys.stderr swaps and corrupt the TUI. Kill logging output
    # entirely and redirect fd 2 to a log file. Textual renders to stderr,
    # so hand its driver a duplicate of the real terminal first — only
    # subprocesses (which inherit fd 2) end up in the log.
    logging.disable(logging.CRITICAL)
    log_path = Path.home() / ".cache" / "jobdrop-tui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    term = os.fdopen(os.dup(2), "w")
    os.dup2(log_fd, 2)
    os.close(log_fd)
    sys.stderr = sys.__stderr__ = term
    app = JobDropApp()
    app.run()


if __name__ == "__main__":
    main()
