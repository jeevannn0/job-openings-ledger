#!/usr/bin/env python3
"""Refresh the public job ledger (state/jobs.json) and render index.html.

Python 3.10+ standard library only.

Safety rules the code enforces:

* Only HTTPS requests to hosts listed in config/public_sources.json with
  ``"fetch": true``. Redirects must stay on the same host. Responses are
  size-capped, requests are sequential with per-host delays, and robots.txt
  is honoured where configured.
* A failed, blocked, partial, or ambiguous source keeps that company's
  last-known-good rows. Rows are removed only on an explicit closed signal
  (HTTP 410, a "no longer available"/"filled" page, a closed Workday flag),
  a passed closing date, the 30-day age-out, or two authoritative absences
  from a complete listing. Search absence alone never removes a row at once.
  A run that would remove most of a company's rows is held, and the removal is applied only
  when a second complete healthy run at least one cadence later sees the same removal.
* Reports contain counts, public job IDs, and reason codes only; fetched
  payloads are never logged.
* Private exclusions come from the EXCLUDED_PUBLIC_IDS_JSON environment
  variable or the git-ignored config/excluded_public_ids.local.json. They are
  never written to a tracked file or named in reports; bad input fails closed.

Usage (run from anywhere; paths resolve against the repository root):
  python scripts/refresh.py refresh [--now ISO] [--offline] [--dry-run]
                                    [--fixtures FILE] [--github-output FILE] [--summary FILE]
  python scripts/refresh.py render [--out PATH]
  python scripts/refresh.py check
  python scripts/refresh.py scan [ROOT]
  python scripts/refresh.py seed --from-html PATH --as-of YYYY-MM-DD
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import email.utils
import hashlib
import html
import http.client
import json
import os
import random
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zlib
from typing import Callable, Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = "state/jobs.json"
TEMPLATE_PATH = "templates/index.template.html"
INDEX_PATH = "index.html"
CONFIG_PATH = "config/public_sources.json"
# Private exclusions are never published: CI passes them in this environment variable (a repository
# secret); local runs may use the git-ignored file instead. Missing or malformed input fails closed.
EXCLUDED_ENV = "EXCLUDED_PUBLIC_IDS_JSON"
EXCLUDED_LOCAL_PATH = "config/excluded_public_ids.local.json"
MAX_EXCLUDED_BYTES = 65_536

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")  # Asia/Kolkata has no DST
MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
BANDS = ("n", "1", "2")
LIVENESS = ("live", "unconfirmed", "absent")
COMPANY_STATUSES = ("seed", "ok", "stale", "static")

USER_AGENT = "job-openings-ledger-refresh/2 (public job-ledger bot; low-rate, sequential)"
ROBOTS_TOKEN = "job-openings-ledger-refresh"
HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"
JSON_ACCEPT = "application/json"

MAX_BYTES = 4_000_000          # decoded body cap
MAX_ROBOTS_BYTES = 500_000
REQUEST_TIMEOUT_S = 25.0       # per socket operation
REQUEST_WALL_S = 60.0          # whole response, so a slow-drip server cannot hold a request open
MAX_ATTEMPTS = 3               # per request, transient failures only
MAX_RETRY_AFTER_S = 60.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3
BLOCK_TRIP = 3                 # consecutive blocked page checks before a source stops probing
REMOVAL_GUARD_MIN = 3          # a run may not remove >= this many rows AND more than half
REMOVAL_DETAIL = "removal guard"
REFRESH_CADENCE = dt.timedelta(hours=6)   # matches the workflow cron; a held removal needs a re-check this much later
MAX_HOLD_IDS = 2000
SHRINK_MIN_PRIOR = 10          # listing-size shrink guard applies once a listing had this many
SHRINK_DETAIL = "suspicious listing shrink"
SCREENED_TTL_DAYS = 45
# screened[id] = "YYYY-MM-DD" (day screened) or "YYYY-MM-DD#<fingerprint>" when the source record is
# fingerprinted; a changed fingerprint means the record changed and the ID is screened again.
SCREENED_VALUE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:#([0-9a-f]{12}))?")
ABSENCES_TO_REMOVE = 2

# --------------------------------------------------------------------------- company specs
# Endpoints live in code; config only selects a mode and tunes caps/delays.


@dataclasses.dataclass(frozen=True)
class CompanySpec:
    url_re: re.Pattern
    modes: frozenset
    hosts: frozenset          # hosts the company's links and fetches may use


def _spec(url_re: str, modes: Iterable[str], hosts: Iterable[str]) -> CompanySpec:
    return CompanySpec(re.compile(url_re), frozenset(modes) | {"static"}, frozenset(hosts))


COMPANY_SPECS = {
    "Siemens": _spec(r"https://jobs\.siemens\.com/en_US/externaljobs/JobDetail/(\d{5,7})",
                     {"siemens_avature_html"}, {"jobs.siemens.com"}),
    "GE Vernova": _spec(r"https://careers\.gevernova\.com/[a-z0-9]+(?:-[a-z0-9]+)*/job/(R\d{6,8})",
                        {"gevernova_paradox_api"}, {"careers.gevernova.com"}),
    "IQVIA": _spec(r"https://jobs\.iqvia\.com/en/jobs/(R\d{6,8})-0",
                   {"iqvia_workday_cxs"}, {"jobs.iqvia.com", "iqvia.wd1.myworkdayjobs.com"}),
    "JPMorganChase": _spec(r"https://jpmc\.fa\.oraclecloud\.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/(\d{6,12})",
                           (), {"jpmc.fa.oraclecloud.com"}),
    "GE HealthCare": _spec(r"https://gehc\.wd5\.myworkdayjobs\.com/GEHC_ExternalSite/job/[A-Za-z0-9-]+/"
                           r"[A-Za-z0-9-]+_(R\d{6,8})(?:-\d{1,3})?",
                           (), {"gehc.wd5.myworkdayjobs.com"}),
    "ZS": _spec(r"https://jobs\.zs\.com/jobs/(\d{3,8})", (), {"jobs.zs.com"}),
}
JOB_ID_RE = {
    "Siemens": re.compile(r"\d{5,7}"), "GE Vernova": re.compile(r"R\d{6,8}"),
    "IQVIA": re.compile(r"R\d{6,8}"), "JPMorganChase": re.compile(r"\d{6,12}"),
    "GE HealthCare": re.compile(r"R\d{6,8}"), "ZS": re.compile(r"\d{3,8}"),
}

SIEMENS_BASE = "https://jobs.siemens.com"
SIEMENS_SEARCH = SIEMENS_BASE + "/en_US/externaljobs/SearchJobs"
SIEMENS_LIST = SIEMENS_SEARCH + "/?folderRecordsPerPage=6&folderOffset={offset}&search={search}"
SIEMENS_DETAIL = SIEMENS_BASE + "/en_US/externaljobs/JobDetail/{id}"
# Server-side session sort (newest posted first); facet field names come from the portal form.
SIEMENS_SORT_BODY = ("folderSort=postedDate&folderSortDirection=DESC&listFilterMode=true&search={search}"
                     "&42387_hidden=1&42388_hidden=1&42394=&42395=&timeZone=UTC")
SIEMENS_PAGE_SIZE = 6
SIEMENS_CARD = re.compile(
    r'article__header__text__title[^"]*"[^>]*>\s*<a[^>]*href="[^"]*JobDetail/(?:[^"/?#]+/)?(\d{5,7})"[^>]*>(.*?)</a>',
    re.S)
SIEMENS_FIELD = re.compile(
    r'article__content__view__field__label"[^>]*>(.*?)</div>\s*'
    r'<div[^>]*article__content__view__field__value"[^>]*>(.*?)</div>', re.S)

IQVIA_API = "https://iqvia.wd1.myworkdayjobs.com/wday/cxs/iqvia/IQVIA"
IQVIA_FRONT = "https://jobs.iqvia.com/en/jobs/{id}-0"
WD_PATH = re.compile(r"/job/[A-Za-z0-9._~%-]+/[A-Za-z0-9._~%-]+_(R\d{6,8})(?:-\d{1,3})?")
WD_PAGE = 20

GE_BASE = "https://careers.gevernova.com"
GE_BOOT = GE_BASE + "/jobs"
GE_LIST = (GE_BASE + "/api/get-jobs?radius=15&filter%5Bcity%5D%5B0%5D={city}"
           "&enable_kilometers=false&page_size=20&page_number={page}")
GE_LIST_BODY = {"disable_switch_search_mode": False, "site_available_languages": ["de", "fr", "en", "en-us"]}
GE_PATH = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*/job/(R\d{6,8})")
GE_PAGE = 20

# --------------------------------------------------------------------------- public boundary
# Character classes keep this file from matching itself during the scan.
FORBIDDEN_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"a[t]oz",
    r"a[2]z\.com",
    r"\bam[a]zon",
    r"\bmid[w]ay\b",
    r"employee[ _-]?i[d]\b",
    r"intern[a]l[ _-]?transfer",
    r"\bset-co[o]kie\s*:",
    r"(?:^|[\s\"'])co[o]kie\s*:",
    r"authori[z]ation\s*:\s*(?:bearer|basic)\s+[A-Za-z0-9+/=._~-]{16,}",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    r"\bgithub_p[a]t_",
    r"\bAK[I]A[0-9A-Z]{16}\b",
    r"-----BEG[I]N [A-Z ]*PRIVATE KEY",
    r"[A-Za-z]:(?:\\{1,2}|/)[U]sers(?:\\{1,2}|/)",
    r"/(?:home|U[s]ers)/[A-Za-z0-9_.-]+/",
    r"applic[a]tions\.csv",
    r"skil[l]s\.yaml",
    r"HONESTY_[N]OTE",
    r"NOT_[C]LAIMED",
    r"\bjeev[a]n",
    r"\bdivy[a]",
    r"tr[a]cker\.py",
    r"ats_[s]core",
    r"\.k[i]ro\b",
    r"[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com\b)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}",
))

ALLOWED_FILES = frozenset({
    ".gitignore",
    ".github/workflows/refresh.yml",
    "README.md",
    "index.html",
    "config/public_sources.json",
    "scripts/refresh.py",
    "state/jobs.json",
    "templates/index.template.html",
    "tests/test_refresh.py",
})
ALLOWED_FIXTURE = re.compile(r"tests/fixtures/[a-z0-9_.-]+\.(?:html|json)")

CHALLENGE_TITLES = ("just a moment", "attention required", "access denied", "request rejected",
                    "pardon our interruption", "security check", "are you a robot")
CHALLENGE_MARKERS = ("cf-chl", "challenge-platform", "/cdn-cgi/challenge", "captcha-delivery", "px-captcha")
MAINTENANCE_PHRASES = ("scheduled maintenance", "down for maintenance", "under maintenance",
                       "temporarily unavailable", "service unavailable")
CLOSED_PHRASES = (
    "this role is no longer available", "this job is no longer available",
    "this position is no longer available", "job is no longer available",
    "no longer accepting applications", "position has been filled", "job has been filled",
    "this job is closed", "job posting has expired", "this posting has expired",
)

SENIOR_TITLE = re.compile(
    r"\b(?:senior|sr|lead|manager|principal|staff|architect|director|head|vp|vice president)\b", re.I)
_WORD_NUM = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}
_N = r"(\d{1,2}(?:\.\d+)?|" + "|".join(_WORD_NUM) + r")"
YEARS_RE = re.compile(rf"\b{_N}\s*\+?\s*(?:(?:-|–|—|to)\s*{_N}\s*\+?\s*)?(?:years?|yrs?)\b", re.I)
EXPERIENCE_CUE = re.compile(
    r"experience|experienced|background|working|work|relevant|industry|hands-on|minimum|at least|proven", re.I)
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


class ConfigError(ValueError):
    pass


# --------------------------------------------------------------------------- text and dates

def label_for(day: dt.date) -> str:
    return f"{MON[day.month - 1]} {day.day}"


def long_date(day: dt.date) -> str:
    return f"{day.day} {MON[day.month - 1]} {day.year}"


_MONTHS = {m.lower(): i + 1 for i, m in enumerate(MON)}


def parse_any_date(value: object) -> dt.date | None:
    """ISO (YYYY-MM-DD[...]), 30-Oct-2026, 30 October 2026, or Oct 30, 2026."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    try:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:$|[T ])", s)
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.fullmatch(r"(\d{1,2})[- ]([A-Za-z]{3,9})\.?[- ,]+(\d{4})", s)
        if m and m.group(2)[:3].lower() in _MONTHS:
            return dt.date(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
        m = re.fullmatch(r"([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})", s)
        if m and m.group(1)[:3].lower() in _MONTHS:
            return dt.date(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    except ValueError:
        return None
    return None


def iso_date(value: object) -> dt.date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def iso_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?[+-]\d{2}:\d{2}", value):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def stamp(now: dt.datetime) -> str:
    return now.astimezone(IST).isoformat(timespec="minutes")


def workday_closing(end_date: object) -> str | None:
    """Workday endDate is exclusive (the posting stops at the start of that day)."""
    d = parse_any_date(end_date)
    return (d - dt.timedelta(days=1)).isoformat() if d else None


def generic_closing(value: object) -> str | None:
    """Closing/end dates published as an inclusive last day."""
    d = parse_any_date(value)
    return d.isoformat() if d else None


def clean_text(value: object, limit: int = 200) -> str:
    text = html.unescape(str(value or ""))
    text = CONTROL_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def strip_html(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", markup or "")
    markup = re.sub(r"(?i)<br\s*/?>|</(?:p|li|h\d|div|tr)>", "\n", markup)
    markup = re.sub(r"<[^>]+>", " ", markup)
    markup = html.unescape(markup).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", markup)


def find_forbidden(text: str) -> list[str]:
    """Pattern sources that hit; never the matched text, so reports cannot echo content."""
    return [p.pattern for p in FORBIDDEN_PATTERNS if p.search(text or "")]


def js_json(value: object) -> str:
    """JSON that is inert inside an inline <script>: no '<' or '>' and no JS line terminators."""
    out = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (out.replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def join_places(places: Iterable[str], limit: int = 5) -> str:
    uniq: list[str] = []
    for p in places:
        p = clean_text(p, 40)
        if p and p not in uniq:
            uniq.append(p)
    if len(uniq) > limit:
        return f"{', '.join(uniq[:limit])}, and {len(uniq) - limit} other cities"
    if len(uniq) <= 2:
        return " and ".join(uniq)
    return ", ".join(uniq[:-1]) + ", and " + uniq[-1]


# --------------------------------------------------------------------------- config

@dataclasses.dataclass(frozen=True)
class HostPolicy:
    fetch: bool
    robots: bool
    min_delay_s: float


@dataclasses.dataclass(frozen=True)
class Config:
    max_age_days: int
    max_years: float
    cities: tuple
    hosts: dict
    companies: tuple          # ordered company names (page order)
    sources: dict             # company -> dict (mode + tunables)

    @property
    def fetch_hosts(self) -> frozenset:
        return frozenset(h for h, p in self.hosts.items() if p.fetch)

    @property
    def link_hosts(self) -> frozenset:
        return frozenset(self.hosts)


_TERM = re.compile(r"[A-Za-z][A-Za-z ]{2,29}")
_TUNABLES = {
    "siemens_avature_html": {"budget_s": (60, 900), "search_text": "term", "discovery_interval_h": (24, 168),
                             "max_list_pages": (1, 80), "probe_every_pages": (1, 10), "max_new_details": (0, 40)},
    "iqvia_workday_cxs": {"budget_s": (30, 600), "search_terms": "terms", "max_total": (20, 1000),
                          "max_new_details": (0, 60)},
    "gevernova_paradox_api": {"budget_s": (30, 600), "city": "term", "max_total": (20, 1000),
                              "max_new_checks": (0, 40)},
    "static": {},
}
SIEMENS_MIN_DELAY_S = 2.0


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def load_config(data: dict) -> Config:
    _check(isinstance(data, dict) and data.get("schema") == 1, "config: schema must be 1")
    _check(set(data) == {"schema", "timezone", "filters", "hosts", "companies"}, "config: unexpected keys")
    _check(data["timezone"] == "Asia/Kolkata", "config: timezone must be Asia/Kolkata")
    f = data["filters"]
    _check(isinstance(f, dict) and set(f) == {"max_age_days", "max_years", "cities"}, "config: bad filters")
    _check(isinstance(f["max_age_days"], int) and 1 <= f["max_age_days"] <= 60, "config: max_age_days")
    _check(_num(f["max_years"]) and 0 <= f["max_years"] <= 10, "config: max_years")
    _check(isinstance(f["cities"], list) and f["cities"] and
           all(isinstance(c, str) and re.fullmatch(r"[a-z]{3,30}", c) for c in f["cities"]), "config: cities")

    hosts: dict[str, HostPolicy] = {}
    _check(isinstance(data["hosts"], dict), "config: hosts")
    for host, pol in data["hosts"].items():
        _check(isinstance(host, str) and re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}", host) is not None,
               f"config: bad host {host!r}")
        _check(isinstance(pol, dict) and set(pol) == {"fetch", "robots", "min_delay_s"}, f"config: host {host}")
        _check(isinstance(pol["fetch"], bool) and isinstance(pol["robots"], bool), f"config: host {host} flags")
        _check(_num(pol["min_delay_s"]) and 0.5 <= pol["min_delay_s"] <= 10, f"config: host {host} delay")
        hosts[host] = HostPolicy(pol["fetch"], pol["robots"], float(pol["min_delay_s"]))

    companies: list[str] = []
    sources: dict[str, dict] = {}
    _check(isinstance(data["companies"], list) and data["companies"], "config: companies")
    needed_fetch: set[str] = set()
    needed_link: set[str] = set()
    for entry in data["companies"]:
        _check(isinstance(entry, dict), "config: company entry")
        name, mode = entry.get("company"), entry.get("mode")
        _check(name in COMPANY_SPECS and name not in sources, f"config: unknown or duplicate company {name!r}")
        spec = COMPANY_SPECS[name]
        _check(mode in spec.modes, f"config: mode {mode!r} not allowed for {name}")
        allowed = _TUNABLES[mode]
        _check(set(entry) == {"company", "mode", *allowed}, f"config: {name} keys must be exactly {sorted(allowed)}")
        for key, rule in allowed.items():
            v = entry[key]
            if rule == "term":
                _check(isinstance(v, str) and _TERM.fullmatch(v) is not None, f"config: {name}.{key}")
            elif rule == "terms":
                _check(isinstance(v, list) and 1 <= len(v) <= 5 and
                       all(isinstance(t, str) and _TERM.fullmatch(t) for t in v), f"config: {name}.{key}")
            else:
                lo, hi = rule
                _check(isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi, f"config: {name}.{key}")
        needed_link |= spec.hosts
        if mode != "static":
            needed_fetch |= spec.hosts
        companies.append(name)
        sources[name] = dict(entry)
    _check(set(hosts) == needed_link, "config: hosts must be exactly the hosts the configured companies use")
    _check({h for h, p in hosts.items() if p.fetch} == needed_fetch,
           "config: fetch must be true exactly for hosts of non-static sources")
    if "jobs.siemens.com" in needed_fetch:
        _check(hosts["jobs.siemens.com"].min_delay_s >= SIEMENS_MIN_DELAY_S, "config: Siemens delay must be >= 2s")
    return Config(f["max_age_days"], float(f["max_years"]), tuple(f["cities"]), hosts, tuple(companies), sources)


def load_excluded(data: dict, config: Config) -> dict[str, frozenset]:
    """Validate {"schema": 1, "ids": {company: [id, ...]}}. Errors never echo the private input."""
    _check(isinstance(data, dict) and set(data) == {"schema", "ids"} and data["schema"] == 1, "excluded: schema")
    _check(isinstance(data["ids"], dict), "excluded: ids")
    out: dict[str, frozenset] = {c: frozenset() for c in config.companies}
    for company, ids in data["ids"].items():
        _check(company in config.companies, "excluded: unknown company")
        _check(isinstance(ids, list) and all(isinstance(i, str) and JOB_ID_RE[company].fullmatch(i) for i in ids),
               f"excluded: bad IDs for {company}")
        out[company] = frozenset(ids)
    return out


def read_excluded(root: str, config: Config, environ: dict | None = None) -> dict[str, frozenset]:
    """Private exclusions from EXCLUDED_PUBLIC_IDS_JSON, else the git-ignored local file.

    A set-but-empty variable (an unset GitHub secret) is an error, as is having neither source,
    so a missing secret can never publish rows that should be hidden.
    """
    environ = os.environ if environ is None else environ
    if EXCLUDED_ENV in environ:
        raw, where = environ[EXCLUDED_ENV], EXCLUDED_ENV
        _check(raw.strip() != "", f"excluded: {where} is empty")
    else:
        where = EXCLUDED_LOCAL_PATH
        try:
            raw = read_text(os.path.join(root, EXCLUDED_LOCAL_PATH))
        except FileNotFoundError:
            raise ConfigError(f"excluded: set {EXCLUDED_ENV} or create {EXCLUDED_LOCAL_PATH}") from None
    _check(len(raw.encode("utf-8")) <= MAX_EXCLUDED_BYTES, f"excluded: {where} is too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ConfigError(f"excluded: {where} is not valid JSON") from None
    return load_excluded(data, config)


# --------------------------------------------------------------------------- state

ROW_FIELDS = ("company", "job_id", "title", "experience_text", "experience_band", "location", "canonical_url",
              "posted_date", "closing_date", "first_seen", "last_seen", "liveness", "absent_runs")
COMPANY_FIELDS = ("status", "detail", "last_success", "last_attempt", "stale_since", "listing_total",
                  "last_discovery_attempt", "screened", "removal_hold")
LEGACY_COMPANY_FIELDS = COMPANY_FIELDS[:-1]     # state written before removal_hold existed


@dataclasses.dataclass(frozen=True)
class Row:
    company: str
    job_id: str
    title: str
    experience_text: str
    experience_band: str
    location: str
    canonical_url: str
    posted_date: str
    closing_date: str | None
    first_seen: str
    last_seen: str
    liveness: str = "live"
    absent_runs: int = 0

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ROW_FIELDS}

    def js_row(self, as_of: dt.date) -> list:
        posted = dt.date.fromisoformat(self.posted_date)
        return [self.company, self.job_id, self.title, self.experience_text, label_for(posted),
                max(0, (as_of - posted).days), self.location, self.canonical_url, self.experience_band,
                self.closing_date]


@dataclasses.dataclass
class CompanyState:
    status: str
    detail: str
    last_success: str | None = None
    last_attempt: str | None = None
    stale_since: str | None = None
    listing_total: int | None = None
    last_discovery_attempt: str | None = None
    screened: dict = dataclasses.field(default_factory=dict)
    # A large removal seen once by a complete healthy run: {"at": ISO time, "ids": [sorted IDs]}.
    removal_hold: dict | None = None

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in COMPANY_FIELDS}
        d["screened"] = dict(sorted(self.screened.items()))
        return d


def screened_entry(day: dt.date, fingerprint: str | None = None) -> str:
    return day.isoformat() + (f"#{fingerprint}" if fingerprint else "")


def screened_parts(value: object) -> tuple[str, str | None] | None:
    """(ISO day, fingerprint or None) for a valid screened value, else None."""
    m = SCREENED_VALUE.fullmatch(value) if isinstance(value, str) else None
    return (m.group(1), m.group(2)) if m and iso_date(m.group(1)) else None


def valid_hold(hold: object, company: str) -> bool:
    return (isinstance(hold, dict) and set(hold) == {"at", "ids"} and iso_time(hold["at"]) is not None
            and isinstance(hold["ids"], list) and 0 < len(hold["ids"]) <= MAX_HOLD_IDS
            and hold["ids"] == sorted(set(hold["ids"]))
            and all(isinstance(i, str) and JOB_ID_RE[company].fullmatch(i) for i in hold["ids"]))


@dataclasses.dataclass
class State:
    as_of: dt.date
    checked_at: str | None
    companies: dict
    empty_companies: list
    jobs: list

    def to_json(self) -> str:
        data = {
            "schema": 1,
            "as_of": self.as_of.isoformat(),
            "checked_at": self.checked_at,
            "companies": {c: s.to_dict() for c, s in self.companies.items()},
            "empty_companies": [list(e) for e in self.empty_companies],
            "jobs": [r.to_dict() for r in self.jobs],
        }
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def validate_row(row: Row, config: Config) -> None:
    c = row.company
    if c not in config.companies:
        raise ValueError(f"row {row.job_id!r}: company not configured")
    if not JOB_ID_RE[c].fullmatch(row.job_id):
        raise ValueError(f"row {row.job_id!r}: invalid {c} job ID")
    m = COMPANY_SPECS[c].url_re.fullmatch(row.canonical_url)
    if not m or m.group(1) != row.job_id:
        raise ValueError(f"row {row.job_id}: non-canonical URL")
    if check_url(row.canonical_url, config.link_hosts):
        raise ValueError(f"row {row.job_id}: URL host not allowlisted")
    if row.experience_band not in BANDS or row.liveness not in LIVENESS:
        raise ValueError(f"row {row.job_id}: invalid band or liveness")
    if not isinstance(row.absent_runs, int) or isinstance(row.absent_runs, bool) or \
            not 0 <= row.absent_runs < ABSENCES_TO_REMOVE:
        raise ValueError(f"row {row.job_id}: invalid absent_runs")
    for d in (row.posted_date, row.first_seen, row.last_seen):
        if iso_date(d) is None:
            raise ValueError(f"row {row.job_id}: invalid date")
    if row.closing_date is not None and iso_date(row.closing_date) is None:
        raise ValueError(f"row {row.job_id}: invalid closing date")
    for text, limit in ((row.title, 160), (row.experience_text, 300), (row.location, 120)):
        if not isinstance(text, str) or not text or len(text) > limit or CONTROL_CHARS.search(text):
            raise ValueError(f"row {row.job_id}: invalid text field")
        if find_forbidden(text):
            raise ValueError(f"row {row.job_id}: forbidden content")


def load_state(data: dict, config: Config) -> State:
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("state: schema must be 1")
    if set(data) != {"schema", "as_of", "checked_at", "companies", "empty_companies", "jobs"}:
        raise ValueError("state: unexpected keys")
    as_of = iso_date(data["as_of"])
    if as_of is None:
        raise ValueError("state: invalid as_of")
    if data["checked_at"] is not None and iso_time(data["checked_at"]) is None:
        raise ValueError("state: invalid checked_at")
    comps = data["companies"]
    if not isinstance(comps, dict) or list(comps) != list(config.companies):
        raise ValueError("state: companies must match config order")
    companies: dict[str, CompanyState] = {}
    for name, raw in comps.items():
        if not isinstance(raw, dict) or tuple(raw) not in (COMPANY_FIELDS, LEGACY_COMPANY_FIELDS):
            raise ValueError(f"state: {name} fields")
        cs = CompanyState(**raw)
        if cs.removal_hold is not None and not valid_hold(cs.removal_hold, name):
            raise ValueError(f"state: {name} removal_hold")
        if cs.status not in COMPANY_STATUSES or not isinstance(cs.detail, str) or len(cs.detail) > 120:
            raise ValueError(f"state: {name} status")
        if (cs.status == "static") != (config.sources[name]["mode"] == "static"):
            raise ValueError(f"state: {name} static status must match its mode")
        for t in (cs.last_success, cs.last_attempt, cs.stale_since, cs.last_discovery_attempt):
            if t is not None and iso_time(t) is None:
                raise ValueError(f"state: {name} timestamp")
        if cs.listing_total is not None and (not isinstance(cs.listing_total, int) or cs.listing_total < 0):
            raise ValueError(f"state: {name} listing_total")
        if not isinstance(cs.screened, dict) or not all(
                isinstance(k, str) and JOB_ID_RE[name].fullmatch(k) and screened_parts(v)
                for k, v in cs.screened.items()):
            raise ValueError(f"state: {name} screened")
        if find_forbidden(cs.detail):
            raise ValueError(f"state: {name} detail")
        companies[name] = cs
    empty = data["empty_companies"]
    if not isinstance(empty, list) or not all(
            isinstance(e, list) and len(e) == 2 and all(isinstance(x, str) and 0 < len(x) <= 160 for x in e)
            and not find_forbidden(" ".join(e)) for e in empty):
        raise ValueError("state: empty_companies")
    if not isinstance(data["jobs"], list):
        raise ValueError("state: jobs")
    jobs = []
    for raw in data["jobs"]:
        if not isinstance(raw, dict) or tuple(raw) != ROW_FIELDS:
            raise ValueError("state: job fields")
        row = Row(**raw)
        validate_row(row, config)
        jobs.append(row)
    keys = [(r.company, r.job_id) for r in jobs]
    if len(set(keys)) != len(keys):
        raise ValueError("state: duplicate rows")
    return State(as_of, data["checked_at"], companies, [tuple(e) for e in empty], jobs)


def sort_rows(rows: Iterable[Row], config: Config) -> list[Row]:
    """Config company order, newest posted first; ties keep their existing (stable) order."""
    return sorted(rows, key=lambda r: (config.companies.index(r.company),
                                       -dt.date.fromisoformat(r.posted_date).toordinal()))


# --------------------------------------------------------------------------- render

def stale_labels(state: State, config: Config) -> dict[str, str]:
    out = {}
    for c in config.companies:
        cs = state.companies[c]
        if cs.status == "static":
            out[c] = "not auto-checked"
        elif cs.status == "stale":
            last = iso_time(cs.last_success)
            out[c] = f"stale since {label_for(last.astimezone(IST).date())}" if last else "not yet verified"
    return out


def checked_text(state: State) -> str:
    when = iso_time(state.checked_at)
    if not when:
        return "Auto-check pending"
    when = when.astimezone(IST)
    return f"Checked {when.day} {MON[when.month - 1]} {when:%H:%M} IST"


def render(state: State, template: str, config: Config) -> str:
    for r in state.jobs:
        validate_row(r, config)
    rows = [r.js_row(state.as_of) for r in state.jobs]
    visible = [r for r in rows if r[5] <= config.max_age_days]
    companies = len({r[0] for r in visible})
    day = long_date(state.as_of)
    values = {
        "DAY": html.escape(day),
        "DAY_UPPER": html.escape(day.upper()),
        "CHECKED": html.escape(checked_text(state)),
        "TOTAL": str(len(visible)),
        "COMPANIES_MATCHED": str(companies),
        "TODAY_COUNT": str(sum(1 for r in visible if r[5] == 0)),
        "VISIBLE_ROWS": f"{len(visible)} row{'' if len(visible) == 1 else 's'}",
        "JOBS_JS": "[\n" + ",\n".join(js_json(r) for r in rows) + "\n]",
        "EMPTY_JS": "[\n" + ",\n".join(js_json(list(e)) for e in state.empty_companies) + "]",
        "ORDER_JS": js_json(list(config.companies)),
        "STALE_JS": js_json(stale_labels(state, config)),
        "AS_OF_JS": js_json(state.as_of.isoformat()),
    }
    used: set[str] = set()

    def sub(m: re.Match) -> str:
        if m.group(1) not in values:
            raise ValueError(f"template: unknown placeholder {m.group(1)}")
        used.add(m.group(1))
        return values[m.group(1)]

    doc = PLACEHOLDER.sub(sub, template)   # single pass: substituted values are never re-scanned
    if used != set(values):
        raise ValueError("template: missing placeholders " + ", ".join(sorted(set(values) - used)))
    if doc.lower().count("</script") != template.lower().count("</script"):
        raise ValueError("render: script terminator count changed")
    hits = find_forbidden(doc)
    if hits:
        raise ValueError("render: forbidden content: " + ", ".join(hits))
    return doc


# --------------------------------------------------------------------------- HTTP

def check_url(url: str, allowed_hosts: Iterable[str]) -> str | None:
    """None when the URL may be used, otherwise a short reason code."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return "bad-url"
    if parts.scheme != "https":
        return "scheme-not-allowed"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "userinfo-not-allowed"
    if port not in (None, 443):
        return "port-not-allowed"
    if (parts.hostname or "").lower() not in set(allowed_hosts):
        return "host-not-allowed"
    if any(ord(ch) < 33 for ch in url):
        return "bad-url"
    return None


@dataclasses.dataclass
class Resp:
    status: int | None
    url: str
    body: bytes = b""
    ctype: str = ""
    error: str | None = None
    headers: list = dataclasses.field(default_factory=list, repr=False)

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json_obj(self) -> object | None:
        if self.error or self.status != 200 or "json" not in self.ctype.lower():
            return None
        try:
            return json.loads(self.body)
        except ValueError:
            return None


Transport = Callable[[str, str, dict, "bytes | None", float], "tuple[int, list, bytes]"]


def https_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    """One HTTPS exchange with certificate verification and no automatic redirects."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPSConnection(parts.hostname, 443, timeout=timeout,
                                       context=ssl.create_default_context())
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        deadline = time.monotonic() + REQUEST_WALL_S
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        chunks, size = [], 0
        while size <= MAX_BYTES:
            if time.monotonic() > deadline:
                raise TimeoutError("response exceeded wall-clock limit")
            chunk = r.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return r.status, r.getheaders(), b"".join(chunks)
    finally:
        conn.close()


def _header(headers: list, name: str) -> str | None:
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


def _decode(raw: bytes, encoding: str | None) -> bytes | None:
    if len(raw) > MAX_BYTES:
        return None
    enc = (encoding or "").lower().strip()
    if enc in ("", "identity"):
        return raw
    if enc not in ("gzip", "deflate"):
        return None
    wbits = 16 + zlib.MAX_WBITS if enc == "gzip" else zlib.MAX_WBITS
    try:
        d = zlib.decompressobj(wbits)
        out = d.decompress(raw, MAX_BYTES + 1)
    except zlib.error:
        return None
    return None if len(out) > MAX_BYTES or d.unconsumed_tail or not d.eof else out


def retry_after_s(value: str | None, now: dt.datetime | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    now = now or dt.datetime.now(dt.timezone.utc)
    return max(0.0, (when - now).total_seconds())


class RobotsRules:
    """RFC 9309 robots.txt matching: our group (else '*'), longest match wins, Allow wins ties."""

    def __init__(self, text: str, token: str):
        groups: list[tuple[set, list]] = []
        agents: set | None = None
        rules: list | None = None
        in_agents = False
        for raw in text.splitlines()[:5000]:
            line = raw.split("#", 1)[0].strip()
            key, sep, val = line.partition(":")
            if not sep:
                continue
            key, val = key.strip().lower(), val.strip()
            if key == "user-agent":
                if not in_agents:
                    agents, rules = set(), []
                    groups.append((agents, rules))
                agents.add(val.lower())
                in_agents = True
                continue
            in_agents = False
            if key in ("allow", "disallow") and rules is not None and val:
                rules.append((key == "allow", val))
        mine = [r for a, r in groups if token.lower() in a] or [r for a, r in groups if "*" in a]
        self.rules = [rule for group in mine for rule in group]

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        return re.match(regex + ("$" if anchored else ""), path) is not None

    def allows(self, path: str) -> bool:
        if path == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None
        for allow, pattern in self.rules:
            if self._matches(pattern, path):
                key = (len(pattern), allow)
                if best is None or key > best:
                    best = key
        return True if best is None else best[1]


class Fetcher:
    """Allowlisted, sequential HTTPS client with polite delays, bounded retries, and robots.txt."""

    def __init__(self, config: Config, budget_s: float, *, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 rand: Callable[[], float] = random.random, cookies: bool = False):
        self._hosts = {h: p for h, p in config.hosts.items() if p.fetch}
        self._transport = transport or https_transport
        self._sleep, self._clock, self._rand = sleep, clock, rand
        self._deadline = clock() + budget_s
        self._next_ok: dict[str, float] = {}
        self._robots: dict[str, object] = {}
        self._cookies: dict[str, dict[str, str]] = {} if cookies else None  # type: ignore[assignment]
        self.requests = 0

    # -- helpers
    def _wait_turn(self, host: str) -> None:
        delay = self._next_ok.get(host, 0.0) - self._clock()
        if delay > 0:
            self._sleep(delay)

    def _done(self, host: str) -> None:
        base = self._hosts[host].min_delay_s
        self._next_ok[host] = self._clock() + base + self._rand() * base * 0.25

    def _store_cookies(self, host: str, headers: list) -> None:
        if self._cookies is None:
            return
        jar = self._cookies.setdefault(host, {})
        for k, v in headers:
            if k.lower() == "set-cookie":
                pair = v.split(";", 1)[0].strip()
                name, sep, val = pair.partition("=")
                if sep and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) and len(val) <= 4096:
                    jar[name] = val

    def _exchange(self, method: str, url: str, headers: dict, body: bytes | None) -> Resp:
        """One logical request with up to MAX_ATTEMPTS tries on transient failures."""
        host = urllib.parse.urlsplit(url).hostname.lower()
        hdrs = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip", **headers}
        jar = (self._cookies or {}).get(host)
        if jar:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in sorted(jar.items()))
        last = Resp(None, url, error="network")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._clock() > self._deadline:
                return Resp(None, url, error="deadline")
            self._wait_turn(host)
            self.requests += 1
            try:
                status, rh, raw = self._transport(method, url, hdrs,
                                                  body, REQUEST_TIMEOUT_S)
            except (socket.timeout, TimeoutError):
                last = Resp(None, url, error="timeout")
                status = None
            except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
                last = Resp(None, url, error="network")
                status = None
            finally:
                self._done(host)
            if status is not None:
                self._store_cookies(host, rh)
                decoded = _decode(raw, _header(rh, "content-encoding"))
                if decoded is None:
                    return Resp(status, url, error="too-large-or-bad-encoding")
                last = Resp(status, url, decoded, _header(rh, "content-type") or "", headers=list(rh))
                if status not in RETRY_STATUSES:
                    return last
                wait = retry_after_s(_header(rh, "retry-after"))
                if wait is not None and wait > MAX_RETRY_AFTER_S:
                    return last
            else:
                wait = None
            if attempt == MAX_ATTEMPTS:
                break
            backoff = wait if wait is not None else min(30.0, 2.0 ** attempt)
            self._sleep(backoff + self._rand())
        return last

    def _robots_allows(self, url: str) -> bool:
        host = urllib.parse.urlsplit(url).hostname.lower()
        if host not in self._robots:
            r = self.request(f"https://{host}/robots.txt", accept="text", _robots=False)
            if r.status == 200 and not r.error and len(r.body) <= MAX_ROBOTS_BYTES:
                # An HTML/challenge page served as robots.txt is not a rule set: do not crawl.
                looks_html = "html" in r.ctype.lower() or is_challenge(r)
                self._robots[host] = "deny" if looks_html else RobotsRules(r.text(), ROBOTS_TOKEN)
            elif r.status is not None and 400 <= r.status < 500 and r.status != 429:
                self._robots[host] = "allow"      # RFC 9309: unavailable robots.txt means no rules
            else:
                self._robots[host] = "deny"       # unreachable: assume complete disallow
        rule = self._robots[host]
        if rule == "allow":
            return True
        if rule == "deny":
            return False
        parts = urllib.parse.urlsplit(url)
        return rule.allows((parts.path or "/") + ("?" + parts.query if parts.query else ""))  # type: ignore[union-attr]

    # -- public
    def request(self, url: str, method: str = "GET", *, body: bytes | None = None,
                headers: dict | None = None, accept: str = "html", _robots: bool = True) -> Resp:
        current = url
        hdrs = {"Accept": {"html": HTML_ACCEPT, "json": JSON_ACCEPT}.get(accept, "text/plain"), **(headers or {})}
        for _ in range(MAX_REDIRECTS + 1):
            reason = check_url(current, self._hosts)
            if reason:
                return Resp(None, current, error=reason)
            if self._clock() > self._deadline:
                return Resp(None, current, error="deadline")
            host = urllib.parse.urlsplit(current).hostname.lower()
            if _robots and self._hosts[host].robots and not self._robots_allows(current):
                return Resp(None, current, error="robots")
            resp = self._exchange(method, current, hdrs, body)
            location = _header(resp.headers, "location")
            if resp.status in REDIRECT_STATUSES and location:
                nxt = urllib.parse.urljoin(current, location)
                if check_url(nxt, self._hosts) or \
                        urllib.parse.urlsplit(nxt).hostname.lower() != host:
                    return Resp(resp.status, current, error="redirect-blocked")
                current = nxt
                if resp.status in (301, 302, 303):
                    method, body = "GET", None
                    hdrs.pop("Content-Type", None)
                continue
            return resp
        return Resp(None, current, error="too-many-redirects")


# --------------------------------------------------------------------------- classification

BLOCKING_ERRORS = frozenset({"timeout", "network", "deadline", "robots"})


def is_challenge(resp: Resp) -> bool:
    low = resp.text()[:20000].lower()
    m = re.search(r"<title[^>]*>(.*?)</title>", low, re.S)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    return any(t in title for t in CHALLENGE_TITLES) or any(k in low for k in CHALLENGE_MARKERS)


def is_maintenance(resp: Resp) -> bool:
    low = resp.text()[:20000].lower()
    return any(p in low for p in MAINTENANCE_PHRASES)


def html_ok(resp: Resp) -> bool:
    return (resp.status == 200 and not resp.error and "html" in resp.ctype.lower()
            and not is_challenge(resp) and not is_maintenance(resp))


def classify_page(resp: Resp, job_id: str) -> str:
    """'live', 'closed', 'blocked' (outage/throttle/challenge) or 'unknown' (ambiguous)."""
    if resp.error in BLOCKING_ERRORS:
        return "blocked"
    if resp.error:
        return "unknown"
    if resp.status == 410:
        return "closed"
    if resp.status in (403, 429) or (resp.status or 0) >= 500:
        return "blocked"
    if resp.status != 200:
        return "unknown"            # includes 404: could be a moved URL, never a removal on its own
    if is_challenge(resp) or is_maintenance(resp):
        return "blocked"
    raw = resp.text()
    text = re.sub(r"\s+", " ", strip_html(raw)).lower()
    if any(p in text for p in CLOSED_PHRASES):
        return "closed"
    if job_id.lower() in raw.lower():
        return "live"
    return "unknown"


def check_pages(items: list[tuple[str, str]], fetcher: Fetcher) -> dict[str, str]:
    """Sequential page checks; stop probing after BLOCK_TRIP consecutive blocks."""
    out: dict[str, str] = {}
    streak = 0
    for jid, url in items:
        if streak >= BLOCK_TRIP:
            out[jid] = "skipped"
            continue
        state = classify_page(fetcher.request(url), jid)
        streak = streak + 1 if state == "blocked" else (0 if state in ("live", "closed") else streak)
        out[jid] = state
    return out


# --------------------------------------------------------------------------- experience filter

def _to_num(token: str) -> float:
    token = token.lower()
    return float(_WORD_NUM[token]) if token in _WORD_NUM else float(token)


def assess_experience(text: str, max_years: float) -> tuple[bool, str, str, float | None]:
    """(qualifies, band, summary, max_years_found). Years count only near an experience cue."""
    flat = re.sub(r"\s+", " ", text or "")
    mins: list[float] = []
    uppers: list[float] = []
    snippets: list[str] = []
    for m in YEARS_RE.finditer(flat):
        window = flat[max(0, m.start() - 60): m.end() + 60]
        if not EXPERIENCE_CUE.search(window):
            continue
        lo = _to_num(m.group(1))
        hi = _to_num(m.group(2)) if m.group(2) else lo
        mins.append(lo)
        uppers.append(max(lo, hi))
        clause = clean_text(re.split(r"[.;\n•]", flat[m.start():m.start() + 200])[0].strip(), 140)
        if clause and clause not in snippets:
            snippets.append(clause)
    if not mins:
        if re.search(r"\bexperience\b", flat, re.I):
            return True, "n", "No number of years stated — see posting for required experience", None
        return True, "n", "Not stated", None
    band = "1" if max(uppers) <= 1 else "2"
    summary = "; ".join(snippets[:2])
    summary = summary[:1].upper() + summary[1:]
    return max(mins) <= max_years, band, clean_text(summary, 200), max(uppers)


def qualifies(title: str, description: str, posted: dt.date | None, places: Iterable[str],
              today: dt.date, config: Config) -> tuple[bool, str, str]:
    if posted is None or not 0 <= (today - posted).days <= config.max_age_days:
        return False, "", ""
    if not any(city in p.lower() for p in places for city in config.cities):
        return False, "", ""
    ok, band, summary, found = assess_experience(description, config.max_years)
    if not ok:
        return False, "", ""
    if SENIOR_TITLE.search(title) and (found is None or found > config.max_years):
        return False, "", ""
    return True, band, summary


def make_row(company: str, jid: str, title: str, exp: str, location: str, url: str, band: str,
             closing: str | None, posted: dt.date, today: dt.date, config: Config) -> Row | None:
    row = Row(company=company, job_id=jid, title=clean_text(title, 160), experience_text=clean_text(exp, 300),
              experience_band=band, location=clean_text(location, 120) or "Not stated", canonical_url=url,
              posted_date=posted.isoformat(), closing_date=closing, first_seen=today.isoformat(),
              last_seen=today.isoformat())
    try:
        validate_row(row, config)
    except ValueError:
        return None
    return row


def target_places(places: Iterable[str], config: Config) -> list[str]:
    hits = [p for p in places if any(c in p.lower() for c in config.cities)]
    rest = [p for p in places if p not in hits]
    return [p.split(",")[0].strip() for p in hits + rest]


# --------------------------------------------------------------------------- sources

@dataclasses.dataclass
class SourceResult:
    company: str
    ok: bool
    detail: str
    live: set = dataclasses.field(default_factory=set)
    closed: set = dataclasses.field(default_factory=set)
    closing: dict = dataclasses.field(default_factory=dict)
    listing_complete: bool = False
    listed: set = dataclasses.field(default_factory=set)
    absence_scope: set | None = None     # IDs the complete listing must contain; None = every row
    listing_total: int | None = None
    added: list = dataclasses.field(default_factory=list)
    discovery_attempted: bool = False
    screened: dict = dataclasses.field(default_factory=dict)
    counts: dict = dataclasses.field(default_factory=dict)


def held(company: str, detail: str, **kw) -> SourceResult:
    return SourceResult(company, False, detail, **kw)


@dataclasses.dataclass
class SiemensDetail:
    state: str
    posted: dt.date | None = None
    places: tuple = ()
    description: str = ""


def siemens_detail(fetcher: Fetcher, jid: str, cache: dict) -> SiemensDetail:
    if jid in cache:
        return cache[jid]
    resp = fetcher.request(SIEMENS_DETAIL.format(id=jid))
    state = classify_page(resp, jid)
    info = SiemensDetail(state)
    if state == "live":
        raw = resp.text()
        fields: dict[str, str] = {}
        for m in SIEMENS_FIELD.finditer(raw):
            label = clean_text(strip_html(m.group(1)), 60)
            if label and label not in fields:
                fields[label] = clean_text(strip_html(m.group(2)), 400)
        posted = parse_any_date(fields.get("Posted since", ""))
        places = tuple(p.strip() for p in re.split(r"[;|]", fields.get("Location(s)", "")) if p.strip())
        start = raw.find("article__content__view__field")
        body = strip_html(raw[start:] if start >= 0 else raw)[:40000]
        info = SiemensDetail(state, posted, places, body)
    cache[jid] = info
    return info


@dataclasses.dataclass
class Discovery:
    cards: list            # [(id, title)] in listing order
    authoritative: bool
    detail: str


def siemens_discover(cfg: dict, fetcher: Fetcher, today: dt.date, config: Config, cache: dict) -> Discovery:
    search = urllib.parse.quote(cfg["search_text"])
    cutoff = today - dt.timedelta(days=config.max_age_days)
    boot = fetcher.request(SIEMENS_LIST.format(offset=0, search=search))   # session cookie
    if not html_ok(boot):
        return Discovery([], False, "listing unavailable")
    sort = fetcher.request(SIEMENS_SEARCH, "POST", body=SIEMENS_SORT_BODY.format(search=search).encode(),
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
    if not html_ok(sort):
        return Discovery([], False, "sort rejected")
    cards: list[tuple[str, str]] = []
    seen: set[str] = set()
    last_probe: dt.date | None = None
    for page in range(cfg["max_list_pages"]):
        resp = fetcher.request(SIEMENS_LIST.format(offset=page * SIEMENS_PAGE_SIZE, search=search))
        if not html_ok(resp):
            return Discovery(cards, False, f"page {page} unavailable")
        found = [(m.group(1), clean_text(strip_html(m.group(2)), 160)) for m in SIEMENS_CARD.finditer(resp.text())]
        if not found:
            return Discovery(cards, False, "empty first page" if page == 0 else "list ended before window")
        for jid, title in found:
            if jid not in seen:
                seen.add(jid)
                cards.append((jid, title))
        last_page = len(found) < SIEMENS_PAGE_SIZE
        if page % cfg["probe_every_pages"] == 0 or last_page:
            probe = siemens_detail(fetcher, found[-1][0], cache)
            if probe.posted is None:
                return Discovery(cards, False, "probe date unavailable")
            if last_probe and probe.posted > last_probe:
                return Discovery(cards, False, "listing not date-ordered")
            last_probe = probe.posted
            if probe.posted < cutoff:
                return Discovery(cards, True, f"window complete ({page + 1} pages)")
        if last_page:
            return Discovery(cards, False, "list ended before window")
    return Discovery(cards, False, "page cap reached")


def refresh_siemens(cfg: dict, rows: list[Row], fetcher: Fetcher, now: dt.datetime, cs: CompanyState,
                    excluded: frozenset, config: Config) -> SourceResult:
    today = now.astimezone(IST).date()
    cache: dict[str, SiemensDetail] = {}
    states: dict[str, str] = {}
    streak = 0
    for r in sorted(rows, key=lambda r: r.job_id, reverse=True):
        if streak >= BLOCK_TRIP:
            states[r.job_id] = "skipped"
            continue
        st = siemens_detail(fetcher, r.job_id, cache).state
        streak = streak + 1 if st == "blocked" else (0 if st in ("live", "closed") else streak)
        states[r.job_id] = st
    res = SourceResult("Siemens", True, "")
    res.live = {k for k, v in states.items() if v == "live"}
    res.closed = {k for k, v in states.items() if v == "closed"}
    notes = [f"liveness {len(res.live) + len(res.closed)}/{len(rows)}"]

    last = iso_time(cs.last_discovery_attempt)
    if last is None or now - last >= dt.timedelta(hours=cfg["discovery_interval_h"]):
        res.discovery_attempted = True
        disc = siemens_discover(cfg, fetcher, today, config, cache)
        notes.append("discovery " + disc.detail)
        existing = {r.job_id for r in rows}
        res.listed = {jid for jid, _ in disc.cards}
        res.listing_complete = disc.authoritative
        cutoff = today - dt.timedelta(days=config.max_age_days)
        term = cfg["search_text"].lower()
        res.absence_scope = {r.job_id for r in rows
                             if dt.date.fromisoformat(r.posted_date) > cutoff and term in r.location.lower()}
        res.live |= res.listed & existing
        res.closed -= res.live
        fresh = [(j, t) for j, t in disc.cards
                 if j not in existing and j not in excluded and j not in cs.screened]
        for jid, title in fresh[:cfg["max_new_details"]]:
            info = siemens_detail(fetcher, jid, cache)
            if info.state != "live" or info.posted is None:
                continue                      # ambiguous: retry on a later discovery
            ok, band, exp = qualifies(title, info.description, info.posted, info.places, today, config)
            row = make_row("Siemens", jid, title, exp, join_places(target_places(info.places, config)),
                           SIEMENS_DETAIL.format(id=jid), band, None, info.posted, today, config) if ok else None
            if row:
                res.added.append(row)
            else:
                res.screened[jid] = today.isoformat()
        res.counts["listed"] = len(res.listed)
        res.counts["new_checked"] = min(len(fresh), cfg["max_new_details"])
    res.counts.update({k: sum(1 for v in states.values() if v == k) for k in set(states.values())})
    if rows and not (res.live or res.closed):
        res.ok = False
        notes[0] = "detail pages unavailable"
    res.detail = "; ".join(notes)
    return res


def _wd_page(fetcher: Fetcher, term: str, offset: int) -> dict | None:
    body = json.dumps({"appliedFacets": {}, "limit": WD_PAGE, "offset": offset, "searchText": term}).encode()
    data = fetcher.request(IQVIA_API + "/jobs", "POST", body=body,
                           headers={"Content-Type": "application/json"}, accept="json").json_obj()
    if not isinstance(data, dict) or not isinstance(data.get("jobPostings"), list):
        return None
    return data


def _wd_search(fetcher: Fetcher, term: str, max_total: int) -> tuple[list, int] | None:
    """All postings for a search term, or None when the listing is incomplete or malformed."""
    first = _wd_page(fetcher, term, 0)
    if first is None:
        return None
    total = first.get("total")     # reliable on the first page only
    if not isinstance(total, int) or isinstance(total, bool) or not 0 <= total <= max_total:
        return None
    posts = list(first["jobPostings"])
    offset = WD_PAGE
    while offset < total:
        page = _wd_page(fetcher, term, offset)
        if page is None or not page["jobPostings"]:
            return None             # a throttled page looks like end-of-list: treat as partial
        posts += page["jobPostings"]
        offset += WD_PAGE
    # Unique postings: results shifting mid-pagination can duplicate one job and skip another.
    if len({str(p.get("externalPath")) for p in posts if isinstance(p, dict)}) < total:
        return None
    return posts, total


def _wd_id(posting: object) -> tuple[str, str] | None:
    path = posting.get("externalPath") if isinstance(posting, dict) else None
    m = WD_PATH.fullmatch(path) if isinstance(path, str) else None
    return (m.group(1), path) if m else None


def _wd_fingerprint(posting: dict) -> str:
    """Stable digest of the listing fields that identify the role (not the daily "Posted N days ago")."""
    key = [str(posting.get(k) or "") for k in ("title", "externalPath", "locationsText")]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _wd_detail(fetcher: Fetcher, path: str, jid: str) -> tuple[str, dict | None]:
    resp = fetcher.request(IQVIA_API + path, accept="json")
    if resp.status == 410:
        return "closed", None
    if resp.error in BLOCKING_ERRORS or resp.status in (403, 429) or (resp.status or 0) >= 500:
        return "blocked", None
    data = resp.json_obj()
    info = data.get("jobPostingInfo") if isinstance(data, dict) else None
    if not isinstance(info, dict) or info.get("jobReqId") != jid:
        return "unknown", None
    if info.get("posted") is False or info.get("canApply") is False:
        return "closed", info
    return "live", info


def refresh_iqvia(cfg: dict, rows: list[Row], fetcher: Fetcher, now: dt.datetime, cs: CompanyState,
                  excluded: frozenset, config: Config) -> SourceResult:
    today = now.astimezone(IST).date()
    listing: dict[str, dict] = {}
    paths: dict[str, str] = {}
    total_sum = 0
    for term in cfg["search_terms"]:
        found = _wd_search(fetcher, term, cfg["max_total"])
        if found is None:
            return held("IQVIA", "workday listing incomplete or unavailable")
        posts, total = found
        total_sum += total
        for p in posts:
            ident = _wd_id(p)
            if ident:
                listing.setdefault(ident[0], p)
                paths.setdefault(*ident)
    existing = {r.job_id for r in rows}
    for jid in sorted(existing - set(paths)):
        page = _wd_page(fetcher, jid, 0)
        if page is None:
            return held("IQVIA", "workday ID search unavailable")
        for p in page["jobPostings"]:
            ident = _wd_id(p)
            if ident and ident[0] == jid:
                paths[jid] = ident[1]
                break
    res = SourceResult("IQVIA", True, "", listing_complete=True, listing_total=total_sum)
    res.listed = set(paths)
    for r in rows:
        if r.job_id in paths:
            res.live.add(r.job_id)            # present in an authoritative listing
            st, info = _wd_detail(fetcher, paths[r.job_id], r.job_id)
            if st == "closed":
                res.closed.add(r.job_id)
                res.live.discard(r.job_id)
            elif st == "live" and info is not None:
                end = workday_closing(info.get("endDate"))
                if end and end != r.closing_date:
                    res.closing[r.job_id] = end
        elif classify_page(fetcher.request(IQVIA_FRONT.format(id=r.job_id)), r.job_id) == "closed":
            res.closed.add(r.job_id)
    # Unscreened (or changed) candidates, newest ID first. Every definitive outcome is recorded in
    # res.screened, so non-qualifying top results cannot starve older candidates: each run spends at
    # most max_new_details detail requests (plus one page check per qualifying role) on the next batch.
    fresh: list[tuple[str, str]] = []
    for jid in sorted(listing, reverse=True):
        if jid in existing or jid in excluded or "30+" in str(listing[jid].get("postedOn", "")):
            continue
        fp = _wd_fingerprint(listing[jid])
        prior = screened_parts(cs.screened.get(jid))
        if prior and prior[1] in (None, fp):
            continue                          # screened before and the listing record is unchanged
        fresh.append((jid, fp))
    checked = 0
    for jid, fp in fresh[:cfg["max_new_details"]]:
        checked += 1
        mark = screened_entry(today, fp)
        st, info = _wd_detail(fetcher, paths[jid], jid)
        if st == "blocked":
            break                             # throttled: leave the rest for a later run
        if st == "closed":
            res.screened[jid] = mark
            continue
        if st != "live" or info is None:
            continue                          # unreadable (maybe a transient error page): retry later
        places = [str(info.get("location") or "")] + [str(x) for x in info.get("additionalLocations") or []]
        title = clean_text(info.get("title"), 160)
        ok, band, exp = qualifies(title, strip_html(str(info.get("jobDescription") or "")),
                                  parse_any_date(info.get("startDate")), places, today, config)
        row = make_row("IQVIA", jid, title, exp, join_places(target_places(
            [p for p in places if any(c in p.lower() for c in config.cities)], config)),
            IQVIA_FRONT.format(id=jid), band, workday_closing(info.get("endDate")),
            parse_any_date(info.get("startDate")), today, config) if ok else None
        if row is None:
            res.screened[jid] = mark          # does not qualify (or cannot form a valid row)
            continue
        page = classify_page(fetcher.request(row.canonical_url), jid)
        if page == "live":
            res.added.append(row)
        elif page == "closed":
            res.screened[jid] = mark
        elif page == "blocked":
            break
        # "unknown": ambiguous public page; retried on a later run
    res.counts = {"listed": len(listing), "live": len(res.live), "closed": len(res.closed),
                  "new_checked": checked, "unscreened": len(fresh)}
    res.detail = f"listing {total_sum} across {len(cfg['search_terms'])} searches"
    return res


def _ge_cf(job: dict, key: str) -> str | None:
    for f in job.get("customFields") or []:
        if isinstance(f, dict) and f.get("cfKey") == key and isinstance(f.get("value"), str):
            return f["value"]
    return None


def _ge_listing(cfg: dict, fetcher: Fetcher) -> tuple[dict, int] | str:
    boot = fetcher.request(GE_BOOT)
    if not html_ok(boot):
        return "boot page unavailable"
    headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest",
               "Referer": GE_BOOT, "Origin": GE_BASE}
    body = json.dumps(GE_LIST_BODY).encode()
    city = urllib.parse.quote(cfg["city"])
    jobs: dict[str, dict] = {}
    unique: set[str] = set()
    received, total, page = 0, None, 1
    max_pages = cfg["max_total"] // GE_PAGE + 1
    while page <= max_pages:
        data = fetcher.request(GE_LIST.format(city=city, page=page), "POST", body=body,
                               headers=headers, accept="json").json_obj()
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            return "listing unavailable or malformed"
        if total is None:
            total = data.get("totalJob")
            if not isinstance(total, int) or isinstance(total, bool) or not 0 <= total <= cfg["max_total"]:
                return "listing total invalid"
        if not data["jobs"] and received < total:
            return "partial pagination"
        for j in data["jobs"]:
            rid = j.get("requisitionID") if isinstance(j, dict) else None
            unique.add(str(rid))
            if isinstance(rid, str) and JOB_ID_RE["GE Vernova"].fullmatch(rid):
                jobs.setdefault(rid, j)
        received += len(data["jobs"])
        if page * GE_PAGE >= total:
            break
        page += 1
    else:
        return "page cap reached"
    if len(unique) < (total or 0):     # unique IDs, so a duplicated job cannot mask a skipped one
        return "partial pagination"
    return jobs, total or 0


def refresh_ge(cfg: dict, rows: list[Row], fetcher: Fetcher, now: dt.datetime, cs: CompanyState,
               excluded: frozenset, config: Config) -> SourceResult:
    today = now.astimezone(IST).date()
    got = _ge_listing(cfg, fetcher)
    if isinstance(got, str):
        return held("GE Vernova", got)
    listing, total = got
    res = SourceResult("GE Vernova", True, f"listing {total}", listing_complete=True,
                       listing_total=total, listed=set(listing))
    existing = {r.job_id for r in rows}
    for r in rows:
        if r.job_id in listing:
            res.live.add(r.job_id)
            end = generic_closing(_ge_cf(listing[r.job_id], "cf_posting_end_date"))
            if end and end != r.closing_date:
                res.closing[r.job_id] = end
    absent = [(r.job_id, r.canonical_url) for r in rows if r.job_id not in listing]
    res.closed = {k for k, v in check_pages(absent, fetcher).items() if v == "closed"}
    candidates: list[Row] = []
    for rid in sorted(listing, reverse=True):
        raw = listing[rid]
        if rid in existing or rid in excluded:
            continue
        path = raw.get("originalURL")
        m = GE_PATH.fullmatch(path) if isinstance(path, str) else None
        if not m or m.group(1) != rid or not raw.get("description"):
            continue
        cities = [str(loc.get("city") or "") for loc in raw.get("locations") or [] if isinstance(loc, dict)]
        title = clean_text(raw.get("title"), 160)
        posted = parse_any_date(_ge_cf(raw, "cf_posting_start_date"))
        ok, band, exp = qualifies(title, strip_html(str(raw["description"])), posted, cities, today, config)
        if not ok:
            continue
        row = make_row("GE Vernova", rid, title, exp, join_places(target_places(cities, config)),
                       f"{GE_BASE}/{path}", band, generic_closing(_ge_cf(raw, "cf_posting_end_date")),
                       posted, today, config)
        if row:
            candidates.append(row)
    verify = check_pages([(c.job_id, c.canonical_url) for c in candidates[:cfg["max_new_checks"]]], fetcher)
    res.added = [c for c in candidates if verify.get(c.job_id) == "live"]
    res.counts = {"listed": len(listing), "live": len(res.live), "closed": len(res.closed),
                  "candidates": len(candidates)}
    return res


RUNNERS = {
    "siemens_avature_html": refresh_siemens,
    "iqvia_workday_cxs": refresh_iqvia,
    "gevernova_paradox_api": refresh_ge,
}


# --------------------------------------------------------------------------- merge

@dataclasses.dataclass
class Outcome:
    company: str
    result: str          # ok | held | static | offline
    detail: str
    before: int
    after: int
    added: list
    removed: list


def removal_verdict(hold: dict | None, removed: list[str], rows: list[Row], res: SourceResult,
                    now: dt.datetime) -> str:
    """Decide a large removal: 'confirmed', 'new' (start a hold), or 'wait' (keep any existing hold).

    A hold starts only from a complete healthy observation (an authoritative listing, or a definitive
    live/closed answer for every row). It is confirmed only by another complete healthy observation at
    least one cadence later that removes nothing outside the held set. Failed runs never reach here,
    and partial ones neither start nor advance a hold.
    """
    complete = res.listing_complete or {r.job_id for r in rows} <= (res.live | res.closed)
    if not complete:
        return "wait"
    held_at = iso_time(hold["at"]) if hold else None
    if held_at is None or not set(removed) <= set(hold["ids"]):
        return "new"
    return "confirmed" if now - held_at >= REFRESH_CADENCE else "wait"


def apply_result(rows: list[Row], cs: CompanyState, res: SourceResult, now: dt.datetime,
                 ) -> tuple[list[Row], CompanyState, list[str]]:
    """Merge one source result into a company's rows. Returns (rows, state, removed IDs)."""
    today = now.astimezone(IST).date().isoformat()
    when = stamp(now)
    new_cs = dataclasses.replace(cs, last_attempt=when, screened=dict(cs.screened))
    if res.discovery_attempted:
        new_cs.last_discovery_attempt = when
    if res.ok and res.listing_complete:
        # A removal-guard hold is only ever set after this run's shrink check already passed, so it
        # must not re-arm the shrink guard and bounce between the two holds.
        repeat_shrink = cs.status == "stale" and cs.detail in (SHRINK_DETAIL, REMOVAL_DETAIL)
        if res.listing_total == 0 and rows:
            res = dataclasses.replace(res, ok=False, detail="empty listing")
        elif (cs.listing_total or 0) >= SHRINK_MIN_PRIOR and res.listing_total is not None \
                and res.listing_total * 2 < cs.listing_total and not repeat_shrink:
            # Held once; a second consecutive shrink is accepted as real. Rows still need two
            # authoritative absences (and pass the removal guard) before anything is removed.
            res = dataclasses.replace(res, ok=False, detail=SHRINK_DETAIL)
    out: list[Row] = []
    removed: list[str] = []
    if res.ok:
        scope = res.absence_scope if res.absence_scope is not None else {r.job_id for r in rows}
        for r in rows:
            if r.job_id in res.closed and r.job_id not in res.live:
                removed.append(r.job_id)
                continue
            if r.job_id in res.live:
                r = dataclasses.replace(r, liveness="live", absent_runs=0, last_seen=today,
                                        closing_date=res.closing.get(r.job_id, r.closing_date))
            elif res.listing_complete and r.job_id in scope and r.job_id not in res.listed:
                if r.absent_runs + 1 >= ABSENCES_TO_REMOVE:
                    removed.append(r.job_id)
                    continue
                r = dataclasses.replace(r, liveness="absent", absent_runs=r.absent_runs + 1)
            else:
                r = dataclasses.replace(r, liveness="unconfirmed")
            out.append(r)
        if len(removed) >= REMOVAL_GUARD_MIN and len(removed) * 2 > len(rows):
            verdict = removal_verdict(cs.removal_hold, removed, rows, res, now)
            if verdict == "confirmed":
                res = dataclasses.replace(res, detail="removal confirmed; " + res.detail)
            else:
                if verdict == "new":
                    new_cs.removal_hold = {"at": when, "ids": sorted(removed)}
                res = dataclasses.replace(res, ok=False, detail=REMOVAL_DETAIL)
    if not res.ok:
        new_cs.status, new_cs.detail = "stale", clean_text(res.detail, 120)
        new_cs.stale_since = cs.stale_since if cs.status == "stale" and cs.stale_since else when
        return list(rows), new_cs, []
    seen = {r.job_id for r in out}
    for r in sorted(res.added, key=lambda r: r.job_id, reverse=True):
        if r.job_id not in seen:
            out.append(r)
            seen.add(r.job_id)
    new_cs.status, new_cs.detail = "ok", clean_text(res.detail, 120) or "ok"
    new_cs.last_success, new_cs.stale_since = when, None
    new_cs.removal_hold = None
    if res.listing_complete and res.listing_total is not None:
        new_cs.listing_total = res.listing_total
    new_cs.screened.update(res.screened)
    return out, new_cs, removed


def finalize(rows: list[Row], today: dt.date, config: Config, excluded: dict) -> tuple[list[Row], list[tuple]]:
    """Deterministic, source-independent pruning: exclusions, passed deadlines, 30-day age-out."""
    keep, dropped = [], []
    for r in rows:
        posted = dt.date.fromisoformat(r.posted_date)
        if r.job_id in excluded.get(r.company, ()):
            dropped.append((r.company, r.job_id, "excluded"))
        elif r.closing_date and dt.date.fromisoformat(r.closing_date) < today:
            dropped.append((r.company, r.job_id, "deadline passed"))
        elif (today - posted).days > config.max_age_days:
            dropped.append((r.company, r.job_id, "older than window"))
        else:
            keep.append(r)
    return sort_rows(keep, config), dropped


FetcherFactory = Callable[[str, dict], Fetcher]


def run_refresh(state: State, config: Config, excluded: dict, now: dt.datetime,
                factory: FetcherFactory | None) -> tuple[State, list[Outcome]]:
    """factory=None: offline (no network); only dates, ages and deterministic pruning change."""
    today = now.astimezone(IST).date()
    companies = {c: dataclasses.replace(s, screened=dict(s.screened)) for c, s in state.companies.items()}
    all_rows: list[Row] = []
    outcomes: list[Outcome] = []
    for company in config.companies:
        rows = [r for r in state.jobs if r.company == company]
        cfg = config.sources[company]
        cs = companies[company]
        cutoff = (today - dt.timedelta(days=SCREENED_TTL_DAYS)).isoformat()
        cs.screened = {k: v for k, v in cs.screened.items() if screened_parts(v)[0] >= cutoff}
        if cfg["mode"] == "static" or factory is None:
            kind = "static" if cfg["mode"] == "static" else "offline"
            outcomes.append(Outcome(company, kind, cs.detail, len(rows), len(rows), [], []))
            all_rows += rows
            continue
        try:
            res = RUNNERS[cfg["mode"]](cfg, rows, factory(company, cfg), now, cs,
                                       excluded.get(company, frozenset()), config)
        except Exception as exc:  # noqa: BLE001 - one source must never break the others
            res = held(company, f"source error ({type(exc).__name__})")
        new_rows, companies[company], removed = apply_result(rows, cs, res, now)
        added = [r.job_id for r in new_rows if r.job_id not in {x.job_id for x in rows}]
        outcomes.append(Outcome(company, "ok" if companies[company].status == "ok" else "held",
                                companies[company].detail, len(rows), len(new_rows), added, removed))
        all_rows += new_rows
    final, dropped = finalize(all_rows, today, config, excluded)
    for company, jid, why in dropped:
        for o in outcomes:
            if o.company == company:
                if why != "excluded":      # private exclusions are never named in reports
                    o.removed.append(f"{jid} ({why})")
                o.after -= 1
                if jid in o.added:
                    o.added.remove(jid)
    checked = state.checked_at if factory is None else stamp(now)
    return State(today, checked, companies, list(state.empty_companies), final), outcomes


def meaningful(before: State, after: State) -> bool:
    """True when data or status changed; volatile timestamps within the same IST day are ignored."""
    def norm(s: State) -> dict:
        d = json.loads(s.to_json())
        d.pop("checked_at")
        for c in d["companies"].values():
            c.pop("last_attempt")
            if c["last_success"]:
                c["last_success"] = c["last_success"][:10]
        return d
    return norm(before) != norm(after)


def summary_markdown(outcomes: list[Outcome], after: State, changed: bool) -> str:
    def cell(s: str) -> str:
        return clean_text(s, 160).replace("|", "/")
    lines = [f"### Job ledger refresh: {checked_text(after)}", "",
             "| Company | Result | Detail | Rows | Added | Removed |", "|---|---|---|---|---|---|"]
    for o in outcomes:
        lines.append(f"| {cell(o.company)} | {o.result} | {cell(o.detail)} | {o.before} → {o.after} | "
                     f"{cell(', '.join(o.added)) or '-'} | {cell(', '.join(o.removed)) or '-'} |")
    lines += ["", f"Rows: {len(after.jobs)} · as of {after.as_of.isoformat()} · "
              f"meaningful change: {'yes' if changed else 'no'}", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- fixtures transport

class FixtureTransport:
    """Serves canned responses from a JSON file; unknown requests behave like a network outage."""

    def __init__(self, path: str):
        with open(path, encoding="utf-8") as fh:
            spec = json.load(fh)
        base = os.path.dirname(os.path.abspath(path))
        self.entries = []
        for e in spec["responses"]:
            body = e.get("body", "")
            if "body_file" in e:
                name = e["body_file"]
                if not re.fullmatch(r"[a-z0-9_.-]+", name):
                    raise ValueError("fixture body_file must be a plain file name")
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    body = fh.read()
            self.entries.append((e["method"], e.get("url"), e.get("url_prefix"), int(e["status"]),
                                 e.get("content_type", "text/html; charset=utf-8"), body,
                                 list(e.get("headers", {}).items())))
        self.misses: list[str] = []

    def __call__(self, method, url, headers, body, timeout):
        for m, exact, prefix, status, ctype, text, extra in self.entries:
            if m != method or not (url == exact or (prefix and url.startswith(prefix))):
                continue
            tail = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            text = text.replace("{tail}", html.escape(tail))
            return status, [("Content-Type", ctype), *extra], text.encode("utf-8")
        self.misses.append(f"{method} {url}")
        raise ConnectionError("no fixture")


# --------------------------------------------------------------------------- legacy seed

def parse_legacy_page(doc: str) -> tuple[dt.date, list[list], list[list]]:
    m_rows = re.search(r"\nconst jobs=(\[\n.*?\n\]);\n", doc, re.S)
    m_empty = re.search(r"\nconst emptyCompanies=(\[\n.*?\]\]);\n", doc, re.S)
    m_asof = re.search(r'const AS_OF="(\d{4}-\d{2}-\d{2})"', doc)
    if not (m_rows and m_empty and m_asof):
        raise ValueError("legacy page: data block not found")
    return dt.date.fromisoformat(m_asof.group(1)), json.loads(m_rows.group(1)), json.loads(m_empty.group(1))


def seed_state(doc: str, as_of: dt.date, config: Config) -> State:
    page_as_of, rows, empty = parse_legacy_page(doc)
    if page_as_of != as_of:
        raise ValueError("legacy page AS_OF differs from --as-of")
    jobs = []
    for r in rows:
        company, jid, title, exp, label, age, loc, url, band, closing = r
        posted = as_of - dt.timedelta(days=age)
        if label_for(posted) != label:
            raise ValueError(f"legacy row {jid}: label disagrees with age")
        row = Row(company=company, job_id=jid, title=title, experience_text=exp, experience_band=band,
                  location=loc, canonical_url=url, posted_date=posted.isoformat(), closing_date=closing,
                  first_seen=as_of.isoformat(), last_seen=as_of.isoformat())
        validate_row(row, config)
        jobs.append(row)
    companies = {}
    for c in config.companies:
        static = config.sources[c]["mode"] == "static"
        companies[c] = CompanyState("static" if static else "seed",
                                    f"manual snapshot {as_of.isoformat()}")
    return State(as_of, None, companies, [tuple(e) for e in empty], sort_rows(jobs, config))


# --------------------------------------------------------------------------- files and scan

def read_text(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    if "\r\n" in text:
        raise ValueError(f"{os.path.basename(path)}: expected LF line endings")
    return text


def read_json(path: str) -> dict:
    return json.loads(read_text(path))


def write_atomic(path: str, text: str) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".refresh-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)        # mkstemp is 0600; published files must be world-readable
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_all(root: str) -> tuple[Config, State, str]:
    """Public inputs only; private exclusions are read separately, by the refresh command alone."""
    config = load_config(read_json(os.path.join(root, CONFIG_PATH)))
    state = load_state(read_json(os.path.join(root, STATE_PATH)), config)
    template = read_text(os.path.join(root, TEMPLATE_PATH))
    return config, state, template


def _repo_files(root: str) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                             cwd=root, capture_output=True, check=True, timeout=30).stdout
        files = [f for f in out.decode("utf-8").split("\0") if f]
        return sorted(f for f in files if os.path.isfile(os.path.join(root, f)))
    except (OSError, subprocess.SubprocessError):
        files = []
        for base, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "dist")]
            for n in names:
                files.append(os.path.relpath(os.path.join(base, n), root).replace(os.sep, "/"))
        return sorted(files)


def scan_repo(root: str) -> list[str]:
    """Recursive public-boundary scan. Problems name the path and rule, never matched content."""
    problems = []
    for rel in _repo_files(root):
        if rel not in ALLOWED_FILES and not ALLOWED_FIXTURE.fullmatch(rel):
            problems.append(f"{rel}: file not in allowlist")
            continue
        with open(os.path.join(root, rel), "rb") as fh:
            raw = fh.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{rel}: not UTF-8 text")
            continue
        for pattern in find_forbidden(text):
            problems.append(f"{rel}: forbidden pattern {pattern}")
    try:
        config, state, template = load_all(root)
        expected = render(state, template, config)
        if read_text(os.path.join(root, INDEX_PATH)) != expected:
            problems.append("index.html: does not match render(state/jobs.json)")
        for rel in (INDEX_PATH, STATE_PATH):
            for url in re.findall(r"https?://[^\s\"'<>)\\]+", read_text(os.path.join(root, rel))):
                if check_url(url, config.link_hosts):
                    problems.append(f"{rel}: URL outside link-host allowlist")
                    break
    except (OSError, ValueError) as exc:
        problems.append(f"data: {exc}")
    return problems


# --------------------------------------------------------------------------- CLI

def _now(arg: str | None) -> dt.datetime:
    if not arg:
        return dt.datetime.now(IST)
    now = dt.datetime.fromisoformat(arg)
    return now if now.tzinfo else now.replace(tzinfo=IST)


def cmd_refresh(args, root: str) -> int:
    config, before, template = load_all(root)
    excluded = read_excluded(root, config)
    now = _now(args.now)
    factory: FetcherFactory | None = None
    fixture: FixtureTransport | None = None
    if not args.offline:
        if args.fixtures:
            fixture = FixtureTransport(args.fixtures)

        def factory(company: str, cfg: dict) -> Fetcher:  # noqa: F811 - closure over fixture
            sleep = (lambda _s: None) if fixture else time.sleep
            return Fetcher(config, cfg["budget_s"], transport=fixture, sleep=sleep,
                           cookies=cfg["mode"] in ("siemens_avature_html", "gevernova_paradox_api"))
    after, outcomes = run_refresh(before, config, excluded, now, factory)
    doc = render(after, template, config)
    changed = meaningful(before, after)
    report = summary_markdown(outcomes, after, changed)
    if fixture and fixture.misses:
        report += f"\nFixture misses (treated as outages): {len(fixture.misses)}\n"
    if not args.dry_run:
        write_atomic(os.path.join(root, STATE_PATH), after.to_json())
        write_atomic(os.path.join(root, INDEX_PATH), doc)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if changed and not args.dry_run else 'false'}\n"
                     f"as_of={after.as_of.isoformat()}\n")
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(report)
    print(report)
    return 0


def cmd_render(args, root: str) -> int:
    config, state, template = load_all(root)
    write_atomic(os.path.join(root, args.out) if not os.path.isabs(args.out) else args.out,
                 render(state, template, config))
    return 0


def cmd_check(args, root: str) -> int:
    config, state, template = load_all(root)
    if read_text(os.path.join(root, INDEX_PATH)) != render(state, template, config):
        print("check: index.html does not match render(state)")
        return 1
    print("check: index.html matches state")
    return 0


def cmd_scan(args, root: str) -> int:
    problems = scan_repo(os.path.abspath(args.scan_root or root))
    for p in problems:
        print("scan: " + p)
    print(f"scan: {'FAILED' if problems else 'clean'}")
    return 1 if problems else 0


def cmd_seed(args, root: str) -> int:
    config = load_config(read_json(os.path.join(root, CONFIG_PATH)))
    template = read_text(os.path.join(root, TEMPLATE_PATH))
    state = seed_state(read_text(args.from_html), dt.date.fromisoformat(args.as_of), config)
    write_atomic(os.path.join(root, STATE_PATH), state.to_json())
    write_atomic(os.path.join(root, INDEX_PATH), render(state, template, config))
    print(f"seed: {len(state.jobs)} rows")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=ROOT, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("refresh", help="refresh sources, update state and index.html")
    p.add_argument("--now", help="override the current time (ISO 8601)")
    p.add_argument("--offline", action="store_true", help="no network; recompute dates and pruning only")
    p.add_argument("--fixtures", help="serve HTTP from a JSON fixture file instead of the network")
    p.add_argument("--dry-run", action="store_true", help="do not write state or index.html")
    p.add_argument("--github-output", help="append changed=/as_of= lines to this file")
    p.add_argument("--summary", help="append a Markdown summary to this file")
    p = sub.add_parser("render", help="render state into an HTML file")
    p.add_argument("--out", default=INDEX_PATH)
    sub.add_parser("check", help="verify index.html matches the state")
    p = sub.add_parser("scan", help="recursive public-boundary scan")
    p.add_argument("scan_root", nargs="?", metavar="ROOT")
    p = sub.add_parser("seed", help="one-time: build state from a legacy page")
    p.add_argument("--from-html", required=True)
    p.add_argument("--as-of", required=True)
    args = ap.parse_args(argv)
    handlers = {"refresh": cmd_refresh, "render": cmd_render, "check": cmd_check,
                "scan": cmd_scan, "seed": cmd_seed}
    try:
        return handlers[args.cmd](args, args.root)
    except (OSError, ValueError) as exc:
        print(f"{args.cmd}: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
