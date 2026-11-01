"""Unit tests for scripts/refresh.py. Standard library only; no network access."""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
LEGACY_PAGE = os.path.join(FIXTURES, "index_2026-10-31.html")
FROZEN_STATE = os.path.join(FIXTURES, "state_2026-10-31.json")   # reviewed state; never refreshed

_spec = importlib.util.spec_from_file_location("refresh", os.path.join(ROOT, "scripts", "refresh.py"))
R = importlib.util.module_from_spec(_spec)
sys.modules["refresh"] = R          # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(R)

NOW = dt.datetime(2026, 10, 31, 18, 0, tzinfo=R.IST)
TODAY = NOW.date()


def resp(status: int, body: str = "", ctype: str = "text/html; charset=utf-8", headers=()):
    return status, [("Content-Type", ctype), *headers], body.encode("utf-8")


def jresp(obj, status: int = 200):
    return resp(status, json.dumps(obj), "application/json")


class FakeTransport:
    """Routes (method, url-or-prefix*) to a response tuple, list (consumed in order), callable, or exception."""

    def __init__(self, routes=(), robots=None):
        self.routes = list(routes)
        self.robots = robots if robots is not None else resp(404, "none")
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        if url.endswith("/robots.txt"):
            if isinstance(self.robots, BaseException):
                raise self.robots
            return self.robots
        for m, pattern, handler in self.routes:
            hit = url == pattern or (pattern.endswith("*") and url.startswith(pattern[:-1]))
            if m == method and hit:
                out = handler(url, body) if callable(handler) else handler
                if isinstance(out, list):
                    out = out.pop(0) if len(out) > 1 else out[0]
                if isinstance(out, BaseException):
                    raise out
                return out
        raise ConnectionError("unrouted")

    def count(self, fragment: str) -> int:
        return sum(1 for _m, u in self.calls if fragment in u)


# Synthetic exclusions only. Real IDs live in a secret / git-ignored file and never in tests.
TEST_EXCLUDED = {"schema": 1, "ids": {"Siemens": ["500001"], "IQVIA": ["R1590001"]}}


def env():
    config = R.load_config(R.read_json(os.path.join(ROOT, R.CONFIG_PATH)))
    excluded = R.load_excluded(TEST_EXCLUDED, config)
    template = R.read_text(os.path.join(ROOT, R.TEMPLATE_PATH))
    state = R.seed_state(R.read_text(LEGACY_PAGE), TODAY, config)
    return config, excluded, state, template


def frozen_env():
    """The reviewed 2026-10-31 state. Its formerly static sources are now live modes, so they load as
    'seed' (the same conversion applied to the committed state); rows are untouched."""
    config = R.load_config(R.read_json(os.path.join(ROOT, R.CONFIG_PATH)))
    template = R.read_text(os.path.join(ROOT, R.TEMPLATE_PATH))
    data = R.read_json(FROZEN_STATE)
    for name, cs in data["companies"].items():
        if cs["status"] == "static" and config.sources[name]["mode"] != "static":
            cs["status"] = "seed"
    return config, R.load_state(data, config), template


def factory_for(config, transport):
    return lambda company, cfg: R.Fetcher(config, cfg["budget_s"], transport=transport,
                                          sleep=lambda _s: None, cookies=True)


def rows_of(state, company):
    return [r for r in state.jobs if r.company == company]


def siemens_detail_page(jid: str, posted: str = "30-Oct-2026", extra: str = "") -> str:
    return ('<html><head><title>Job</title></head><body>'
            '<div class="article__content__view__field__label">Job ID</div>'
            f'<div class="article__content__view__field__value">{jid}</div>'
            '<div class="article__content__view__field__label">Posted since</div>'
            f'<div class="article__content__view__field__value">{posted}</div>'
            '<div class="article__content__view__field__label">Location(s)</div>'
            '<div class="article__content__view__field__value">Bangalore, Karnataka, India</div>'
            f'<p>{extra}</p></body></html>')


def siemens_list_page(ids) -> str:
    return "".join(f'<div class="article__header__text__title"><a href="https://jobs.siemens.com/'
                   f'en_US/externaljobs/JobDetail/{i}">Role {i}</a></div>' for i in ids)


def live_siemens_routes():
    return [("GET", "https://jobs.siemens.com/en_US/externaljobs/JobDetail/*",
             lambda url, _b: resp(200, siemens_detail_page(url.rsplit("/", 1)[-1])))]


def wd_listing(ids_paths):
    return {"total": len(ids_paths), "jobPostings": [
        {"title": "t", "externalPath": p, "postedOn": "Posted Yesterday"} for p in ids_paths]}


IQVIA_PATHS = {"R1553313": "/job/Kochi-India/Assoc-Clin-DB_R1553313",
               "R1566373": "/job/Kochi-India/Clin-Data-Spec_R1566373"}


def wd_detail(jid, **kw):
    info = {"jobReqId": jid, "title": "t", "posted": True, "canApply": True, "startDate": "2026-10-30"}
    info.update(kw)
    return jresp({"jobPostingInfo": info})


def ge_listing(ids, total=None):
    return jresp({"totalJob": len(ids) if total is None else total,
                  "jobs": [{"requisitionID": i, "title": "t"} for i in ids]})


GE_LIST_URL = R.GE_LIST.format(city="Bengaluru", page=1)
GE_IDS = ["R5055232", "R5055227", "R5056034", "R5055467"]

# One compact breakpoint shared by the script (initial panel state) and the stylesheet (drawer chrome).
COMPACT_MQ = "(max-width:1023px)"      # single-column layout; the script and the CSS share this string
WIDE_MQ = "(min-width:1024px)"         # two columns: feed + sticky Informational aside
SHEET_MQ = "(max-width:767px)"
PHONE_MQ = "(max-width:639px)"


def style_of(doc: str) -> str:
    return re.search(r"<style>(.*?)</style>", doc, re.S).group(1)


def media_blocks(style: str) -> tuple[str, dict]:
    """(rules outside any @media, {condition: body}) by brace matching; conditions keep source spelling."""
    base, blocks, i = [], {}, 0
    while True:
        j = style.find("@media", i)
        if j < 0:
            base.append(style[i:])
            return "".join(base), blocks
        base.append(style[i:j])
        open_ = style.index("{", j)
        depth, k = 1, open_ + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(style[k], 0)
            k += 1
        blocks[style[j + 6:open_]] = style[open_ + 1:k - 1]
        i = k


def css_rules(css: str) -> list:
    """[(selectors, declarations)] for a flat rule list."""
    return [([s.strip() for s in sel.split(",")], body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def hidden_selectors(css: str) -> set:
    """Every selector, from any nesting level, given display:none. Selectors are split on commas."""
    flat = re.sub(r"@media[^{]*\{", "", css)
    return {s for sels, body in css_rules(flat) if re.search(r"(^|;)display:none", body) for s in sels}


def media_matches(condition: str, width: int, height: int) -> bool:
    """Evaluate the OR-list of (max-|min-)(width|height):Npx features the page uses."""
    def one(q):
        feats = re.findall(r"\((min|max)-(width|height):(\d+)px\)", q)
        assert feats and re.fullmatch(r"(\((min|max)-(width|height):\d+px\)|and|\s)+", q), q
        return all((width if dim == "width" else height) <= int(n) if op == "max" else
                   (width if dim == "width" else height) >= int(n) for op, dim, n in feats)
    return any(one(q.strip()) for q in condition.split(","))


HSL_TOKEN = r"(\d+(?:\.\d+)?) (\d+(?:\.\d+)?)% (\d+(?:\.\d+)?)%"


def hsl_luminance(value: str) -> float:
    """WCAG relative luminance of an `H S% L%` token triplet (CSS hsl -> sRGB)."""
    h, s, lightness = (float(x) for x in re.fullmatch(HSL_TOKEN, value).groups())
    s, lightness = s / 100, lightness / 100
    a = s * min(lightness, 1 - lightness)

    def channel(n: int) -> float:
        k = (n + h / 30) % 12
        c = lightness - a * max(-1.0, min(k - 3, 9 - k, 1.0))
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(0) + 0.7152 * channel(8) + 0.0722 * channel(4)


def contrast(x: str, y: str) -> float:
    hi, lo = sorted((hsl_luminance(x), hsl_luminance(y)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def theme_tokens(style: str) -> tuple[dict, dict]:
    """({token: 'H S% L%'} for the default dark theme, the same with the light overrides applied)."""
    grab = lambda body: dict(re.findall(r"--([a-z0-9-]+):(\d+ \d+% \d+%)", body))  # noqa: E731
    dark = grab(re.search(r":root\{([^}]*)\}", style).group(1))
    return dark, dark | grab(re.search(r"html\[data-theme=light\]\{([^}]*)\}", style).group(1))


# --------------------------------------------------------------------------- seed, render, template

class SeedAndRenderTests(unittest.TestCase):
    def test_seed_round_trips_current_rows(self):
        config, _ex, state, _t = env()
        _as_of, legacy, _empty = R.parse_legacy_page(R.read_text(LEGACY_PAGE))
        self.assertEqual(len(state.jobs), len(legacy))
        for company in config.companies:
            expected = [r for r in legacy if r[0] == company]
            got = [r.js_row(state.as_of) for r in rows_of(state, company)]
            self.assertEqual(got, expected, company)
        self.assertEqual(R.load_state(json.loads(state.to_json()), config).to_json(), state.to_json())

    def test_seed_preserves_iqvia_closing_dates(self):
        _c, _e, state, _t = env()
        closing = {r.job_id: r.closing_date for r in rows_of(state, "IQVIA")}
        self.assertEqual(closing, {"R1553313": "2026-11-23", "R1566373": "2026-11-02"})

    def test_render_is_deterministic_and_rows_match_legacy_lines(self):
        config, _e, state, template = env()
        first, second = R.render(state, template, config), R.render(state, template, config)
        self.assertEqual(first, second)
        legacy = R.read_text(LEGACY_PAGE)
        legacy_lines = set(re.search(r"\nconst jobs=\[\n(.*?)\n\];", legacy, re.S).group(1).split(",\n"))
        new_lines = set(re.search(r"\nconst jobs=\[\n(.*?)\n\];", first, re.S).group(1).split(",\n"))
        self.assertEqual(new_lines, legacy_lines)

    def test_committed_index_matches_committed_state(self):
        config, state, template = R.load_all(ROOT)
        self.assertEqual(R.read_text(os.path.join(ROOT, "index.html")), R.render(state, template, config))

    def test_closing_filter_and_compact_layout_retained(self):
        config, _e, state, template = env()
        doc, legacy = R.render(state, template, config), R.read_text(LEGACY_PAGE)
        snippets = [
            re.search(r'<div class="field"><label for="closing">.*?</select></div>', legacy, re.S).group(0),
            re.search(r"function match\(r\)\{.*?\}return true\}", legacy).group(0),
            re.search(r"daysToClose=s=>[^,]*,fmtClose=[^;]*;", legacy).group(0),
        ]
        for s in snippets:
            self.assertIn(s, doc)
        # Content grid: one column by default and below 1024px (feed first, then the Informational aside);
        # from 1024px the feed plus one sticky 300px aside.
        style = style_of(doc)
        base, blocks = media_blocks(style)
        self.assertIn(".grid{display:grid;grid-template-columns:minmax(0,1fr);gap:24px;margin-top:24px}", base)
        self.assertIn(".grid{grid-template-columns:minmax(0,1fr)}.feed{order:1}.ar{order:2}", blocks[COMPACT_MQ])
        self.assertEqual(blocks[WIDE_MQ],
                         ".grid{grid-template-columns:minmax(0,1fr) 300px}.feed{grid-column:1}.ar{grid-column:2;"
                         "position:sticky;top:16px;align-self:start;display:flex;flex-direction:column;"
                         "max-height:calc(100dvh - 2rem)}.ar-in{overflow-y:auto}")
        self.assertFalse([c for c in blocks if "1279px" in c or "1280px" in c])
        for w in (320, 390, 768, 1023, 1024, 1279, 1280, 1440):
            with self.subTest(w):
                self.assertEqual([media_matches(c, w, 900) for c in (COMPACT_MQ, WIDE_MQ)], [w < 1024, w >= 1024])
        # DOM order matches the single-column order: feed, then the Informational aside. No left aside.
        self.assertLess(doc.index('<section class="feed" id="feed"'), doc.index('<aside class="ar"'))
        self.assertNotIn('class="al"', doc)
        self.assertNotIn(".al", style)
        # Phones: tighter stream, titles clamped to two lines, filters 2 columns.
        phone = blocks[PHONE_MQ]
        for rule in ("#results{gap:8px;", "-webkit-line-clamp:2", "-webkit-line-clamp:1",
                     ".fsel{grid-template-columns:repeat(2,minmax(0,1fr));",
                     ".cidx ol{flex-wrap:nowrap;overflow-x:auto;"):
            self.assertIn(rule, phone, rule)
        self.assertNotIn(".c-f", style)
        self.assertIn("#results{display:grid;gap:12px;", base)
        self.assertIn(".fsel{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));", base)
        # Search panel: heading then the LED line; no "Live" tag and no helper paragraph.
        self.assertIn('<h2 id="qd-h">Search the ledger</h2><p class="label-sys src-led" id="src-led"', doc)
        for gone in ('class="tag"', "srch-p", "Type a title, skill or job ID"):
            self.assertNotIn(gone, doc, gone)
        self.assertFalse([s for s, _b in css_rules(re.sub(r"@media[^{]*\{", "", style))
                          if any(x in (".tag", ".srch-p") for x in s)])
        self.assertIn("overflow-wrap:anywhere", style)
        self.assertNotIn("<table", doc)
        self.assertIn(f'mq=matchMedia("{COMPACT_MQ}")', doc)
        self.assertIn("@media(prefers-reduced-motion:reduce)", style)

    def test_checked_and_stale_metadata(self):
        config, _e, state, template = env()
        doc = R.render(state, template, config)
        led = '<p class="label-sys src-led" id="src-led" data-s="'
        self.assertRegex(doc, re.escape(led) + r'[a-z]+"><span class="led" aria-hidden="true"></span>'
                              r'Auto-check pending</p>')
        self.assertEqual(doc.count("Auto-check pending"), 2)   # src-led + status block Last refresh
        self.assertNotIn('id="checked"', doc)
        state.checked_at = "2026-10-31T15:34+05:30"
        state.companies["IQVIA"] = dataclasses.replace(state.companies["IQVIA"], status="stale",
                                                       last_success="2026-10-30T09:23+05:30")
        doc = R.render(state, template, config)
        self.assertRegex(doc, re.escape(led) + r'[a-z]+"><span class="led" aria-hidden="true"></span>'
                              r'Checked 31 Oct 15:34 IST</p>')
        self.assertEqual(doc.count("Checked 31 Oct 15:34 IST"), 1)
        self.assertIn('"IQVIA":"stale since Oct 30"', doc)
        self.assertIn('"ZS":"not auto-checked"', doc)

    def test_malicious_title_is_inert(self):
        config, _e, state, template = env()
        evil = '</script><script>alert(1)</script><!-- \u2028\u2029 "q" \'s\''
        state.jobs[0] = dataclasses.replace(state.jobs[0], title=evil)
        doc = R.render(state, template, config)
        self.assertEqual(doc.lower().count("</script"), template.lower().count("</script"))
        self.assertNotIn("<script>alert", doc)
        self.assertNotIn("<!--", doc)
        self.assertNotIn("\u2028", doc)
        self.assertIn("\\u003c/script\\u003e", doc)
        self.assertIn("\\u2028\\u2029", doc)

    def test_template_rejects_unknown_or_missing_placeholders(self):
        config, _e, state, template = env()
        with self.assertRaises(ValueError):
            R.render(state, template + "{{EVIL}}", config)
        with self.assertRaises(ValueError):
            R.render(state, template.replace("{{STALE_JS}}", "{}"), config)

    def test_placeholder_text_in_data_is_not_expanded(self):
        config, _e, state, template = env()
        state.jobs[0] = dataclasses.replace(state.jobs[0], title="{{JOBS_JS}} {{DAY}}")
        self.assertIn('"{{JOBS_JS}} {{DAY}}"', R.render(state, template, config))


# --------------------------------------------------------------------------- dates

class DateTests(unittest.TestCase):
    def test_workday_end_date_is_exclusive(self):
        self.assertEqual(R.workday_closing("2026-11-24"), "2026-11-23")
        self.assertEqual(R.workday_closing("2026-12-01T00:00:00.000Z"), "2026-11-30")
        self.assertIsNone(R.workday_closing(None))
        self.assertIsNone(R.workday_closing("soon"))

    def test_generic_closing_dates_are_inclusive(self):
        self.assertEqual(R.generic_closing("2026-11-24"), "2026-11-24")
        self.assertEqual(R.generic_closing("24-Nov-2026"), "2026-11-24")
        self.assertEqual(R.generic_closing("Nov 24, 2026"), "2026-11-24")
        self.assertEqual(R.generic_closing("24 November 2026"), "2026-11-24")
        self.assertIsNone(R.generic_closing("2026-02-30"))

    def test_passed_deadline_and_age_out_are_pruned(self):
        config, excluded, state, _t = env()
        later = dt.datetime(2026, 11, 3, 12, 0, tzinfo=R.IST)
        after, _o = R.run_refresh(state, config, excluded, later, None)
        ids = {r.job_id for r in after.jobs}
        self.assertNotIn("R1566373", ids)          # closes Nov 2, inclusive
        self.assertIn("R1553313", ids)             # closes Nov 23
        far = dt.datetime(2026, 11, 26, 12, 0, tzinfo=R.IST)
        after, _o = R.run_refresh(state, config, excluded, far, None)
        self.assertTrue(all((far.date() - dt.date.fromisoformat(r.posted_date)).days <= 30 for r in after.jobs))

    def test_offline_refresh_updates_ages_only(self):
        config, excluded, state, _t = env()
        nxt = dt.datetime(2026, 11, 1, 8, 0, tzinfo=R.IST)
        after, _o = R.run_refresh(state, config, excluded, nxt, None)
        self.assertEqual(after.as_of, nxt.date())
        self.assertIsNone(after.checked_at)
        self.assertEqual([r.to_dict() for r in after.jobs], [r.to_dict() for r in state.jobs])
        self.assertEqual(after.jobs[0].js_row(after.as_of)[5], state.jobs[0].js_row(state.as_of)[5] + 1)


# --------------------------------------------------------------------------- outages keep LKG

class OutageTests(unittest.TestCase):
    def assert_all_preserved(self, transport):
        config, excluded, state, _t = env()
        after, outcomes = R.run_refresh(state, config, excluded, NOW, factory_for(config, transport))
        self.assertEqual([r.to_dict() for r in after.jobs], [r.to_dict() for r in state.jobs])
        for o in outcomes:
            if o.result != "static":
                self.assertEqual(o.result, "held", o)
                self.assertEqual(after.companies[o.company].status, "stale")
                self.assertIsNone(after.companies[o.company].last_success)
                self.assertIsNotNone(after.companies[o.company].stale_since)
        return after

    def test_outages_preserve_last_known_good(self):
        cases = {
            "403": resp(403, "<html><title>Forbidden</title></html>"),
            "429": resp(429, "slow down", headers=[("Retry-After", "1")]),
            "500": resp(500, "error"),
            "503": resp(503, "unavailable"),
            "maintenance": resp(200, "<html><head><title>Workday</title></head><body>"
                                     "We are down for maintenance</body></html>"),
            "challenge": resp(200, "<html><head><title>Just a moment...</title></head></html>"),
            "timeout": TimeoutError("slow"),
            "network": ConnectionError("reset"),
        }
        for name, out in cases.items():
            with self.subTest(name):
                self.assert_all_preserved(FakeTransport([("GET", "https://*", out), ("POST", "https://*", out)],
                                                        robots=out))

    def test_workday_schema_change_preserves_iqvia(self):
        config, excluded, state, _t = env()
        t = FakeTransport([("POST", R.IQVIA_API + "/jobs", jresp({"results": []}))])
        after, _o = R.run_refresh(state, config, excluded, NOW, factory_for(config, t))
        self.assertEqual(after.companies["IQVIA"].status, "stale")
        self.assertEqual(rows_of(after, "IQVIA"), rows_of(state, "IQVIA"))

    def test_workday_partial_pagination_preserves_iqvia(self):
        config, excluded, state, _t = env()
        first = {"total": 25, "jobPostings": [{"externalPath": IQVIA_PATHS["R1553313"]}] * 20}
        t = FakeTransport([("POST", R.IQVIA_API + "/jobs", [jresp(first), jresp({"total": 0, "jobPostings": []})])])
        after, _o = R.run_refresh(state, config, excluded, NOW, factory_for(config, t))
        self.assertEqual(after.companies["IQVIA"].detail, "workday listing incomplete or unavailable")
        self.assertEqual(rows_of(after, "IQVIA"), rows_of(state, "IQVIA"))

    def test_duplicated_pagination_is_not_complete(self):
        config, excluded, state, _t = env()
        page1 = {"total": 21, "jobPostings": [{"externalPath": f"/job/K/X_R15{i:05d}"} for i in range(20)]}
        page2 = {"total": 0, "jobPostings": [{"externalPath": "/job/K/X_R1500000"}]}   # duplicate of page 1
        t = FakeTransport([("POST", R.IQVIA_API + "/jobs", [jresp(page1), jresp(page2)] * 3)])
        f = R.Fetcher(config, 60, transport=t, sleep=lambda _s: None)
        self.assertIsNone(R._wd_search(f, "Kochi", 600))
        t = FakeTransport([("GET", R.GE_BOOT, resp(200, "<html><title>Jobs</title></html>")),
                           ("POST", GE_LIST_URL, ge_listing(GE_IDS[:2] + GE_IDS[:2], total=4))])
        f = R.Fetcher(config, 60, transport=t, sleep=lambda _s: None, cookies=True)
        self.assertEqual(R._ge_listing(config.sources["GE Vernova"], f), "partial pagination")

    def test_paradox_partial_pagination_preserves_ge(self):
        config, excluded, state, _t = env()
        t = FakeTransport([("GET", R.GE_BOOT, resp(200, "<html><title>Jobs</title></html>")),
                           ("POST", GE_LIST_URL, ge_listing(GE_IDS[:2], total=45)),
                           ("POST", R.GE_LIST.format(city="Bengaluru", page=2), jresp({"totalJob": 45, "jobs": []}))])
        after, _o = R.run_refresh(state, config, excluded, NOW, factory_for(config, t))
        self.assertEqual(after.companies["GE Vernova"].detail, "partial pagination")
        self.assertEqual(rows_of(after, "GE Vernova"), rows_of(state, "GE Vernova"))

    def test_source_exception_is_contained(self):
        config, excluded, state, _t = env()
        original = R.RUNNERS["iqvia_workday_cxs"]
        R.RUNNERS["iqvia_workday_cxs"] = lambda *a: (_ for _ in ()).throw(KeyError("boom"))
        try:
            after, _o = R.run_refresh(state, config, excluded, NOW, factory_for(config, FakeTransport()))
        finally:
            R.RUNNERS["iqvia_workday_cxs"] = original
        self.assertEqual(after.companies["IQVIA"].detail, "source error (KeyError)")
        self.assertEqual(rows_of(after, "IQVIA"), rows_of(state, "IQVIA"))

    def test_stale_since_is_kept_across_failures(self):
        _c, _e, state, _t = env()
        cs = dataclasses.replace(state.companies["IQVIA"], status="stale", stale_since="2026-10-30T09:23+05:30")
        _rows, new_cs, _rm = R.apply_result([], cs, R.held("IQVIA", "x"), NOW)
        self.assertEqual(new_cs.stale_since, "2026-10-30T09:23+05:30")


# --------------------------------------------------------------------------- removals

class RemovalTests(unittest.TestCase):
    def test_workday_closed_flag_removes_and_end_date_updates(self):
        config, excluded, state, _t = env()
        t = FakeTransport([
            ("POST", R.IQVIA_API + "/jobs", jresp(wd_listing(list(IQVIA_PATHS.values())))),
            ("GET", R.IQVIA_API + IQVIA_PATHS["R1553313"], wd_detail("R1553313", canApply=False)),
            ("GET", R.IQVIA_API + IQVIA_PATHS["R1566373"], wd_detail("R1566373", endDate="2026-11-05")),
        ])
        after, outcomes = R.run_refresh(state, config, excluded, NOW, factory_for(config, t))
        iq = rows_of(after, "IQVIA")
        self.assertEqual([r.job_id for r in iq], ["R1566373"])
        self.assertEqual(iq[0].closing_date, "2026-11-04")
        self.assertEqual(after.companies["IQVIA"].status, "ok")
        self.assertIn("R1553313", next(o for o in outcomes if o.company == "IQVIA").removed)

    def test_ge_410_removes_absent_row(self):
        config, excluded, state, _t = env()
        t = FakeTransport([("GET", R.GE_BOOT, resp(200, "<html><title>Jobs</title></html>")),
                           ("POST", GE_LIST_URL, ge_listing([i for i in GE_IDS if i != "R5056034"])),
                           ("GET", "https://careers.gevernova.com/piping-design-engineer/job/R5056034", resp(410, "gone"))])
        after, _o = R.run_refresh(state, config, excluded, NOW, factory_for(config, t))
        self.assertNotIn("R5056034", {r.job_id for r in rows_of(after, "GE Vernova")})
        self.assertEqual(len(rows_of(after, "GE Vernova")), 3)

    def test_siemens_no_longer_available_removes(self):
        config, excluded, state, _t = env()
        closed = "<html><head><title>Job</title></head><body>This job is no longer available.</body></html>"
        t = FakeTransport([("GET", R.SIEMENS_DETAIL.format(id="527740"), resp(200, closed)), *live_siemens_routes()])
        after, _o = R.run_refresh(state, config, excluded, NOW, factory_for(config, t))
        ids = {r.job_id for r in rows_of(after, "Siemens")}
        self.assertNotIn("527740", ids)
        self.assertEqual(len(ids), 22)

    def test_404_alone_never_removes(self):
        config, excluded, state, _t = env()
        t = FakeTransport([("GET", R.SIEMENS_DETAIL.format(id="527740"), resp(404, "not found")),
                           *live_siemens_routes()])
        after, _o = R.run_refresh(state, config, excluded, NOW, factory_for(config, t))
        row = next(r for r in after.jobs if r.job_id == "527740")
        self.assertEqual(row.liveness, "unconfirmed")

    def _complete(self, ids, rows, **kw):
        return R.SourceResult("IQVIA", True, "ok", listing_complete=True, listed=set(ids),
                              listing_total=kw.pop("total", 40), **kw)

    def test_one_authoritative_absence_keeps_two_remove(self):
        _c, _e, state, _t = env()
        rows, cs = rows_of(state, "IQVIA"), state.companies["IQVIA"]
        res = self._complete({"R1553313"}, rows, live={"R1553313"})
        rows1, cs1, removed1 = R.apply_result(rows, cs, res, NOW)
        self.assertEqual(removed1, [])
        absent = next(r for r in rows1 if r.job_id == "R1566373")
        self.assertEqual((absent.liveness, absent.absent_runs), ("absent", 1))
        rows2, _cs2, removed2 = R.apply_result(rows1, cs1, self._complete({"R1553313"}, rows1, live={"R1553313"}), NOW)
        self.assertEqual(removed2, ["R1566373"])
        self.assertEqual([r.job_id for r in rows2], ["R1553313"])

    def test_live_evidence_resets_absence(self):
        _c, _e, state, _t = env()
        rows = [dataclasses.replace(r, absent_runs=1, liveness="absent") for r in rows_of(state, "IQVIA")]
        out, _cs, removed = R.apply_result(rows, state.companies["IQVIA"],
                                           self._complete({"R1553313", "R1566373"}, rows,
                                                          live={"R1553313", "R1566373"}), NOW)
        self.assertEqual(removed, [])
        self.assertTrue(all(r.absent_runs == 0 and r.liveness == "live" for r in out))

    def test_incomplete_listing_never_counts_absence(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "IQVIA")
        res = R.SourceResult("IQVIA", True, "ok", listing_complete=False, listed=set())
        out, _cs, removed = R.apply_result(rows, state.companies["IQVIA"], res, NOW)
        self.assertEqual(removed, [])
        self.assertTrue(all(r.absent_runs == 0 and r.liveness == "unconfirmed" for r in out))

    def test_absence_scope_limits_counting(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "IQVIA")
        res = self._complete(set(), rows, absence_scope={"R1553313"})
        out, _cs, _rm = R.apply_result(rows, state.companies["IQVIA"], res, NOW)
        runs = {r.job_id: r.absent_runs for r in out}
        self.assertEqual(runs, {"R1553313": 1, "R1566373": 0})

    def test_absences_must_come_from_consecutive_healthy_runs(self):
        _c, _e, state, _t = env()
        rows, cs = rows_of(state, "IQVIA"), state.companies["IQVIA"]
        absent = self._complete({"R1553313"}, rows, live={"R1553313"})
        between = {
            "partial listing": R.SourceResult("IQVIA", True, "ok", listing_complete=False, live={"R1553313"}),
            "out of absence scope": self._complete({"R1553313"}, rows, live={"R1553313"}, absence_scope=set()),
            "failed source": R.held("IQVIA", "workday listing incomplete or unavailable"),
            "suspicious shrink": self._complete({"R1553313"}, rows, live={"R1553313"}, total=4),
        }
        for name, middle in between.items():
            with self.subTest(name):
                rows1, cs1, _ = R.apply_result(rows, cs, absent, NOW)
                self.assertEqual(by_id(rows1)["R1566373"].absent_runs, 1)
                rows2, cs2, removed = R.apply_result(rows1, cs1, middle, NOW + R.REFRESH_CADENCE)
                self.assertEqual(removed, [])
                self.assertEqual([r.job_id for r in rows2], [r.job_id for r in rows])    # nothing deleted
                self.assertEqual(by_id(rows2)["R1566373"].absent_runs, 0)
                rows3, _cs3, removed = R.apply_result(rows2, cs2, absent, NOW + 2 * R.REFRESH_CADENCE)
                self.assertEqual((removed, by_id(rows3)["R1566373"].absent_runs), ([], 1))

    def test_siemens_runs_between_discoveries_keep_the_absence_streak(self):
        # Discovery (complete listing) runs once a day; the three six-hourly runs in between check
        # detail pages only and must neither count nor break an absence streak.
        _c, _e, state, _t = env()
        rows, cs = rows_of(state, "Siemens")[:4], state.companies["Siemens"]
        gone = rows[0].job_id
        others = {r.job_id for r in rows[1:]}
        discovery = R.SourceResult("Siemens", True, "ok", listing_complete=True, listed=set(others),
                                   live=set(others), discovery_attempted=True)
        between = R.SourceResult("Siemens", True, "ok", live=set(others), listing_observed=False)
        rows1, cs1, _ = R.apply_result(rows, cs, discovery, NOW)
        self.assertEqual(by_id(rows1)[gone].absent_runs, 1)
        for k in (1, 2, 3):
            rows1, cs1, removed = R.apply_result(rows1, cs1, between, NOW + k * R.REFRESH_CADENCE)
            self.assertEqual((removed, by_id(rows1)[gone].absent_runs), ([], 1))
        _rows, _cs, removed = R.apply_result(rows1, cs1, discovery, NOW + 4 * R.REFRESH_CADENCE)
        self.assertEqual(removed, [gone])
        # A discovery run that fails to complete does break the streak.
        partial = dataclasses.replace(discovery, listing_complete=False)
        rows2, cs2, _ = R.apply_result(rows, cs, discovery, NOW)
        rows2, cs2, _ = R.apply_result(rows2, cs2, partial, NOW + R.REFRESH_CADENCE)
        self.assertEqual(by_id(rows2)[gone].absent_runs, 0)

    def test_failed_run_keeps_rows_and_their_display_fields(self):
        _c, _e, state, _t = env()
        rows = [dataclasses.replace(r, absent_runs=1, liveness="absent") for r in rows_of(state, "IQVIA")]
        out, cs, removed = R.apply_result(rows, state.companies["IQVIA"], R.held("IQVIA", "down"), NOW)
        self.assertEqual((removed, cs.status), ([], "stale"))
        strip = lambda r: dataclasses.replace(r, absent_runs=0, liveness="live")   # noqa: E731
        self.assertEqual([strip(r) for r in out], [strip(r) for r in rows])
        self.assertTrue(all((r.absent_runs, r.liveness) == (0, "unconfirmed") for r in out))

    def test_removal_guard_hold_keeps_the_absence_streak(self):
        _c, _e, state, _t = env()
        rows = [dataclasses.replace(r, absent_runs=1, liveness="absent") for r in rows_of(state, "Siemens")[:6]]
        cs = state.companies["Siemens"]
        res = R.SourceResult("Siemens", True, "ok", listing_complete=True, listed=set(), live=set())
        out, cs1, removed = R.apply_result(rows, cs, res, NOW)
        self.assertEqual((removed, cs1.detail), ([], R.REMOVAL_DETAIL))
        self.assertEqual(out, rows)                       # the confirming run needs these exact rows
        out2, _cs2, removed = R.apply_result(out, cs1, res, NOW + R.REFRESH_CADENCE)
        self.assertEqual(sorted(removed), sorted(r.job_id for r in rows))


class GuardTests(unittest.TestCase):
    def test_empty_listing_with_rows_is_held(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "IQVIA")
        res = R.SourceResult("IQVIA", True, "ok", listing_complete=True, listed=set(), listing_total=0)
        out, cs, removed = R.apply_result(rows, state.companies["IQVIA"], res, NOW)
        self.assertEqual((out, removed, cs.status, cs.detail), (rows, [], "stale", "empty listing"))

    def test_suspicious_shrink_is_held(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "IQVIA")
        prior = dataclasses.replace(state.companies["IQVIA"], listing_total=120)
        res = R.SourceResult("IQVIA", True, "ok", listing_complete=True, listed={"R1553313", "R1566373"},
                             live={"R1553313", "R1566373"}, listing_total=40)
        out, cs, _rm = R.apply_result(rows, prior, res, NOW)
        self.assertEqual((out, cs.status, cs.detail, cs.listing_total), (rows, "stale", "suspicious listing shrink", 120))

    def test_repeated_shrink_is_accepted_without_mass_removal(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "IQVIA")
        prior = dataclasses.replace(state.companies["IQVIA"], listing_total=120)
        res = R.SourceResult("IQVIA", True, "ok", listing_complete=True, listed={"R1553313", "R1566373"},
                             live={"R1553313", "R1566373"}, listing_total=40)
        _o, held_cs, _rm = R.apply_result(rows, prior, res, NOW)
        out, cs, removed = R.apply_result(rows, held_cs, res, NOW)
        self.assertEqual((cs.status, cs.listing_total, removed, len(out)), ("ok", 40, [], 2))

    def test_mass_removal_is_held(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "Siemens")[:6]
        res = R.SourceResult("Siemens", True, "ok", closed={r.job_id for r in rows[:4]}, live={rows[5].job_id})
        out, cs, removed = R.apply_result(rows, state.companies["Siemens"], res, NOW)
        self.assertEqual((out, removed, cs.detail), (rows, [], "removal guard"))

    @staticmethod
    def closes(rows, n, company="Siemens"):
        """Complete observation: the first n rows closed, every other row live."""
        return R.SourceResult(company, True, "ok", closed={r.job_id for r in rows[:n]},
                              live={r.job_id for r in rows[n:]})

    def test_mass_removal_is_confirmed_one_cadence_later(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "Siemens")[:6]
        out1, cs1, rm1 = R.apply_result(rows, state.companies["Siemens"], self.closes(rows, 4), NOW)
        self.assertEqual((out1, rm1, cs1.status, cs1.detail), (rows, [], "stale", R.REMOVAL_DETAIL))
        self.assertEqual(cs1.removal_hold, {"at": R.stamp(NOW), "ids": sorted(r.job_id for r in rows[:4])})
        soon = NOW + R.REFRESH_CADENCE - dt.timedelta(minutes=1)
        out2, cs2, rm2 = R.apply_result(rows, cs1, self.closes(rows, 4), soon)
        self.assertEqual((out2, rm2, cs2.status, cs2.removal_hold), (rows, [], "stale", cs1.removal_hold))
        later = NOW + R.REFRESH_CADENCE
        out3, cs3, rm3 = R.apply_result(rows, cs2, self.closes(rows, 4), later)
        self.assertEqual(sorted(rm3), cs1.removal_hold["ids"])
        self.assertEqual(out3, [dataclasses.replace(r, last_seen=later.date().isoformat()) for r in rows[4:]])
        self.assertEqual((cs3.status, cs3.removal_hold, cs3.stale_since), ("ok", None, None))
        self.assertTrue(cs3.detail.startswith("removal confirmed"))

    def test_failed_or_partial_runs_never_start_or_advance_a_hold(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "Siemens")[:6]
        later = NOW + dt.timedelta(hours=7)
        partial = R.SourceResult("Siemens", True, "ok", closed={r.job_id for r in rows[:4]},
                                 live={rows[4].job_id})            # rows[5] got no definitive answer
        _o, cs_p, rm = R.apply_result(rows, state.companies["Siemens"], partial, NOW)
        self.assertEqual((rm, cs_p.detail, cs_p.removal_hold), ([], R.REMOVAL_DETAIL, None))
        _o, cs1, _rm = R.apply_result(rows, cs_p, self.closes(rows, 4), NOW)
        hold = cs1.removal_hold
        self.assertIsNotNone(hold)
        _o, cs_f, rm = R.apply_result(rows, cs1, R.held("Siemens", "detail pages unavailable"), later)
        self.assertEqual((rm, cs_f.status, cs_f.removal_hold), ([], "stale", hold))
        out, cs_p2, rm = R.apply_result(rows, cs_f, partial, later)
        self.assertEqual((out, rm, cs_p2.detail, cs_p2.removal_hold), (rows, [], R.REMOVAL_DETAIL, hold))
        _o, cs_ok, rm = R.apply_result(rows, cs_p2, self.closes(rows, 4), later)
        self.assertEqual((len(rm), cs_ok.status, cs_ok.removal_hold), (4, "ok", None))

    def test_hold_restarts_when_more_rows_would_go(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "Siemens")[:8]
        _o, cs1, _rm = R.apply_result(rows, state.companies["Siemens"], self.closes(rows, 5), NOW)
        later = NOW + R.REFRESH_CADENCE
        out, cs2, rm = R.apply_result(rows, cs1, self.closes(rows, 6), later)
        self.assertEqual((out, rm, cs2.detail), (rows, [], R.REMOVAL_DETAIL))
        self.assertEqual(cs2.removal_hold, {"at": R.stamp(later), "ids": sorted(r.job_id for r in rows[:6])})
        _o, cs3, rm = R.apply_result(rows, cs2, self.closes(rows, 5), later + R.REFRESH_CADENCE)
        self.assertEqual((len(rm), cs3.status), (5, "ok"))      # a subset of the held set confirms

    def test_recovery_clears_a_hold(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "Siemens")[:6]
        _o, cs1, _rm = R.apply_result(rows, state.companies["Siemens"], self.closes(rows, 4), NOW)
        out, cs2, rm = R.apply_result(rows, cs1, self.closes(rows, 0), NOW + dt.timedelta(hours=6))
        self.assertEqual((len(out), rm, cs2.status, cs2.removal_hold), (6, [], "ok", None))

    def test_shrink_then_removal_guard_does_not_bounce(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "GE Vernova")
        cs = dataclasses.replace(state.companies["GE Vernova"], listing_total=120)

        def shrunk():
            return R.SourceResult("GE Vernova", True, "ok", listing_complete=True, listing_total=40,
                                  listed={rows[3].job_id}, live={rows[3].job_id},
                                  closed={r.job_id for r in rows[:3]})
        _o, cs, rm = R.apply_result(rows, cs, shrunk(), NOW)
        self.assertEqual((rm, cs.detail), ([], R.SHRINK_DETAIL))
        t2 = NOW + R.REFRESH_CADENCE
        _o, cs, rm = R.apply_result(rows, cs, shrunk(), t2)
        self.assertEqual((rm, cs.detail, cs.removal_hold["at"]), ([], R.REMOVAL_DETAIL, R.stamp(t2)))
        out, cs, rm = R.apply_result(rows, cs, shrunk(), t2 + R.REFRESH_CADENCE)
        self.assertEqual((len(rm), len(out), cs.status, cs.listing_total), (3, 1, "ok", 40))

    def test_hold_state_round_trip_and_validation(self):
        config, _e, state, _t = env()
        hold = {"at": "2026-10-31T18:00+05:30", "ids": ["527740", "527741"]}
        held_state = dataclasses.replace(state, companies={
            **state.companies, "Siemens": dataclasses.replace(state.companies["Siemens"], removal_hold=hold)})
        data = json.loads(held_state.to_json())
        self.assertEqual(R.load_state(data, config).companies["Siemens"].removal_hold, hold)
        for bad in ({"at": "soon", "ids": ["527740"]}, {"at": hold["at"], "ids": []},
                    {"at": hold["at"], "ids": ["R1"]}, {"at": hold["at"], "ids": ["527741", "527740"]},
                    {"at": hold["at"], "ids": ["527740"], "x": 1}, ["527740"]):
            with self.subTest(bad):
                d = json.loads(held_state.to_json())
                d["companies"]["Siemens"]["removal_hold"] = bad
                with self.assertRaises(ValueError):
                    R.load_state(d, config)
        legacy = json.loads(state.to_json())
        for c in legacy["companies"].values():
            del c["removal_hold"], c["pulls"]       # state written before removal_hold and pulls existed
        self.assertTrue(all(cs.removal_hold is None for cs in R.load_state(legacy, config).companies.values()))

    def test_small_removal_is_allowed(self):
        _c, _e, state, _t = env()
        rows = rows_of(state, "Siemens")[:6]
        res = R.SourceResult("Siemens", True, "ok", closed={rows[0].job_id, rows[1].job_id},
                             live={r.job_id for r in rows[2:]})
        out, cs, removed = R.apply_result(rows, state.companies["Siemens"], res, NOW)
        self.assertEqual((len(out), len(removed), cs.status), (4, 2, "ok"))


# --------------------------------------------------------------------------- HTTP client

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.config, _e, _s, _t = env()

    def fetcher(self, transport, **kw):
        return R.Fetcher(self.config, 60, transport=transport, sleep=lambda _s: None, **kw)

    def test_url_allowlist(self):
        hosts = self.config.fetch_hosts
        self.assertIsNone(R.check_url("https://jobs.siemens.com/x", hosts))
        for bad in ("http://jobs.siemens.com/x", "https://jobs.siemens.com.evil.example/x",
                    "https://user" + "@jobs.siemens.com/x", "https://jobs.siemens.com:8443/x",
                    "https://evil.example/x", "ftp://jobs.siemens.com/x", "https://careers.gehealthcare.com/x",
                    "https://incareers-zs.icims.com/jobs/1", "https://jobs.siemens.com/a b"):
            self.assertIsNotNone(R.check_url(bad, hosts), bad)

    def test_disallowed_host_makes_no_request(self):
        t = FakeTransport()
        self.assertEqual(self.fetcher(t).request("https://evil.example/").error, "host-not-allowed")
        self.assertEqual(t.calls, [])

    def test_cross_host_redirect_is_blocked(self):
        for target in ("https://evil.example/x", "https://jobs.iqvia.com/x", "http://jobs.siemens.com/x"):
            with self.subTest(target):
                t = FakeTransport([("GET", "https://jobs.siemens.com/a", resp(302, "", headers=[("Location", target)]))])
                r = self.fetcher(t).request("https://jobs.siemens.com/a")
                self.assertEqual(r.error, "redirect-blocked")
                self.assertFalse(any(target in u for _m, u in t.calls))

    def test_same_host_redirect_is_followed(self):
        t = FakeTransport([("GET", "https://jobs.siemens.com/a", resp(302, "", headers=[("Location", "/b")])),
                           ("GET", "https://jobs.siemens.com/b", resp(200, "ok"))])
        r = self.fetcher(t).request("https://jobs.siemens.com/a")
        self.assertEqual((r.status, r.url, r.error), (200, "https://jobs.siemens.com/b", None))

    def test_redirect_loop_is_bounded(self):
        t = FakeTransport([("GET", "https://jobs.siemens.com/*", resp(302, "", headers=[("Location", "/loop")]))])
        self.assertEqual(self.fetcher(t).request("https://jobs.siemens.com/a").error, "too-many-redirects")

    def test_transient_errors_retry_at_most_three_times(self):
        t = FakeTransport([("GET", "https://jobs.siemens.com/a", resp(503, "busy"))])
        r = self.fetcher(t).request("https://jobs.siemens.com/a")
        self.assertEqual((r.status, t.count("/a")), (503, 3))
        t = FakeTransport([("GET", "https://jobs.siemens.com/a", [resp(503, "busy"), resp(200, "ok")])])
        self.assertEqual(self.fetcher(t).request("https://jobs.siemens.com/a").status, 200)

    def test_retry_after_is_honoured_and_capped(self):
        sleeps = []
        t = FakeTransport([("GET", "https://jobs.siemens.com/a",
                            [resp(429, "", headers=[("Retry-After", "7")]), resp(200, "ok")])])
        R.Fetcher(self.config, 60, transport=t, sleep=sleeps.append, rand=lambda: 0.0).request("https://jobs.siemens.com/a")
        self.assertIn(7.0, sleeps)
        t = FakeTransport([("GET", "https://jobs.siemens.com/a", resp(429, "", headers=[("Retry-After", "3600")]))])
        self.assertEqual(self.fetcher(t).request("https://jobs.siemens.com/a").status, 429)
        self.assertEqual(t.count("/a"), 1)

    def test_non_transient_errors_do_not_retry(self):
        t = FakeTransport([("GET", "https://jobs.siemens.com/a", resp(403, "no"))])
        self.fetcher(t).request("https://jobs.siemens.com/a")
        self.assertEqual(t.count("/a"), 1)

    def test_polite_per_host_delay(self):
        sleeps, clock = [], [0.0]
        t = FakeTransport([("GET", "https://jobs.siemens.com/*", resp(200, "ok"))])
        f = R.Fetcher(self.config, 60, transport=t, sleep=lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)),
                      clock=lambda: clock[0], rand=lambda: 0.0)
        f.request("https://jobs.siemens.com/a")
        f.request("https://jobs.siemens.com/b")
        self.assertTrue(sleeps and all(s >= 2.0 for s in sleeps))

    def test_budget_deadline_stops_requests(self):
        clock = [0.0]
        t = FakeTransport([("GET", "https://jobs.siemens.com/*", resp(200, "ok"))])
        f = R.Fetcher(self.config, 10, transport=t, sleep=lambda s: None, clock=lambda: clock[0])
        clock[0] = 11.0
        self.assertEqual(f.request("https://jobs.siemens.com/a").error, "deadline")

    def test_body_size_cap(self):
        t = FakeTransport([("GET", "https://jobs.siemens.com/a", (200, [("Content-Type", "text/html")],
                                                                   b"x" * (R.MAX_BYTES + 1)))])
        self.assertEqual(self.fetcher(t).request("https://jobs.siemens.com/a").error, "too-large-or-bad-encoding")

    def test_gzip_bomb_is_rejected(self):
        import gzip
        bomb = gzip.compress(b"\0" * (R.MAX_BYTES + 10))
        t = FakeTransport([("GET", "https://jobs.siemens.com/a",
                            (200, [("Content-Type", "text/html"), ("Content-Encoding", "gzip")], bomb))])
        self.assertEqual(self.fetcher(t).request("https://jobs.siemens.com/a").error, "too-large-or-bad-encoding")

    def test_robots_rules(self):
        t = FakeTransport([("GET", "https://jobs.siemens.com/*", resp(200, "ok"))],
                          robots=resp(200, "User-agent: *\nDisallow: /private\n", "text/plain"))
        f = self.fetcher(t)
        self.assertEqual(f.request("https://jobs.siemens.com/private/x").error, "robots")
        self.assertIsNone(f.request("https://jobs.siemens.com/public").error)
        self.assertEqual(t.count("robots.txt"), 1)          # cached per host
        self.assertEqual(t.count("/private"), 0)
        for robots, expect in ((resp(503, "x"), "robots"), (TimeoutError("x"), "robots"), (resp(404, "x"), None)):
            with self.subTest(robots):
                t = FakeTransport([("GET", "https://jobs.siemens.com/a", resp(200, "ok"))], robots=robots)
                self.assertEqual(self.fetcher(t).request("https://jobs.siemens.com/a").error, expect)

    def test_robots_rfc9309_matching(self):
        rules = R.RobotsRules("User-agent: *\nDisallow: /*?folderOffset=\nDisallow: /en_US/\n"
                              "Allow: /en_US/externaljobs/JobDetail/\nDisallow: /*.pdf$\n\n"
                              "User-agent: otherbot\nDisallow: /\n", R.ROBOTS_TOKEN)
        self.assertFalse(rules.allows("/en_US/externaljobs/SearchJobs/?folderOffset=6"))
        self.assertTrue(rules.allows("/en_US/externaljobs/JobDetail/527740"))   # longer Allow wins
        self.assertFalse(rules.allows("/en_US/other"))
        self.assertFalse(rules.allows("/files/a.pdf"))
        self.assertTrue(rules.allows("/files/a.pdf?x=1"))                       # $ anchors the end
        self.assertTrue(rules.allows("/robots.txt"))
        tie = R.RobotsRules("User-agent: *\nDisallow: /a\nAllow: /a\n", R.ROBOTS_TOKEN)
        self.assertTrue(tie.allows("/a"))                                        # Allow wins ties
        own = R.RobotsRules(f"User-agent: *\nAllow: /\n\nUser-agent: {R.ROBOTS_TOKEN}\nDisallow: /\n", R.ROBOTS_TOKEN)
        self.assertFalse(own.allows("/x"))                                       # our group overrides *

    def test_html_robots_is_deny(self):
        t = FakeTransport([("GET", "https://jobs.siemens.com/a", resp(200, "ok"))],
                          robots=resp(200, "<html><title>Just a moment...</title></html>"))
        self.assertEqual(self.fetcher(t).request("https://jobs.siemens.com/a").error, "robots")

    def test_truncated_gzip_is_rejected(self):
        import gzip
        cut = gzip.compress(b"hello world " * 1000)[:-12]
        t = FakeTransport([("GET", "https://jobs.siemens.com/a",
                            (200, [("Content-Type", "text/html"), ("Content-Encoding", "gzip")], cut))])
        self.assertEqual(self.fetcher(t).request("https://jobs.siemens.com/a").error, "too-large-or-bad-encoding")

    def test_cookies_stay_on_their_host(self):
        seen = {}

        def capture(host):
            def handler(url, _b):
                return resp(200, "ok", headers=[("Set-Cookie", f"sid_{host}=1; Path=/")])
            return handler
        t = FakeTransport([("GET", "https://jobs.siemens.com/*", capture("s")),
                           ("GET", "https://jobs.iqvia.com/*", capture("i"))])
        orig = t.__call__

        def spy(method, url, headers, body, timeout):
            seen.setdefault(url, headers.get("Cookie"))
            return orig(method, url, headers, body, timeout)
        f = self.fetcher(spy, cookies=True)
        f.request("https://jobs.siemens.com/a")
        f.request("https://jobs.iqvia.com/a")
        f.request("https://jobs.iqvia.com/b")
        self.assertEqual(seen["https://jobs.iqvia.com/b"], "sid_i=1")


# --------------------------------------------------------------------------- Siemens cadence

class SiemensTests(unittest.TestCase):
    def run_siemens(self, last_discovery, routes, cfg_overrides=None):
        config, excluded, state, _t = env()
        cfg = {**config.sources["Siemens"], **(cfg_overrides or {})}
        cs = dataclasses.replace(state.companies["Siemens"], last_discovery_attempt=last_discovery)
        t = FakeTransport(routes)
        f = R.Fetcher(config, 600, transport=t, sleep=lambda _s: None, cookies=True)
        res = R.refresh_siemens(cfg, rows_of(state, "Siemens"), f, NOW, cs, excluded["Siemens"], config)
        return res, t

    def test_liveness_every_run_discovery_at_most_daily(self):
        res, t = self.run_siemens("2026-10-31T09:23+05:30", live_siemens_routes())
        self.assertFalse(res.discovery_attempted)
        self.assertEqual(t.count("SearchJobs"), 0)
        self.assertEqual(t.count("JobDetail"), 23)
        self.assertEqual(len(res.live), 23)
        res, t = self.run_siemens("2026-10-30T17:59+05:30", live_siemens_routes())
        self.assertTrue(res.discovery_attempted)
        self.assertGreater(t.count("SearchJobs"), 0)

    def test_discovery_attempt_is_recorded_even_when_held(self):
        _c, _e, state, _t = env()
        res = R.held("Siemens", "x", discovery_attempted=True)
        _rows, cs, _rm = R.apply_result(rows_of(state, "Siemens"), state.companies["Siemens"], res, NOW)
        self.assertEqual(cs.last_discovery_attempt, R.stamp(NOW))

    def test_discovery_is_capped(self):
        page_ids = iter(range(600000, 700000, 6))

        def list_page(url, _b):
            start = next(page_ids)
            return resp(200, siemens_list_page(range(start, start + 6)))
        routes = [("GET", R.SIEMENS_SEARCH + "/*", list_page), ("POST", R.SIEMENS_SEARCH, resp(200, "<html>sorted</html>")),
                  *live_siemens_routes()]
        res, t = self.run_siemens(None, routes, {"max_list_pages": 4, "probe_every_pages": 2, "max_new_details": 3})
        self.assertFalse(res.listing_complete)
        self.assertIn("page cap reached", res.detail)
        list_gets = sum(1 for m, u in t.calls if m == "GET" and "SearchJobs" in u)
        self.assertEqual(list_gets, 1 + 4)                     # session bootstrap + capped pages
        new_details = sum(1 for _m, u in t.calls if "JobDetail/6" in u)
        self.assertLessEqual(new_details, 2 + 3)               # probes + capped new details
        self.assertLessEqual(t.count("JobDetail"), 23 + 2 + 3)

    def test_unordered_listing_is_not_authoritative(self):
        dates = iter(["10-Oct-2026", "20-Oct-2026"])
        routes = [("GET", R.SIEMENS_SEARCH + "/*", lambda u, _b: resp(200, siemens_list_page(range(
                      int(re.search(r"folderOffset=(\d+)", u).group(1)) + 610000,
                      int(re.search(r"folderOffset=(\d+)", u).group(1)) + 610006)))),
                  ("POST", R.SIEMENS_SEARCH, resp(200, "<html>sorted</html>")),
                  ("GET", "https://jobs.siemens.com/en_US/externaljobs/JobDetail/61*",
                   lambda u, _b: resp(200, siemens_detail_page(u.rsplit("/", 1)[-1], next(dates, "01-Oct-2026")))),
                  *live_siemens_routes()]
        res, _t = self.run_siemens(None, routes, {"probe_every_pages": 1, "max_new_details": 0})
        self.assertFalse(res.listing_complete)
        self.assertIn("not date-ordered", res.detail)


# --------------------------------------------------------------------------- IQVIA discovery

class IqviaDiscoveryTests(unittest.TestCase):
    QUALIFYING = "R1500001"          # lowest ID, so it sorts below every non-qualifying result
    DETAIL = "/wday/cxs/iqvia/IQVIA/job/"

    def setUp(self):
        self.config, _e, _s, _t = env()
        self.cfg = {**self.config.sources["IQVIA"], "max_new_details": 20}
        self.titles = {f"R16{i:05d}": "Clinical Data Associate" for i in range(45)}
        self.titles[self.QUALIFYING] = "Clinical Data Coordinator"

    def routes(self):
        posts = [{"title": t, "externalPath": f"/job/Kochi-India/Role_{j}", "locationsText": "Kochi, India",
                  "postedOn": "Posted Yesterday"} for j, t in sorted(self.titles.items(), reverse=True)]

        def listing(_url, body):
            q = json.loads(body)
            return jresp({"total": len(posts), "jobPostings": posts[q["offset"]: q["offset"] + q["limit"]]})

        def detail(url, _b):
            jid = url.rsplit("_", 1)[-1]
            years = "At least 1 year" if jid == self.QUALIFYING else "Minimum 5 years"
            return wd_detail(jid, title=self.titles[jid], location="Kochi, India", endDate="2026-11-20",
                             jobDescription=f"<p>{years} of experience in clinical data management.</p>")
        return [("POST", R.IQVIA_API + "/jobs", listing),
                ("GET", R.IQVIA_API + "/job/*", detail),
                ("GET", "https://jobs.iqvia.com/en/jobs/*",
                 lambda url, _b: resp(200, f"<html><title>Job</title><body>{url.rsplit('/', 1)[-1]}</body></html>"))]

    def run_once(self, rows, cs, routes=None):
        t = FakeTransport(routes or self.routes())
        f = R.Fetcher(self.config, 600, transport=t, sleep=lambda _s: None)
        res = R.refresh_iqvia(self.cfg, rows, f, NOW, cs, frozenset(), self.config)
        out, new_cs, _rm = R.apply_result(rows, cs, res, NOW)
        new_details = sum(1 for m, u in t.calls if m == "GET" and self.DETAIL in u
                          and u.rsplit("_", 1)[-1] not in {r.job_id for r in rows})
        return out, new_cs, res, new_details

    def test_qualifying_role_below_many_non_qualifying_is_reached_with_bounded_requests(self):
        rows, cs = [], R.CompanyState("seed", "manual snapshot")
        per_run = []
        for _run in range(5):
            rows, cs, res, new_details = self.run_once(rows, cs)
            per_run.append(new_details)
            self.assertLessEqual(new_details, self.cfg["max_new_details"])
            self.assertEqual(res.counts["new_checked"], new_details)
            if rows:
                break
        self.assertEqual(per_run, [20, 20, 6])
        self.assertEqual([r.job_id for r in rows], [self.QUALIFYING])
        self.assertEqual(set(cs.screened), set(self.titles) - {self.QUALIFYING})
        self.assertTrue(all(R.screened_parts(v)[1] for v in cs.screened.values()))   # fingerprinted
        _rows, _cs, res, new_details = self.run_once(rows, cs)
        self.assertEqual((new_details, res.counts["unscreened"]), (0, 0))     # nothing left to re-check

    def test_changed_listing_record_is_screened_again(self):
        rows, cs = [], R.CompanyState("seed", "manual snapshot")
        for _run in range(3):
            rows, cs, _res, _n = self.run_once(rows, cs)
        self.titles["R1600007"] = "Clinical Data Associate II"
        _rows, _cs, res, new_details = self.run_once(rows, cs)
        self.assertEqual((new_details, res.counts["unscreened"]), (1, 1))

    def test_throttled_detail_stops_screening_without_recording(self):
        routes = self.routes()
        routes[1] = ("GET", R.IQVIA_API + "/job/*", resp(403, "rate limited"))
        _rows, cs, res, new_details = self.run_once([], R.CompanyState("seed", "manual snapshot"), routes)
        self.assertEqual((new_details, res.counts["new_checked"], cs.screened), (1, 1, {}))
        # An unreadable detail (e.g. an HTML error page) uses budget but is never recorded as screened.
        routes[1] = ("GET", R.IQVIA_API + "/job/*", resp(200, "<html><title>Workday</title></html>"))
        _rows, cs, res, new_details = self.run_once([], R.CompanyState("seed", "manual snapshot"), routes)
        self.assertEqual((new_details, cs.screened), (self.cfg["max_new_details"], {}))

    def test_ttl_prunes_fingerprinted_entries(self):
        config, excluded, state, _t = env()
        old = (TODAY - dt.timedelta(days=R.SCREENED_TTL_DAYS + 1)).isoformat()
        cs = dataclasses.replace(state.companies["IQVIA"], screened={
            "R1600001": R.screened_entry(TODAY, "0123456789ab"), "R1600002": old + "#0123456789ab"})
        state = dataclasses.replace(state, companies={**state.companies, "IQVIA": cs})
        after, _o = R.run_refresh(state, config, excluded, NOW, None)
        self.assertIn("R1600001", after.companies["IQVIA"].screened)
        self.assertNotIn("R1600002", after.companies["IQVIA"].screened)


# --------------------------------------------------------------------------- misc

class ChangeDetectionTests(unittest.TestCase):
    def test_identical_state_is_not_meaningful(self):
        config, _e, state, _t = env()
        self.assertFalse(R.meaningful(state, R.load_state(json.loads(state.to_json()), config)))

    def test_advancing_run_timestamps_are_meaningful(self):
        _c, _e, state, _t = env()
        state.checked_at = "2026-10-31T05:53+05:30"
        state.companies["Siemens"] = dataclasses.replace(
            state.companies["Siemens"], status="ok", last_success="2026-10-31T05:53+05:30",
            last_attempt="2026-10-31T05:53+05:30")
        when = "2026-10-31T11:53+05:30"        # same IST day as every timestamp above

        def siemens(**kw):
            return dataclasses.replace(state, companies={
                **state.companies, "Siemens": dataclasses.replace(state.companies["Siemens"], **kw)})
        cases = {"checked_at": dataclasses.replace(state, checked_at=when),
                 "last_attempt": siemens(last_attempt=when),
                 "last_success": siemens(last_success=when),
                 "last_discovery_attempt": siemens(last_discovery_attempt=when),
                 "rows": dataclasses.replace(state, jobs=state.jobs[1:])}
        for name, after in cases.items():
            with self.subTest(name):
                self.assertTrue(R.meaningful(state, after))

    def test_offline_run_is_not_meaningful(self):
        config, excluded, state, _t = env()
        state.checked_at = "2026-10-31T05:53+05:30"
        after, _o = R.run_refresh(state, config, excluded, NOW, None)
        self.assertEqual(after.checked_at, state.checked_at)
        self.assertFalse(R.meaningful(state, after))

    def test_same_day_success_is_kept_for_a_later_held_run(self):
        """Regression: a 11:53 success must be committed so a 17:53 hold reads it, not the 05:53 one."""
        config, _e, state, _t = env()
        ist = lambda h, m: dt.datetime(2026, 10, 31, h, m, tzinfo=R.IST)   # noqa: E731
        state.checked_at = R.stamp(ist(5, 53))
        state.companies["Siemens"] = dataclasses.replace(
            state.companies["Siemens"], status="ok", detail="ok",
            last_success=R.stamp(ist(5, 53)), last_attempt=R.stamp(ist(5, 53)))
        # 11:53 run succeeds; nothing else changes.
        midday = dataclasses.replace(state, checked_at=R.stamp(ist(11, 53)), companies={
            **state.companies, "Siemens": dataclasses.replace(state.companies["Siemens"],
                                                              last_success=R.stamp(ist(11, 53)),
                                                              last_attempt=R.stamp(ist(11, 53)))})
        self.assertTrue(R.meaningful(state, midday))         # so the workflow commits it
        committed = R.load_state(json.loads(midday.to_json()), config)
        # 17:53 run is held; apply_result keeps last_success from whatever state was committed.
        evening = ist(17, 53)
        rows = rows_of(committed, "Siemens")
        _r, cs, _x = R.apply_result(rows, committed.companies["Siemens"], R.held("Siemens", "x"), evening)
        self.assertEqual((cs.status, cs.last_success), ("stale", R.stamp(ist(11, 53))))
        self.assertEqual(R.classify_source(cs.status, cs.last_success, cs.last_attempt, evening), "degraded")
        # Had the 11:53 success been dropped, the same hold would read 05:53 and wrongly show STALE.
        _r, old_cs, _x = R.apply_result(rows, state.companies["Siemens"], R.held("Siemens", "x"), evening)
        self.assertEqual(R.classify_source(old_cs.status, old_cs.last_success, old_cs.last_attempt, evening),
                         "stale")
        held = dataclasses.replace(committed, checked_at=R.stamp(evening),
                                   companies={**committed.companies, "Siemens": cs})
        self.assertTrue(R.meaningful(committed, held))
        self.assertEqual(R.stale_labels(held, config).get("Siemens"), "cached data")


class ConfigTests(unittest.TestCase):
    def load(self, mutate):
        data = R.read_json(os.path.join(ROOT, R.CONFIG_PATH))
        mutate(data)
        return R.load_config(data)

    def test_repo_config_is_valid(self):
        config = self.load(lambda d: None)
        self.assertEqual(config.companies, ("Siemens", "JPMorganChase", "GE HealthCare", "GE Vernova", "IQVIA", "ZS"))
        self.assertEqual(config.fetch_hosts, {"jobs.siemens.com", "careers.gevernova.com", "jobs.iqvia.com",
                                              "iqvia.wd1.myworkdayjobs.com", "jpmc.fa.oraclecloud.com",
                                              "gehc.wd5.myworkdayjobs.com", "jobs.zs.com"})
        self.assertEqual({c: config.sources[c]["mode"] for c in config.companies}, {
            "Siemens": "siemens_avature_html", "JPMorganChase": "jpm_oracle_ce", "GE HealthCare": "gehc_workday_cxs",
            "GE Vernova": "gevernova_paradox_api", "IQVIA": "iqvia_workday_cxs", "ZS": "zs_jibe_api"})
        self.assertNotIn("careers.gehealthcare.com", config.link_hosts)     # stale Phenom aliases stay out

    def test_endpoints_stay_on_company_hosts(self):
        for company, urls in {"JPMorganChase": (R.JPM_SEARCH, R.JPM_DETAIL, R.JPM_FRONT),
                              "GE HealthCare": (R.GEHC_API, R.GEHC_FRONT),
                              "ZS": (R.ZS_LIST, R.ZS_FRONT, R.ZS_DETAIL)}.items():
            for url in urls:
                self.assertIsNone(R.check_url(url.replace("{", "").replace("}", ""),
                                              R.COMPANY_SPECS[company].hosts), url)

    def test_config_rejects_unsafe_changes(self):
        def static(name):
            def mutate(d):
                for i, e in enumerate(d["companies"]):
                    if e["company"] == name:
                        d["companies"][i] = {"company": name, "mode": "static"}
            return mutate
        mutations = {
            "extra host": lambda d: d["hosts"].__setitem__("evil.example", {"fetch": True, "robots": False,
                                                                            "min_delay_s": 1.0}),
            "static fetch": static("ZS"),            # a static source's host must not stay fetchable
            "unfetched live source": lambda d: d["hosts"]["jpmc.fa.oraclecloud.com"].__setitem__("fetch", False),
            "phenom alias host": lambda d: d["hosts"].__setitem__("careers.gehealthcare.com", {
                "fetch": True, "robots": True, "min_delay_s": 1.0}),
            "fast zs": lambda d: d["hosts"]["jobs.zs.com"].__setitem__("min_delay_s", 1.0),
            "zs without robots": lambda d: d["hosts"]["jobs.zs.com"].__setitem__("robots", False),
            "wrong mode for jpm": lambda d: d["companies"][1].__setitem__("mode", "gehc_workday_cxs"),
            "jpm search terms": lambda d: d["companies"][1].__setitem__("search_terms", ["Bengaluru"]),
            "gehc huge total": lambda d: d["companies"][2].__setitem__("max_total", 50000),
            "zs too many checks": lambda d: d["companies"][5].__setitem__("max_new_checks", 100),
            "fast siemens": lambda d: d["hosts"]["jobs.siemens.com"].__setitem__("min_delay_s", 1.0),
            "bad mode": lambda d: d["companies"][0].__setitem__("mode", "iqvia_workday_cxs"),
            "sub-daily discovery": lambda d: d["companies"][0].__setitem__("discovery_interval_h", 6),
            "url in config": lambda d: d["companies"][0].__setitem__("url", "https://evil.example"),
            "huge cap": lambda d: d["companies"][0].__setitem__("max_new_details", 2000),
        }
        for name, mutate in mutations.items():
            with self.subTest(name), self.assertRaises(R.ConfigError):
                self.load(mutate)

    def test_excluded_ids_are_ids_only(self):
        config = self.load(lambda d: None)
        R.load_excluded(TEST_EXCLUDED, config)
        with self.assertRaises(R.ConfigError):
            R.load_excluded({"schema": 1, "ids": {"IQVIA": ["R1590001 applied"]}}, config)

    def test_excluded_rows_are_pruned(self):
        config, excluded, state, _t = env()
        excluded = {**excluded, "ZS": frozenset({"24123"})}
        after, _o = R.run_refresh(state, config, excluded, NOW, None)
        self.assertNotIn("24123", {r.job_id for r in after.jobs})


class BoundaryScanTests(unittest.TestCase):
    def copy_repo(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        dst = os.path.join(tmp, "repo")
        shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "dist", "__pycache__",
                                                                 "excluded_public_ids*.json"))
        return dst

    def test_scan_flags_private_exclusion_files(self):
        root = self.copy_repo()
        for rel in ("config/excluded_public_ids.json", R.EXCLUDED_LOCAL_PATH):
            with open(os.path.join(root, rel), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(TEST_EXCLUDED))
        problems = R.scan_repo(root)      # no .git in the copy: nothing is ignored, so both are flagged
        self.assertIn("config/excluded_public_ids.json: file not in allowlist", problems)
        self.assertIn(R.EXCLUDED_LOCAL_PATH + ": file not in allowlist", problems)

    def test_repository_is_clean(self):
        self.assertEqual(R.scan_repo(ROOT), [])

    def test_scan_flags_forbidden_content(self):
        samples = ["am" + "azon", "a" + "toz", "mid" + "way", "C:" + "\\Users\\someone\\x", "D:" + "/Users/x",
                   "/ho" + "me/someone/x", "applic" + "ations.csv", "skil" + "ls.yaml", "jeev" + "an",
                   "ghp" + "_" + "a" * 30, "AK" + "IA" + "A" * 16, "name" + "@" + "example.com",
                   "Author" + "ization: Bearer " + "a" * 20, "Set-Coo" + "kie: x=1", "NOT_" + "CLAIMED",
                   "." + "kiro" + "/steering"]
        for sample in samples:
            with self.subTest(sample[:6]):
                root = self.copy_repo()
                with open(os.path.join(root, "tests", "fixtures", "leak.json"), "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"x": sample}))
                problems = R.scan_repo(root)
                self.assertTrue(any("leak.json: forbidden pattern" in p for p in problems), problems)
                self.assertFalse(any(sample in p for p in problems))   # never echo content

    def test_scan_flags_unlisted_files_and_index_drift(self):
        root = self.copy_repo()
        os.makedirs(os.path.join(root, "snapshots"))
        with open(os.path.join(root, "snapshots", "x.csv"), "w", encoding="utf-8") as fh:
            fh.write("a,b\n")
        with open(os.path.join(root, "index.html"), "a", encoding="utf-8", newline="\n") as fh:
            fh.write("<!-- drift -->\n")
        problems = R.scan_repo(root)
        self.assertIn("snapshots/x.csv: file not in allowlist", problems)
        self.assertIn("index.html: does not match render(state/jobs.json)", problems)

    def test_scan_flags_fixture_urls_off_the_allowlist(self):
        for url in ("https://careers.gehealthcare.com/global/en/job/R4048338", "https://incareers-zs.icims.com/jobs/1"):
            with self.subTest(url):
                root = self.copy_repo()
                with open(os.path.join(root, "tests", "fixtures", "extra.json"), "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"responses": [{"method": "GET", "url": url}]}))
                self.assertIn("tests/fixtures/extra.json: URL outside link-host allowlist", R.scan_repo(root))

    def test_state_rejects_off_allowlist_urls(self):
        config, _e, state, _t = env()
        data = json.loads(state.to_json())
        data["jobs"][0]["canonical_url"] = "https://evil.example/en_US/externaljobs/JobDetail/527740"
        with self.assertRaises(ValueError):
            R.load_state(data, config)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = R.read_text(os.path.join(ROOT, ".github", "workflows", "refresh.yml"))

    def block(self, job: str) -> str:
        m = re.search(rf"^  {job}:\n(.*?)(?=^  [a-z]+:\n|\Z)", self.text, re.S | re.M)
        self.assertIsNotNone(m, job)
        return m.group(1)

    def test_triggers(self):
        self.assertRegex(self.text, r'(?m)^  schedule:\n    - cron: "23 \*/6 \* \* \*"')
        self.assertEqual(R.REFRESH_CADENCE, dt.timedelta(hours=6))   # removal-hold confirmation spacing
        self.assertRegex(self.text, r"(?m)^  workflow_dispatch:\s*$")
        self.assertNotRegex(self.text, r"(?m)^  (push|pull_request|pull_request_target):")

    def test_least_privilege(self):
        self.assertRegex(self.text, r"(?m)^permissions: \{\}$")
        refresh, deploy = self.block("refresh"), self.block("deploy")
        self.assertRegex(refresh, r"permissions:\n      contents: write[^\n]*\n    steps:")
        self.assertRegex(deploy, r"permissions:\n      pages: write\n      id-token: write\n    environment:")
        self.assertEqual(len(re.findall(r"(?m)^ +permissions:", self.text)), 2)
        self.assertIn("persist-credentials: false", refresh)

    def test_concurrency_timeouts_runner(self):
        self.assertRegex(self.text, r"(?m)^concurrency:\n  group: job-ledger-refresh\n  cancel-in-progress: false$")
        timeouts = [int(x) for x in re.findall(r"timeout-minutes: (\d+)", self.text)]
        self.assertEqual(len(timeouts), 2)
        self.assertTrue(all(t <= 25 for t in timeouts))
        self.assertEqual(re.findall(r"runs-on: (\S+)", self.text), ["ubuntu-24.04", "ubuntu-24.04"])

    def test_actions_are_pinned_to_reviewed_shas(self):
        uses = re.findall(r"uses: (\S+)", self.text)
        self.assertEqual(sorted(uses), sorted([
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9",
            "actions/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d",
            "actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346",
        ]))
        # Each pin carries the release tag it was verified against (git ls-remote --tags).
        tags = dict(re.findall(r"uses: (\S+)\s+# (v\d+\.\d+\.\d+)\b", self.text))
        self.assertEqual(tags, {
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1": "v7.0.1",
            "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9": "v5.0.0",
            "actions/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d": "v6.0.0",
            "actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346": "v5.0.1",
        })

    def test_step_order_and_safe_push(self):
        refresh = self.block("refresh")
        self.assertIn("if: github.event_name == 'schedule' ||", refresh)
        self.assertIn('echo "::add-mask::${auth}"', refresh)
        order = ["unittest discover", "refresh.py check", "refresh.py refresh", "refresh.py scan",
                 "render --out dist/index.html",
                 "git add -- state/jobs.json index.html", "upload-pages-artifact"]
        positions = [refresh.index(s) for s in order]
        self.assertEqual(positions, sorted(positions))
        self.assertNotRegex(self.text, r"--force|-f\b|\+HEAD|reset --hard")
        self.assertIn('push origin "HEAD:refs/heads/${GITHUB_REF_NAME}"', refresh)
        for line in refresh.splitlines():   # expressions only in if:/env:, never inlined into run scripts
            if "${{" in line:
                self.assertRegex(line, r"^\s+(if:|[A-Z_]+: \$\{\{)", line)
        self.assertIn("needs: refresh", self.block("deploy"))


def temp_repo(test: unittest.TestCase, state, template, config) -> str:
    """A throwaway repo root with public inputs only (no private exclusion file)."""
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    for rel in (R.CONFIG_PATH, R.TEMPLATE_PATH):
        os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
        shutil.copy(os.path.join(ROOT, rel), os.path.join(tmp, rel))
    R.write_atomic(os.path.join(tmp, R.STATE_PATH), state.to_json())
    R.write_atomic(os.path.join(tmp, R.INDEX_PATH), R.render(state, template, config))
    return tmp


def run_main(argv, environ):
    """Run the CLI with an exact EXCLUDED_PUBLIC_IDS_JSON setting; returns (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    saved = os.environ.pop(R.EXCLUDED_ENV, None)
    try:
        if environ is not None:
            os.environ[R.EXCLUDED_ENV] = environ
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = R.main(argv)
    finally:
        os.environ.pop(R.EXCLUDED_ENV, None)
        if saved is not None:
            os.environ[R.EXCLUDED_ENV] = saved
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- corrected static sources

GEHC_WD = "https://gehc.wd5.myworkdayjobs.com/GEHC_ExternalSite/job/"
GEHC_URLS = {
    "R4048338": GEHC_WD + "Bengaluru/Graduate-Engineer-Trainee_R4048338",
    "R4046068": GEHC_WD + "IND36-01-Bengaluru-Kalyani-Platina-Magnolia/Graduate-Engineering-Trainee_R4046068-1",
    "R4048320": GEHC_WD + "Bengaluru/Graduate-Engineering-Trainee_R4048320-1",
    "R4047718": GEHC_WD + "Bengaluru/Graduate-Engineer-Trainee_R4047718-1",
    "R4047774": GEHC_WD + "IND19-01-Bengaluru-EPIP-122-Phase-II/Mechanical-Engineer_R4047774",
}
GEHC_END_DATES = {"R4048338": "2026-11-09", "R4046068": None, "R4048320": "2026-11-06",
                  "R4047718": "2026-11-06", "R4047774": None}   # Workday endDate (exclusive)
JPMC_CLOSING = {   # Oracle ExternalPostedEndDate calendar dates; authoritative, never shifted
    "210804767": "2026-11-27", "210804676": "2026-11-06", "210706687": "2026-11-06",
    "210803733": "2026-11-12", "210804246": "2026-11-20", "210804276": "2026-11-03",
    "210804686": "2026-11-27", "210798869": "2026-11-06", "210802418": "2026-11-06",
    "210799626": None, "210776625": None, "210796266": "2026-11-07", "210796619": "2026-11-13",
    "210800887": "2026-11-20", "210801652": "2026-11-13",
}


def closing_filter(rows, value):
    """Python mirror of the page's closing filter (match(): daysToClose(r[9]) against #closing)."""
    out = []
    for r in rows:
        k = None if r[9] is None else (dt.date.fromisoformat(r[9]) - TODAY).days
        if k is None or k < 0 or (value != "k" and k > int(value)):
            continue
        out.append(r[1])
    return out


class StaticSourceTests(unittest.TestCase):
    """Exact rows, URLs and closing dates of the corrected static sources, pinned to a frozen copy
    of the 2026-10-31 state so the checks survive rows expiring from the live state/jobs.json."""

    @classmethod
    def setUpClass(cls):
        cls.config, cls.state, cls.template = frozen_env()

    def test_frozen_state_is_the_reviewed_snapshot(self):
        self.assertEqual(self.state.as_of, TODAY)
        self.assertEqual(len(self.state.jobs), 50)

    def by_id(self, company):
        return {r.job_id: r for r in rows_of(self.state, company)}

    def test_ge_healthcare_uses_live_workday_urls(self):
        rows = self.by_id("GE HealthCare")
        self.assertEqual({j: r.canonical_url for j, r in rows.items()}, GEHC_URLS)
        self.assertEqual(self.config.link_hosts & {"careers.gehealthcare.com"}, set())
        stale = dataclasses.replace(rows["R4048338"],
                                    canonical_url="https://careers.gehealthcare.com/global/en/job/R4048338")
        with self.assertRaises(ValueError):
            R.validate_row(stale, self.config)
        wrong = dataclasses.replace(rows["R4048338"], canonical_url=GEHC_URLS["R4048320"])
        with self.assertRaises(ValueError):             # URL must carry the row's own ID
            R.validate_row(wrong, self.config)

    def test_ge_healthcare_closing_dates_are_workday_exclusive(self):
        rows = self.by_id("GE HealthCare")
        self.assertEqual({j: r.closing_date for j, r in rows.items()},
                         {j: R.workday_closing(e) for j, e in GEHC_END_DATES.items()})
        self.assertEqual(rows["R4048338"].closing_date, "2026-11-08")
        self.assertEqual(rows["R4048320"].closing_date, "2026-11-05")
        self.assertEqual(rows["R4047718"].closing_date, "2026-11-05")

    def test_jpmc_closing_dates_follow_oracle_end_date(self):
        rows = self.by_id("JPMorganChase")
        self.assertEqual({j: r.closing_date for j, r in rows.items()}, JPMC_CLOSING)
        self.assertTrue(all(r.liveness == "live" for r in rows.values()))
        # Oracle timestamps keep their calendar date (no exclusive-end shift).
        self.assertEqual(R.generic_closing("2026-11-03T00:00:00+00:00"), "2026-11-03")

    def test_zs_row_unchanged(self):
        rows = self.by_id("ZS")
        self.assertEqual(list(rows), ["24123"])
        self.assertEqual(rows["24123"].canonical_url, "https://jobs.zs.com/jobs/24123")
        self.assertIsNone(rows["24123"].closing_date)
        self.assertEqual(rows["24123"].liveness, "live")

    def test_static_closing_dates_render_and_filter(self):
        doc = R.render(self.state, self.template, self.config)
        rows = json.loads(re.search(r"\nconst jobs=(\[\n.*?\n\]);\n", doc, re.S).group(1))
        rendered = {(r[0], r[1]): r[9] for r in rows}
        for r in self.state.jobs:
            self.assertEqual(rendered[(r.company, r.job_id)], r.closing_date)
        static = [r for r in rows if r[0] in ("GE HealthCare", "JPMorganChase", "ZS")]
        self.assertEqual(closing_filter(static, "3"), ["210804276"])                  # Nov 3
        self.assertEqual(sorted(closing_filter(static, "7")), sorted(
            ["210804676", "210706687", "210804276", "210798869", "210802418", "210796266",
             "R4048320", "R4047718"]))
        self.assertIn("R4048338", closing_filter(static, "14"))                       # Nov 8
        self.assertEqual(len(closing_filter(static, "k")), 16)
        self.assertNotIn("24123", closing_filter(static, "k"))


class ProductionStateTests(unittest.TestCase):
    """The committed state/jobs.json is checked for shape and safe links only: rows expire and get
    removed in normal operation, so no exact counts, IDs or dates are asserted here."""

    @classmethod
    def setUpClass(cls):
        cls.config, cls.state, cls.template = R.load_all(ROOT)   # load_state validates every row

    def test_rows_are_unique_canonical_and_allowlisted(self):
        keys = [(r.company, r.job_id) for r in self.state.jobs]
        self.assertEqual(len(keys), len(set(keys)))
        for r in self.state.jobs:
            with self.subTest(r.job_id):
                m = R.COMPANY_SPECS[r.company].url_re.fullmatch(r.canonical_url)
                self.assertIsNotNone(m)
                self.assertEqual(m.group(1), r.job_id)
                self.assertIsNone(R.check_url(r.canonical_url, R.COMPANY_SPECS[r.company].hosts))

    def test_companies_follow_config(self):
        self.assertEqual(tuple(self.state.companies), self.config.companies)
        for c, cs in self.state.companies.items():
            self.assertEqual(cs.status == "static", self.config.sources[c]["mode"] == "static", c)

    def test_state_round_trips(self):
        again = R.load_state(json.loads(self.state.to_json()), self.config)
        self.assertEqual(again.to_json(), self.state.to_json())


# --------------------------------------------------------------------------- private exclusions

class PrivateExclusionTests(unittest.TestCase):
    MALFORMED = {
        "empty": "",
        "blank": "   \n",
        "not json": "{R1590001 private-marker",
        "no ids": '{"schema": 1}',
        "wrong schema": '{"schema": 2, "ids": {}}',
        "text in id": '{"schema": 1, "ids": {"IQVIA": ["R1590001 private-marker"]}}',
        "unknown company": '{"schema": 1, "ids": {"private-marker Co": ["1"]}}',
        "list": '["R1590001"]',
        "too large": json.dumps({"schema": 1, "ids": {"IQVIA": ["R1590001"] * 8000}}),
    }

    def setUp(self):
        self.config, _e, self.state, self.template = env()

    def test_env_var_wins_over_local_file(self):
        root = temp_repo(self, self.state, self.template, self.config)
        R.write_atomic(os.path.join(root, R.EXCLUDED_LOCAL_PATH), json.dumps(
            {"schema": 1, "ids": {"ZS": ["24123"]}}))
        got = R.read_excluded(root, self.config, {R.EXCLUDED_ENV: json.dumps(TEST_EXCLUDED)})
        self.assertEqual(got["IQVIA"], {"R1590001"})
        self.assertEqual(got["ZS"], frozenset())
        got = R.read_excluded(root, self.config, {})
        self.assertEqual(got["ZS"], {"24123"})
        self.assertEqual(got["IQVIA"], frozenset())

    def test_no_source_fails_closed(self):
        root = temp_repo(self, self.state, self.template, self.config)
        with self.assertRaises(R.ConfigError):
            R.read_excluded(root, self.config, {})

    def test_malformed_input_fails_closed_without_echo(self):
        root = temp_repo(self, self.state, self.template, self.config)
        for name, raw in self.MALFORMED.items():
            with self.subTest(name):
                with self.assertRaises(R.ConfigError) as ctx:
                    R.read_excluded(root, self.config, {R.EXCLUDED_ENV: raw})
                msg = str(ctx.exception)
                self.assertNotIn("private-marker", msg)
                self.assertNotIn("R1590001", msg)

    def test_malformed_secret_aborts_refresh_before_any_write(self):
        root = temp_repo(self, self.state, self.template, self.config)
        before = {p: R.read_text(os.path.join(root, p)) for p in (R.STATE_PATH, R.INDEX_PATH)}
        gh_out, summary = os.path.join(root, "gh_output"), os.path.join(root, "summary.md")
        cases = {k: v for k, v in self.MALFORMED.items() if k != "too large"}   # Windows caps env vars at 32 KiB
        for name, raw in [*cases.items(), ("unset", None)]:
            with self.subTest(name):
                code, out, err = run_main(["--root", root, "refresh", "--offline", "--now",
                                           "2026-11-01T08:00+05:30", "--github-output", gh_out,
                                           "--summary", summary], raw)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("refresh: error: excluded:", err)
                self.assertNotIn("private-marker", err)
                self.assertNotIn("R1590001", err)
                for p, text in before.items():
                    self.assertEqual(R.read_text(os.path.join(root, p)), text)
                self.assertFalse(os.path.exists(gh_out) or os.path.exists(summary))

    def test_excluded_ids_never_reach_logs_state_or_index(self):
        # A row that should be hidden is already in state and also appears in the live listings.
        hidden = R.Row(company="IQVIA", job_id="R1590001", title="Clin Data Spec", experience_text="Not stated",
                       experience_band="n", location="Kochi", canonical_url="https://jobs.iqvia.com/en/jobs/R1590001-0",
                       posted_date="2026-10-28", closing_date=None, first_seen="2026-10-31",
                       last_seen="2026-10-31")
        state = dataclasses.replace(self.state, jobs=R.sort_rows([*self.state.jobs, hidden], self.config))
        root = temp_repo(self, state, self.template, self.config)
        summary = os.path.join(root, "summary.md")
        code, out, err = run_main(["--root", root, "refresh", "--fixtures", os.path.join(FIXTURES, "dry_run.json"),
                                   "--now", "2026-10-31T18:00+05:30", "--summary", summary],
                                  json.dumps(TEST_EXCLUDED))
        self.assertEqual(code, 0, err)
        with open(summary, encoding="utf-8") as fh:
            published = [out, err, fh.read()]
        published += [R.read_text(os.path.join(root, p)) for p in (R.STATE_PATH, R.INDEX_PATH)]
        self.assertNotIn("R1590001", {r.job_id for r in R.load_all(root)[1].jobs})
        for jid in ("R1590001", "500001"):
            for text in published:
                self.assertNotIn(jid, text)

    def test_real_private_ids_absent_from_public_files(self):
        path = os.path.join(ROOT, R.EXCLUDED_LOCAL_PATH)
        if not os.path.exists(path):
            self.skipTest("no local private exclusion file (expected in CI)")
        private = {i for ids in R.read_excluded(ROOT, R.load_all(ROOT)[0], {}).values() for i in ids}
        files = R._repo_files(ROOT)
        self.assertNotIn(R.EXCLUDED_LOCAL_PATH, files)       # git-ignored, never listed
        for rel in files:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                text = fh.read()
            for jid in private:
                self.assertIsNone(re.search(rf"(?<![A-Za-z0-9]){jid}(?![0-9])", text), rel)

    def test_ignore_rules_and_allowlist(self):
        ignore = R.read_text(os.path.join(ROOT, ".gitignore")).splitlines()
        self.assertIn(R.EXCLUDED_LOCAL_PATH, ignore)
        self.assertIn("config/excluded_public_ids.json", ignore)
        self.assertNotIn("config/excluded_public_ids.json", R.ALLOWED_FILES)
        self.assertNotIn(R.EXCLUDED_LOCAL_PATH, R.ALLOWED_FILES)

    def test_workflow_passes_secret_only_to_refresh_step(self):
        text = R.read_text(os.path.join(ROOT, ".github", "workflows", "refresh.yml"))
        self.assertEqual(text.count("secrets."), 1)
        step = re.search(r"- name: Refresh public sources\n(.*?)(?=\n      - name:)", text, re.S).group(1)
        self.assertIn("EXCLUDED_PUBLIC_IDS_JSON: ${{ secrets.EXCLUDED_PUBLIC_IDS_JSON }}", step)
        self.assertNotRegex(text, r"(echo|printf|cat)[^\n]*EXCLUDED_PUBLIC_IDS_JSON")


class DryRunTests(unittest.TestCase):
    def test_offline_fixture_dry_run_end_to_end(self):
        config, _e, state, template = env()
        tmp = temp_repo(self, state, template, config)
        out_file = os.path.join(tmp, "gh_output")
        code, out, err = run_main(["--root", tmp, "refresh", "--fixtures", os.path.join(FIXTURES, "dry_run.json"),
                                   "--now", "2026-10-31T18:00+05:30", "--github-output", out_file],
                                  json.dumps(TEST_EXCLUDED))
        self.assertEqual(code, 0, err)
        after = R.load_all(tmp)[1]
        added = {r.job_id: r for r in after.jobs} .keys() - {r.job_id for r in state.jobs}
        self.assertEqual(added, {"528001", "R1580001", "R5060001", "210809001", "R4049001", "68999"})
        new = {r.job_id: r for r in after.jobs}
        self.assertEqual(new["R1580001"].closing_date, "2026-11-15")   # Workday endDate 2026-11-16 is exclusive
        self.assertEqual(new["R5060001"].closing_date, "2026-11-20")
        self.assertEqual(new["210809001"].closing_date, "2026-11-21")  # Oracle end date: same calendar day
        self.assertEqual(new["210809001"].location, "Bengaluru and Mumbai")
        self.assertEqual(new["210809001"].experience_band, "1")        # required section only, not preferred
        self.assertEqual(new["R4049001"].closing_date, "2026-11-14")   # GE HealthCare Workday: exclusive
        self.assertEqual(new["R4049001"].location, "Bengaluru")
        self.assertEqual(new["68999"].location, "Bengaluru and Pune")
        self.assertEqual(new["68999"].canonical_url, "https://jobs.zs.com/jobs/68999")
        ids = {r.job_id for r in after.jobs}
        self.assertNotIn("500001", ids)                          # excluded ID seen in the Siemens listing
        self.assertNotIn("R1590001", ids)                        # excluded ID seen in the IQVIA listing
        self.assertNotIn("210809002", ids)                       # senior title with 8+ years
        self.assertNotIn("R4049002", ids)                        # non-target city: never fetched
        self.assertIn("520000", after.companies["Siemens"].screened)
        self.assertIn("210809002", after.companies["JPMorganChase"].screened)
        for company in ("JPMorganChase", "GE HealthCare", "ZS"):   # current rows retained, dates kept
            self.assertEqual([r.closing_date for r in rows_of(after, company) if r.job_id in
                              {x.job_id for x in rows_of(state, company)}],
                             [r.closing_date for r in rows_of(state, company)], company)
        self.assertEqual({c: s.status for c, s in after.companies.items()},
                         {"Siemens": "ok", "JPMorganChase": "ok", "GE HealthCare": "ok",
                          "GE Vernova": "ok", "IQVIA": "ok", "ZS": "ok"})
        self.assertEqual(R.read_text(os.path.join(tmp, "index.html")), R.render(after, template, config))
        with open(out_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "changed=true\nas_of=2026-10-31\n")
        self.assertIn("| Siemens | ok |", out)


# --------------------------------------------------------------------------- per-source pull history

def pull(at, result="accepted", published=0, added=0, removed=0):
    return dict(zip(R.PULL_FIELDS, (at, result, published, added, removed)))


def channel_row(doc: str, company: str) -> str:
    """One source's whole `<li class="ch">` line (pull items nest their own <li>, so no regex end)."""
    rows = [ln for ln in health_section(doc).splitlines() if ln.startswith('<li class="ch"')]
    return next(ln for ln in rows if f'<strong class="ch-name">{company}</strong>' in ln)


class PullHistoryTests(unittest.TestCase):
    def setUp(self):
        self.config, self.excluded, self.state, self.template = env()

    def test_company_state_fields_end_with_pulls(self):
        self.assertEqual([f.name for f in dataclasses.fields(R.CompanyState)], list(R.COMPANY_FIELDS))
        self.assertEqual(R.COMPANY_FIELDS[-2:], ("removal_hold", "pulls"))
        self.assertEqual(R.PULL_FIELDS, ("at", "result", "published", "added", "removed"))
        self.assertEqual((R.MAX_PULLS, R.CompanyState("seed", "x").pulls), (6, []))
        self.assertEqual(list(json.loads(self.state.to_json())["companies"]["Siemens"]), list(R.COMPANY_FIELDS))

    def test_missing_pulls_key_is_seeded_from_the_last_attempt(self):
        at = "2026-10-31T16:38+05:30"
        self.state.companies["Siemens"] = dataclasses.replace(self.state.companies["Siemens"], status="ok",
                                                              last_success=at, last_attempt=at)
        published = R.published_counts(self.state, self.config)
        for drop in (("pulls",), ("pulls", "removal_hold")):       # both older field sets still load
            with self.subTest(drop):
                data = json.loads(self.state.to_json())
                for c in data["companies"].values():
                    for k in drop:
                        del c[k]
                loaded = R.load_state(data, self.config)
                self.assertEqual(loaded.companies["Siemens"].pulls,
                                 [pull(at, "accepted", published["Siemens"], None, None)])
                for name in self.config.companies[1:]:            # no attempt recorded: nothing to seed
                    self.assertEqual(loaded.companies[name].pulls, [], name)
                again = R.load_state(json.loads(loaded.to_json()), self.config)
                self.assertEqual(again.to_json(), loaded.to_json())

    def test_seed_uses_the_attempt_outcome(self):
        at = "2026-10-31T16:38+05:30"
        self.state.companies["ZS"] = dataclasses.replace(self.state.companies["ZS"], status="stale",
                                                         last_attempt=at)
        self.state.companies["IQVIA"] = dataclasses.replace(self.state.companies["IQVIA"], status="seed",
                                                            last_attempt=at)       # outcome "unrecorded"
        data = json.loads(self.state.to_json())
        for c in data["companies"].values():
            del c["pulls"]
        loaded = R.load_state(data, self.config)
        n = R.published_counts(self.state, self.config)["ZS"]
        self.assertEqual(loaded.companies["ZS"].pulls,
                         [pull(at, "first_snapshot" if n else "first_empty", n, None, None)])
        self.assertEqual(loaded.companies["IQVIA"].pulls, [])

    def test_strict_validation_fails_closed(self):
        times = [f"2026-10-{d:02d}T05:53+05:30" for d in range(20, 27)]
        good = [pull(t) for t in times[:6]]
        bad = {"seven entries": [pull(t) for t in times],
               "wrong keys": [{"at": times[0], "result": "accepted", "published": 1, "added": 0}],
               "extra key": [{**pull(times[0]), "detail": "x"}],
               "key order": [dict(reversed(list(pull(times[0]).items())))],
               "bad result": [pull(times[0], result="unrecorded")],
               "descending": [pull(times[1]), pull(times[0])],
               "bad time": [pull("yesterday")],
               "negative": [pull(times[0], published=-1)],
               "bool count": [pull(times[0], added=True)],
               "null published": [pull(times[0], published=None)],
               "not a list": {"at": times[0]},
               "not a dict": [list(pull(times[0]).values())]}
        data = json.loads(self.state.to_json())
        data["companies"]["Siemens"]["pulls"] = good
        self.assertEqual(R.load_state(data, self.config).companies["Siemens"].pulls, good)
        for name, value in bad.items():
            with self.subTest(name):
                d = json.loads(self.state.to_json())
                d["companies"]["Siemens"]["pulls"] = value
                with self.assertRaises(ValueError):
                    R.load_state(d, self.config)

    def test_fixture_run_records_one_pull_per_source(self):
        tmp = temp_repo(self, self.state, self.template, self.config)
        code, _out, err = run_main(["--root", tmp, "refresh", "--fixtures", os.path.join(FIXTURES, "dry_run.json"),
                                    "--now", "2026-10-31T18:00+05:30"], json.dumps(TEST_EXCLUDED))
        self.assertEqual(code, 0, err)
        after = R.load_all(tmp)[1]
        published = R.published_counts(after, self.config)
        before_ids = {r.job_id for r in self.state.jobs}
        for company in self.config.companies:
            with self.subTest(company):
                added = {r.job_id for r in rows_of(after, company)} - before_ids
                self.assertEqual(len(added & FIXTURE_ADDED), 1)
                self.assertEqual(after.companies[company].pulls,
                                 [pull("2026-10-31T18:00+05:30", "accepted", published[company], len(added), 0)])
        doc = R.read_text(os.path.join(tmp, R.INDEX_PATH))
        self.assertIn('<summary><span class="tri" aria-hidden="true">▸</span>last 1 pull</summary>',
                      channel_row(doc, "Siemens"))

    def test_history_keeps_the_newest_six_pulls(self):
        transport = FakeTransport()                 # every source call fails: each run is a held attempt
        state = self.state
        runs = [NOW + dt.timedelta(hours=6 * i) for i in range(7)]
        for when in runs:
            state, outcomes = R.run_refresh(state, self.config, self.excluded, when,
                                            factory_for(self.config, transport))
            self.assertEqual({o.result for o in outcomes}, {"held"})
            state = R.load_state(json.loads(state.to_json()), self.config)     # persisted between runs
        for company in self.config.companies:
            with self.subTest(company):
                pulls = state.companies[company].pulls
                self.assertEqual([p["at"] for p in pulls], [R.stamp(w) for w in runs[1:]])   # oldest dropped
                self.assertEqual(pulls[-1]["at"], state.companies[company].last_attempt)
                n = R.published_counts(state, self.config)[company]
                self.assertEqual(pulls[-1], pull(R.stamp(runs[-1]), "first_snapshot" if n else "first_empty", n, 0, 0))

    def test_budget_skipped_source_records_a_held_pull(self):
        ticks = iter([0.0] + [R.RUN_BUDGET_S] * 50)            # the run budget is gone before any source
        after, outcomes = R.run_refresh(self.state, self.config, self.excluded, NOW,
                                        factory_for(self.config, FakeTransport()), clock=lambda: next(ticks))
        self.assertEqual({o.detail for o in outcomes}, {"run time budget exhausted"})
        for company in self.config.companies:
            self.assertEqual([(p["at"], p["added"], p["removed"]) for p in after.companies[company].pulls],
                             [(R.stamp(NOW), 0, 0)])

    def test_offline_and_static_runs_record_nothing(self):
        self.state.companies["Siemens"].pulls = [pull("2026-10-31T05:53+05:30", published=3)]
        after, _o = R.run_refresh(self.state, self.config, self.excluded, NOW, None)
        self.assertEqual(after.companies["Siemens"].pulls, [pull("2026-10-31T05:53+05:30", published=3)])
        self.assertTrue(all(not after.companies[c].pulls for c in self.config.companies[1:]))
        self.assertFalse(R.meaningful(self.state, after))
        raw = R.read_json(os.path.join(ROOT, R.CONFIG_PATH))
        raw["companies"] = [e if e["company"] != "ZS" else {"company": "ZS", "mode": "static"}
                            for e in raw["companies"]]
        raw["hosts"]["jobs.zs.com"]["fetch"] = False
        static = R.load_config(raw)
        self.state.companies["ZS"] = dataclasses.replace(self.state.companies["ZS"], status="static")
        after, outcomes = R.run_refresh(self.state, static, self.excluded, NOW,
                                        factory_for(static, FakeTransport()))
        self.assertEqual([o.result for o in outcomes if o.company == "ZS"], ["static"])
        self.assertEqual(after.companies["ZS"].pulls, [])
        self.assertEqual(len(after.companies["IQVIA"].pulls), 1)

    def test_render_lists_pulls_newest_first(self):
        self.state.checked_at = "2026-10-31T22:47+05:30"
        self.state.companies["Siemens"].pulls = [
            pull("2026-10-31T05:53+05:30", "first_empty", 0, 0, 0),
            pull("2026-10-31T11:53+05:30", "held", 4, 0, 2),
            pull("2026-10-31T17:53+05:30", "accepted", 9, 3, 1)]
        self.state.companies["IQVIA"].pulls = [pull("2026-10-31T16:38+05:30", "accepted", 5, None, None)]
        self.state.companies["ZS"].pulls = [pull("2026-10-31T17:53+05:30", "accepted", 3, 2, 0)]
        doc = R.render(self.state, self.template, self.config)
        # Each line leads with what changed and ends with the total; zero counts are never spelled out.
        self.assertIn(
            '</dl><details class="pulls"><summary><span class="tri" aria-hidden="true">▸</span>last 3 pulls</summary>'
            '<ol class="pl-list">'
            '<li><time datetime="2026-10-31T17:53:00+05:30">31 Oct 17:53 IST</time> · '
            '<span class="oc" data-o="accepted">Successful refresh</span> · <span class="pd"><b class="pa">+3 new</b> · '
            '<span class="pr">−1 removed</span></span> · 9 total</li>'
            '<li><time datetime="2026-10-31T11:53:00+05:30">31 Oct 11:53 IST</time> · '
            '<span class="oc" data-o="held">Refresh held, cached data kept</span> · <span class="pd">'
            '<span class="pr">−2 removed</span></span> · 4 total</li>'
            '<li><time datetime="2026-10-31T05:53:00+05:30">31 Oct 05:53 IST</time> · '
            '<span class="oc" data-o="failed">No successful automated refresh yet</span> · '
            '<span class="nc">no change</span> · 0 total</li></ol></details></li>', channel_row(doc, "Siemens"))
        self.assertIn('<span class="pd"><b class="pa">+2 new</b></span> · 3 total</li>', channel_row(doc, "ZS"))
        # A seeded entry has no added/removed counts, so it shows only the total.
        self.assertIn('last 1 pull</summary><ol class="pl-list"><li><time datetime="2026-10-31T16:38:00+05:30">'
                      '31 Oct 16:38 IST</time> · <span class="oc" data-o="accepted">Successful refresh</span> · '
                      '5 total</li></ol></details></li>', channel_row(doc, "IQVIA"))
        iqvia_pulls = channel_row(doc, "IQVIA").split("<details")[1]
        for gone in ('class="pd"', 'class="nc"', "published"):
            self.assertNotIn(gone, iqvia_pulls, gone)
        for company in ("Siemens", "IQVIA", "ZS"):       # zero deltas are never spelled out (visible text only)
            text = re.sub(r"<[^>]+>", "", channel_row(doc, company).split("<details")[1])
            self.assertNotIn("+0 new", text, company)
            self.assertNotIn("−0 removed", text, company)
        for company in ("JPMorganChase", "GE HealthCare", "GE Vernova"):
            self.assertTrue(channel_row(doc, company).endswith('</dl><p class="pulls-none">No pull history yet</p></li>'))
        self.assertEqual(doc.count('<details class="pulls">'), 3)
        self.assertNotIn("<details", page_outside_dialog(doc))
        # Re-aging reads only .h-data times; pull times stay as rendered.
        self.assertNotIn(".pulls", main_script(doc))
        self.assertNotIn("pl-list", main_script(doc))

    def test_pull_styles_and_breakpoints(self):
        base, blocks = media_blocks(style_of(R.render(self.state, self.template, self.config)))
        self.assertIn(".ch>.pulls,.ch>.pulls-none{grid-column:2/-1;grid-row:2;margin:4px 0 0}", base)
        self.assertIn(".pulls[open] .tri{transform:rotate(90deg)}", base)
        self.assertIn(".pulls>summary::-webkit-details-marker{display:none}", base)
        self.assertIn(".pl-list,.pulls>summary,.pulls-none{font-family:var(--font-mono);", base)
        self.assertIn(".ch>.pulls,.ch>.pulls-none{grid-column:1/-1;grid-row:3}", blocks[SHEET_MQ])
        # .pr is also the search prompt; the removed-count colour is scoped so the prompt keeps its look.
        self.assertIn(".pr{font-size:14px;font-weight:700;color:hsl(var(--signal))}", base)
        self.assertIn(".pd .pr{font-size:inherit;font-weight:inherit;color:hsl(var(--bad))}", base)


# --------------------------------------------------------------------------- phone alerts (dist/alerts.json)

LEDGER = "https://example-owner.github.io/job-openings-ledger/"   # synthetic owner; CI derives the real one
FIXTURE_ADDED = {"528001", "R1580001", "R5060001", "210809001", "R4049001", "68999"}


def outcome_for(state, added=(), removed=0, run_at="2026-10-31T18:00+05:30", mode="fixtures"):
    return {"schema": 1, "run_at": run_at, "mode": mode, "state_sha256": R.sha256_text(state.to_json()),
            "added": [list(k) for k in added], "removed_count": removed}


def alert_for(state, config, **kw):
    return R.build_alert(state, R.load_outcome(outcome_for(state, **kw), config), config, LEDGER)


def with_closing(state, dates):
    """State whose rows have the given closing dates (by index); every other row has none."""
    jobs = [dataclasses.replace(r, closing_date=dates.get(i)) for i, r in enumerate(state.jobs)]
    return dataclasses.replace(state, jobs=jobs)


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.config, self.excluded, self.state, self.template = env()

    def day(self, n):
        return (self.state.as_of + dt.timedelta(days=n)).isoformat()

    def cli_run(self, root, now="2026-10-31T18:00+05:30", fixtures=True):
        outcome = os.path.join(root, "tmp", "outcome.json")
        argv = ["--root", root, "refresh", "--now", now, "--outcome", outcome]
        argv += ["--fixtures", os.path.join(FIXTURES, "dry_run.json")] if fixtures else ["--offline"]
        code, out, err = run_main(argv, json.dumps(TEST_EXCLUDED))
        self.assertEqual(code, 0, err)
        dist = os.path.join(root, "dist")
        self.assertEqual(run_main(["--root", root, "render", "--out", os.path.join(dist, "index.html")], None)[0], 0)
        code, aout, aerr = run_main(["--root", root, "alert", "--outcome", outcome,
                                     "--out", os.path.join(dist, "alerts.json"), "--ledger-url", LEDGER], None)
        self.assertEqual(code, 0, aerr)
        with open(os.path.join(dist, "alerts.json"), encoding="utf-8") as fh:
            text = fh.read()
        return text, json.loads(text), out + err + aout + aerr, outcome

    # ---- payload contents

    def test_no_new_jobs_payload_still_has_a_fresh_refresh_id(self):
        after, outcomes = R.run_refresh(self.state, self.config, self.excluded, NOW, None)
        o1 = R.run_outcome(after, outcomes, NOW, "offline", self.excluded)
        self.assertEqual(o1["added"], [])
        a = R.build_alert(after, R.load_outcome(o1, self.config), self.config, LEDGER)
        self.assertEqual((a["new_count"], a["new_jobs"], a["remaining_new_count"]), (0, [], 0))
        self.assertEqual(a["notification_title"], "No new job openings")
        self.assertTrue(a["notification_body"].startswith("No new openings. "))
        self.assertEqual(a["removed_count"], sum(len(o.removed) for o in outcomes))
        self.assertEqual((a["checked_at"], a["as_of"]), ("2026-10-31T18:00+05:30", "2026-10-31"))
        self.assertTrue(a["refresh_id"].startswith("2026-10-31T18:00+05:30#"))
        later = NOW + dt.timedelta(hours=6)
        after2, outcomes2 = R.run_refresh(after, self.config, self.excluded, later, None)
        a2 = R.build_alert(after2, R.load_outcome(R.run_outcome(after2, outcomes2, later, "offline", self.excluded),
                                                  self.config), self.config, LEDGER)
        self.assertEqual(a2["new_count"], 0)
        self.assertNotEqual(a2["refresh_id"], a["refresh_id"])     # the phone sees each completed check

    def test_payload_shape_and_totals_match_the_page(self):
        a = alert_for(self.state, self.config)
        self.assertEqual(tuple(a), R.ALERT_FIELDS)
        doc = R.render(self.state, self.template, self.config)
        self.assertIn(f'<dd id="total">{a["total_jobs"]}</dd>', doc)
        self.assertIn(f'<dd id="companies">{a["company_count"]}</dd>', doc)
        self.assertEqual(a["total_jobs"], len(self.state.jobs))
        self.assertEqual(a["company_count"], len({r.company for r in self.state.jobs}))
        self.assertEqual(a["ledger_url"], LEDGER)
        self.assertEqual(list(a["source_status"]), list(self.config.companies))
        self.assertTrue(set(a["source_status"].values()) <= {"ok", "stale", "static"})

    def test_additions_come_from_the_refresh_outcome(self):
        root = temp_repo(self, self.state, self.template, self.config)
        text, a, logs, _o = self.cli_run(root)
        after = R.load_all(root)[1]
        rows = {r.job_id: r for r in after.jobs}
        self.assertEqual(a["new_count"], len(FIXTURE_ADDED))
        self.assertEqual({j["job_id"] for j in a["new_jobs"]}, FIXTURE_ADDED)
        for j in a["new_jobs"]:
            r = rows[j["job_id"]]
            self.assertEqual(j, {"company": r.company, "job_id": r.job_id, "title": r.title,
                                 "canonical_url": r.canonical_url})
        self.assertEqual(a["notification_title"], "6 new job openings")
        self.assertTrue(a["notification_body"].startswith("New: Siemens 1, JPMorganChase 1, GE HealthCare 1, "))
        self.assertNotIn("mode", a)                                           # run mode stays internal
        for title in {j["title"] for j in a["new_jobs"]}:
            self.assertNotIn(title, logs)                                     # logs carry IDs and counts only

    def test_row_found_and_pruned_in_the_same_run_is_neither_added_nor_removed(self):
        zs = rows_of(self.state, "ZS")
        expired = dataclasses.replace(zs[0], job_id="68997", canonical_url="https://jobs.zs.com/jobs/68997",
                                      closing_date=(TODAY - dt.timedelta(days=1)).isoformat())
        fresh = dataclasses.replace(zs[0], job_id="68996", canonical_url="https://jobs.zs.com/jobs/68996")
        live = {r.job_id for r in zs}

        def runner(cfg, rows, fetcher, now, cs, excluded, config):
            return R.SourceResult("ZS", True, "listing", live=live, listing_complete=True, listed=live,
                                  listing_total=len(live) + 2, added=[expired, fresh])
        only_zs = lambda _c, _cfg: None   # noqa: E731 - the patched runner never fetches
        with mock.patch.dict(R.RUNNERS, {"zs_jibe_api": runner}), \
                mock.patch.dict(self.config.sources, {c: {**s, "mode": "static"} for c, s in
                                                      self.config.sources.items() if c != "ZS"}):
            after, outcomes = R.run_refresh(self.state, self.config, self.excluded, NOW, only_zs)
        o = next(o for o in outcomes if o.company == "ZS")
        self.assertEqual((o.added, o.removed, o.after), (["68996"], [], len(zs) + 1))
        self.assertNotIn("68997", {r.job_id for r in after.jobs})
        outcome = R.run_outcome(after, outcomes, NOW, "fixtures", self.excluded)
        self.assertEqual((outcome["added"], outcome["removed_count"]), ([["ZS", "68996"]], 0))

    def test_offline_run_adds_nothing(self):
        root = temp_repo(self, self.state, self.template, self.config)
        _t, a, _l, _o = self.cli_run(root, fixtures=False)
        self.assertEqual((a["new_count"], a["new_jobs"]), (0, []))
        bad = outcome_for(self.state, added=[("ZS", "24123")], mode="offline")
        with self.assertRaises(ValueError):
            R.load_outcome(bad, self.config)

    def test_more_than_eight_new_jobs_are_truncated(self):
        keys = [(r.company, r.job_id) for r in self.state.jobs[:11]]
        a = alert_for(self.state, self.config, added=keys)
        self.assertEqual((a["new_count"], len(a["new_jobs"]), a["remaining_new_count"]), (11, 8, 3))
        self.assertEqual([(j["company"], j["job_id"]) for j in a["new_jobs"]], keys[:8])   # page order
        self.assertEqual(a["notification_title"], "11 new job openings")

    def test_closing_soon_window_and_order(self):
        state = with_closing(self.state, {0: self.day(7), 1: self.day(0), 2: self.day(8), 3: self.day(-1),
                                          4: self.day(3), 5: self.day(3)})
        a = alert_for(state, self.config)
        self.assertEqual(a["closing_soon_count"], 4)                 # today, +3, +3, +7; not +8, not passed
        self.assertEqual([c["closing_date"] for c in a["closing_soon"]],
                         [self.day(0), self.day(3), self.day(3), self.day(7)])
        self.assertEqual([c["job_id"] for c in a["closing_soon"]],
                         [state.jobs[i].job_id for i in (1, 4, 5, 0)])
        self.assertIn("4 closing within 7 days.", a["notification_body"])
        # Same rule as the page's "closing within 7 days" filter.
        doc = R.render(state, self.template, self.config)
        rows = json.loads(re.search(r"\nconst jobs=(\[\n.*?\n\]);\n", doc, re.S).group(1))
        self.assertEqual(sorted(closing_filter(rows, "7")), sorted(c["job_id"] for c in a["closing_soon"]))

    def test_closing_soon_is_capped_at_five_and_passed_dates_are_excluded(self):
        state = with_closing(self.state, {i: self.day(i % 4) for i in range(7)} | {7: self.day(-2), 8: self.day(-1)})
        a = alert_for(state, self.config)
        self.assertEqual((a["closing_soon_count"], len(a["closing_soon"])), (7, 5))
        self.assertTrue(all(c["closing_date"] >= self.day(0) for c in a["closing_soon"]))
        dates = [c["closing_date"] for c in a["closing_soon"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(alert_for(with_closing(self.state, {0: self.day(-1)}), self.config)["closing_soon_count"], 0)

    def test_notification_body_length_and_plain_text(self):
        everyone = [(r.company, r.job_id) for r in self.state.jobs]
        a = alert_for(with_closing(self.state, {i: self.day(1) for i in range(20)}), self.config, added=everyone)
        self.assertLessEqual(len(a["notification_body"]), R.ALERT_BODY_MAX)
        self.assertNotRegex(a["notification_title"] + a["notification_body"], r"[<>&\n]")
        long_names = [("Company " + "x" * 90, 1), ("Company " + "y" * 90, 2), ("Company " + "z" * 90, 3)]
        title, body = R.notification_text(long_names, 12, 999, 6)
        self.assertEqual(title, "6 new job openings")
        self.assertEqual(body, "New: 6. 12 closing within 7 days. 999 jobs across 6 companies.")
        self.assertEqual(R.notification_text([("ZS", 1)], 1, 1, 1),
                         ("1 new job opening", "New: ZS 1. 1 closing within 7 days. 1 job across 1 company."))

    # ---- safety

    def test_markup_in_titles_is_escaped(self):
        evil = "</script><img src=x onerror=alert(1)> & \u2028Analyst"
        jobs = [dataclasses.replace(self.state.jobs[0], title=evil), *self.state.jobs[1:]]
        state = dataclasses.replace(self.state, jobs=jobs)
        a = alert_for(state, self.config, added=[(jobs[0].company, jobs[0].job_id)])
        text = R.alert_json(a)
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)
        self.assertNotIn("&", text)
        self.assertNotIn("\u2028", text)
        self.assertTrue(text.isascii())
        self.assertEqual(json.loads(text)["new_jobs"][0]["title"], evil)         # escaping is lossless

    def test_unsafe_titles_are_rejected(self):
        for title in ("Analyst\x07", "Analyst\nTwo", "Mid" + "way Analyst", "Analyst " + "x" + "@" + "example.com"):
            with self.subTest(repr(title[:8])):
                jobs = [dataclasses.replace(self.state.jobs[0], title=title), *self.state.jobs[1:]]
                with self.assertRaises(ValueError):
                    alert_for(dataclasses.replace(self.state, jobs=jobs), self.config,
                              added=[(jobs[0].company, jobs[0].job_id)])

    def test_public_boundary_checks_on_the_payload(self):
        a = alert_for(self.state, self.config)
        self.assertEqual(R.find_forbidden(R.alert_json(a).replace(LEDGER, "")), [])
        for key, value in (("notification_body", "Ask " + "x" + "@" + "example.com"),
                           ("notification_body", "Open " + "a" + "toz"),
                           ("notification_body", "<b>3 new</b>"),
                           ("notification_title", "x" * 61),
                           ("notification_body", "x" * 241),
                           ("ledger_url", "https://evil.example/job-openings-ledger/"),
                           ("ledger_url", LEDGER + "?next=https://evil.example/"),
                           ("ledger_url", "http://example-owner.github.io/job-openings-ledger/")):
            with self.subTest(key=key, value=value[:12]):
                bad = json.loads(json.dumps(a))
                bad[key] = value
                with self.assertRaises(ValueError):
                    R.validate_alert(bad, self.config)
        bad = json.loads(json.dumps(a))
        bad["source_status"]["ZS"] = "source error (Timeout)"                 # status words only, no raw detail
        with self.assertRaises(ValueError):
            R.validate_alert(bad, self.config)
        bad = json.loads(json.dumps(a))
        bad["closing_soon"] = [{"company": "ZS", "job_id": "1", "title": "x", "closing_date": self.day(1),
                                "canonical_url": "https://evil.example/jobs/1"}]
        bad["closing_soon_count"] = 1
        with self.assertRaises(ValueError):
            R.validate_alert(bad, self.config)
        # The Pages owner may contain a scanner term; only the strictly shaped ledger_url is exempt.
        owner_url = "https://" + "jeev" + "annn0.github.io/job-openings-ledger/"
        self.assertEqual(R.build_alert(self.state, R.load_outcome(outcome_for(self.state), self.config),
                                       self.config, owner_url)["ledger_url"], owner_url)

    def test_private_exclusions_never_reach_outcome_or_alert(self):
        hidden = R.Row(company="IQVIA", job_id="R1590001", title="Clin Data Spec", experience_text="Not stated",
                       experience_band="n", location="Kochi", canonical_url="https://jobs.iqvia.com/en/jobs/R1590001-0",
                       posted_date="2026-10-28", closing_date=self.day(2), first_seen="2026-10-31",
                       last_seen="2026-10-31")
        state = dataclasses.replace(self.state, jobs=R.sort_rows([*self.state.jobs, hidden], self.config))
        root = temp_repo(self, state, self.template, self.config)
        text, a, logs, outcome = self.cli_run(root)
        published = [text, logs, R.read_text(outcome)]
        for jid in ("R1590001", "500001"):
            for doc in published:
                self.assertNotIn(jid, doc)
        plain = self.cli_run(temp_repo(self, self.state, self.template, self.config))[1]
        self.assertEqual(a["removed_count"], plain["removed_count"])   # an exclusion is not reported as a removal
        self.assertEqual(a["new_count"], plain["new_count"])
        after, outcomes = R.run_refresh(self.state, self.config, self.excluded, NOW, None)
        outcomes[0].added.append("500001")
        with self.assertRaises(ValueError):
            R.run_outcome(after, outcomes, NOW, "offline", self.excluded)

    def test_deterministic_for_fixed_time_and_outcomes(self):
        texts = []
        for _ in range(2):
            root = temp_repo(self, self.state, self.template, self.config)
            texts.append(self.cli_run(root)[0])
        self.assertEqual(texts[0], texts[1])
        self.assertEqual(R.alert_json(alert_for(self.state, self.config)), R.alert_json(alert_for(self.state, self.config)))

    def test_artifact_contains_index_and_alerts_only(self):
        root = temp_repo(self, self.state, self.template, self.config)
        self.cli_run(root)
        dist = os.path.join(root, "dist")
        self.assertEqual(sorted(os.listdir(dist)), ["alerts.json", "index.html"])
        self.assertEqual(R.read_text(os.path.join(dist, "index.html")), R.read_text(os.path.join(root, "index.html")))
        self.assertIn("dist/", R.read_text(os.path.join(ROOT, ".gitignore")).splitlines())
        self.assertNotIn("alerts.json", " ".join(R.ALLOWED_FILES))

    # ---- failure never publishes a success payload

    def test_failed_refresh_writes_no_outcome_and_alert_fails(self):
        root = temp_repo(self, self.state, self.template, self.config)
        outcome, out = os.path.join(root, "tmp", "outcome.json"), os.path.join(root, "dist", "alerts.json")
        code, _o, _e = run_main(["--root", root, "refresh", "--offline", "--outcome", outcome], "{bad")
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(outcome))
        code, _o, err = run_main(["--root", root, "alert", "--outcome", outcome, "--out", out,
                                  "--ledger-url", LEDGER], None)
        self.assertEqual(code, 2, err)
        self.assertFalse(os.path.exists(out))

    def test_stale_or_tampered_outcome_is_rejected(self):
        root = temp_repo(self, self.state, self.template, self.config)
        _t, _a, _l, outcome = self.cli_run(root)
        saved = R.read_text(outcome)
        # A later run that did not record an outcome leaves the earlier outcome stale.
        code, _o, err = run_main(["--root", root, "refresh", "--offline", "--now", "2026-11-01T00:00+05:30"],
                                 json.dumps(TEST_EXCLUDED))
        self.assertEqual(code, 0, err)
        out = os.path.join(root, "dist", "stale.json")
        code, _o, err = run_main(["--root", root, "alert", "--outcome", outcome, "--out", out,
                                  "--ledger-url", LEDGER], None)
        self.assertEqual(code, 2)
        self.assertIn("does not match state", err)
        self.assertFalse(os.path.exists(out))
        data = json.loads(saved)
        for key, value in (("added", [["ZS", "99999"], ["ZS", "99999"]]), ("removed_count", -1),
                           ("mode", "partial"), ("state_sha256", "0" * 63), ("extra", 1)):
            with self.subTest(key):
                with self.assertRaises(ValueError):
                    R.load_outcome({**data, key: value}, self.config)
        missing = outcome_for(self.state, added=[("ZS", "99999")])          # not in the published state
        with self.assertRaises(ValueError):
            R.build_alert(self.state, R.load_outcome(missing, self.config), self.config, LEDGER)

    def test_outcome_cannot_be_combined_with_dry_run(self):
        root = temp_repo(self, self.state, self.template, self.config)
        outcome = os.path.join(root, "tmp", "outcome.json")
        code, _o, err = run_main(["--root", root, "refresh", "--offline", "--dry-run", "--outcome", outcome],
                                 json.dumps(TEST_EXCLUDED))
        self.assertEqual(code, 2)
        self.assertIn("--dry-run", err)
        self.assertFalse(os.path.exists(outcome))

    def test_workflow_builds_alert_after_refresh_and_never_commits_it(self):
        text = R.read_text(os.path.join(ROOT, ".github", "workflows", "refresh.yml"))
        refresh = re.search(r"^  refresh:\n(.*?)(?=^  [a-z]+:\n)", text, re.S | re.M).group(1)
        order = ["refresh.py refresh", '--outcome "$RUNNER_TEMP/refresh-outcome.json"', "refresh.py scan",
                 "render --out dist/index.html", "cmp dist/index.html index.html",
                 'alert --outcome "$RUNNER_TEMP/refresh-outcome.json" --out dist/alerts.json',
                 'test "$(ls -A dist | tr', "git add -- state/jobs.json index.html", "upload-pages-artifact"]
        positions = [refresh.index(s) for s in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('--ledger-url "https://${GITHUB_REPOSITORY_OWNER,,}.github.io/${GITHUB_REPOSITORY#*/}/"',
                      refresh)
        build = re.search(r"- name: Build Pages artifact\n(.*?)(?=\n      - name:)", refresh, re.S).group(1)
        self.assertIn("set -euo pipefail", build)
        self.assertIn("path: dist", refresh)
        self.assertEqual(text.count("alerts.json"), 2)                        # built and listed; never staged
        self.assertNotIn("alerts.json", re.search(r"git add[^\n]*", refresh).group(0))
        self.assertNotRegex(text, r"continue-on-error|always\(\)|failure\(\)")
        self.assertEqual(text.count("refresh-outcome.json"), 2)


# --------------------------------------------------------------------------- live adapters: JPMorganChase, GE HealthCare, ZS

def run_source(company, routes, *, state=None, cs=None, excluded=frozenset(), now=NOW, robots=None):
    """One adapter run plus its merge. Returns (result, rows after, company state after, removed, transport)."""
    config, _e, seeded, _t = env()
    state = state or seeded
    rows = rows_of(state, company)
    cs = cs or state.companies[company]
    cfg = config.sources[company]
    t = FakeTransport(routes, robots=robots)
    f = R.Fetcher(config, cfg["budget_s"], transport=t, sleep=lambda _s: None)
    res = R.RUNNERS[cfg["mode"]](cfg, rows, f, now, cs, frozenset(excluded), config)
    new_rows, new_cs, removed = R.apply_result(rows, cs, res, now)
    return res, new_rows, new_cs, removed, t


def by_id(rows):
    return {r.job_id: r for r in rows}


BLR_FACET = 300000081145775
JPM_DETAIL_PREFIX = R.JPM_REST + "recruitingCEJobRequisitionDetails?*"
JPM_EMPTY = jresp({"items": [], "count": 0, "hasMore": False})


def jpm_boot(facets=(("Bengaluru, Karnataka, India", BLR_FACET), ("Mumbai, Maharashtra, India", 300000081151360))):
    return jresp({"items": [{"TotalJobsCount": 7000, "requisitionList": [], "locationsFacet": [
        {"Id": i, "Name": n, "TotalCount": 1} for n, i in facets]}], "count": 1})


def jpm_req(jid, posted="2026-10-30", title="Analyst"):
    return {"Id": jid, "Title": title, "PostedDate": posted, "PrimaryLocation": "Bengaluru, Karnataka, India",
            "secondaryLocations": []}


def jpm_listing_routes(reqs, total=None, pages=None):
    """Boot + target-facet pages of 25. `pages` overrides individual page responses by offset."""
    total = len(reqs) if total is None else total
    routes = [("GET", R.JPM_SEARCH.format(offset=0, facet=""), jpm_boot())]
    for off in range(0, max(total, 1), R.JPM_PAGE):
        body = (pages or {}).get(off) or jresp({"items": [{"TotalJobsCount": total,
                                                           "requisitionList": reqs[off:off + R.JPM_PAGE]}]})
        routes.append(("GET", R.JPM_SEARCH.format(offset=off, facet=R.JPM_FACET.format(id=BLR_FACET)), body))
    return routes


def jpm_detail(jid, end=None, required="", title="Analyst", posted="2026-10-30T09:51:54+00:00", secondary=(),
               desc=None):
    desc = desc if desc is not None else (
        f"<p><b>Required qualifications, capabilities, and skills</b></p><ul><li>{required}</li></ul>"
        "<p><b>Preferred qualifications, capabilities, and skills</b></p><ul><li>6+ years of experience in banking</li></ul>")
    return jresp({"items": [{"Id": jid, "Title": title, "ExternalPostedStartDate": posted,
                             "ExternalPostedEndDate": end, "PrimaryLocation": "Bengaluru, Karnataka, India",
                             "secondaryLocations": [{"Name": s} for s in secondary],
                             "ExternalDescriptionStr": desc}], "count": 1, "hasMore": False})


def jpm_details(overrides=None):
    overrides = overrides or {}

    def handler(url, _body):
        jid = re.search(r"Id=%22(\d+)%22", url).group(1)
        out = overrides.get(jid, jpm_detail(jid))
        return out(url, _body) if callable(out) else out
    return ("GET", JPM_DETAIL_PREFIX, handler)


class JpmOracleTests(unittest.TestCase):
    def setUp(self):
        _c, _e, self.state, _t = env()
        self.rows = rows_of(self.state, "JPMorganChase")
        self.ids = [r.job_id for r in self.rows]
        # 15 current rows plus 20 old postings: two pages of 25; old postings are never detail-checked.
        self.reqs = [jpm_req(i) for i in self.ids] + [jpm_req(f"2107{i:05d}", posted="2026-09-01") for i in range(20)]

    def test_complete_pagination_liveness_and_inclusive_closing(self):
        routes = jpm_listing_routes(self.reqs) + [jpm_details({
            "210804276": jpm_detail("210804276", end="2026-11-04T05:00:00+00:00"),
            "210804767": jpm_detail("210804767", end="2026-11-27T05:00:00+00:00")})]
        res, rows, cs, removed, t = run_source("JPMorganChase", routes)
        self.assertTrue(res.ok and res.listing_complete)
        self.assertEqual(res.listing_total, 35)
        self.assertEqual(t.count("offset=25,"), 1)
        self.assertEqual(res.live, set(self.ids))
        self.assertEqual(res.closing, {"210804276": "2026-11-04"})       # same calendar day, no shift
        self.assertEqual(cs.status, "ok")
        self.assertEqual(removed, [])
        self.assertEqual([r.job_id for r in rows], self.ids)            # current rows retained in order
        before = by_id(self.rows)
        for r in rows:
            if r.job_id != "210804276":
                self.assertEqual(r.closing_date, before[r.job_id].closing_date)
        self.assertEqual(t.count("2107000"), 0)                          # old postings: no detail request

    def test_partial_or_shifting_pagination_holds(self):
        cases = {
            "empty second page": {25: jresp({"items": [{"TotalJobsCount": 35, "requisitionList": []}]})},
            "total changed": {25: jresp({"items": [{"TotalJobsCount": 36, "requisitionList": self.reqs[25:]}]})},
            "duplicated page": {25: jresp({"items": [{"TotalJobsCount": 35, "requisitionList": self.reqs[:10]}]})},
            "malformed page": {25: jresp({"items": []})},
            "throttled page": {25: resp(429, "slow", headers=[("Retry-After", "1")])},
        }
        for name, pages in cases.items():
            with self.subTest(name):
                res, rows, cs, removed, _t = run_source(
                    "JPMorganChase", jpm_listing_routes(self.reqs, pages=pages) + [jpm_details()])
                self.assertFalse(res.ok)
                self.assertEqual(cs.status, "stale")
                self.assertEqual(rows, self.rows)
                self.assertEqual(removed, [])

    def test_empty_items_is_an_explicit_close(self):
        listed = [r for r in self.reqs if r["Id"] != "210804276"]
        routes = jpm_listing_routes(listed) + [jpm_details({"210804276": JPM_EMPTY, "210804767": JPM_EMPTY})]
        res, rows, cs, removed, _t = run_source("JPMorganChase", routes)
        self.assertEqual(removed, ["210804276"])                         # unlisted and empty detail
        kept = by_id(rows)
        self.assertEqual(kept["210804767"].liveness, "unconfirmed")      # listed but empty: contradiction kept
        self.assertEqual(len(rows), 14)

    def test_blocked_or_malformed_detail_keeps_rows(self):
        listed = [r for r in self.reqs if r["Id"] != "210804276"]
        for name, out in {"403": resp(403, "<html><title>Forbidden</title></html>"),
                          "429": resp(429, "slow", headers=[("Retry-After", "1")]), "500": resp(500, "x"),
                          "timeout": TimeoutError("slow"), "html": resp(200, "<html>maintenance</html>"),
                          "wrong id": jpm_detail("210000001"), "items not list": jresp({"items": {}})}.items():
            with self.subTest(name):
                res, rows, cs, removed, _t = run_source(
                    "JPMorganChase", jpm_listing_routes(listed) + [jpm_details({"210804276": out})])
                self.assertTrue(res.ok)
                self.assertEqual(removed, [])
                self.assertEqual(by_id(rows)["210804276"].liveness, "unconfirmed")
                self.assertEqual(by_id(rows)["210804276"].absent_runs, 0)
        res, rows, cs, removed, _t = run_source(
            "JPMorganChase", jpm_listing_routes(self.reqs) + [("GET", JPM_DETAIL_PREFIX, resp(503, "down"))])
        self.assertFalse(res.ok)                                         # every detail blocked: hold
        self.assertEqual(rows, self.rows)

    def test_additions_use_required_section_window_and_exclusions(self):
        new = [jpm_req("210809001"), jpm_req("210809002", title="Associate"), jpm_req("210809003"),
               jpm_req("210809004", posted="2026-09-15")]
        routes = jpm_listing_routes(self.reqs + new) + [jpm_details({
            "210809001": jpm_detail("210809001", end="2026-11-21T05:00:00+00:00", required="At least 1 year of experience in operations",
                                    secondary=["Mumbai, Maharashtra, India"]),
            "210809002": jpm_detail("210809002", required="Minimum 5 years of experience in risk"),
        })]
        res, rows, cs, removed, t = run_source("JPMorganChase", routes, excluded={"210809003"})
        added = by_id(res.added)
        self.assertEqual(set(added), {"210809001"})
        row = added["210809001"]
        self.assertEqual((row.closing_date, row.location, row.experience_band, row.posted_date),
                         ("2026-11-21", "Bengaluru and Mumbai", "1", "2026-10-30"))
        self.assertEqual(row.canonical_url, R.JPM_FRONT.format(id="210809001"))
        self.assertIn("210809002", cs.screened)                          # 5 years: screened, not added
        self.assertEqual(t.count("210809003"), 0)                        # private exclusion: never fetched
        self.assertEqual(t.count("210809004"), 0)                        # outside the 30-day window
        _r, _rows, _cs, _rm, t2 = run_source("JPMorganChase", routes, cs=cs)
        self.assertEqual(t2.count("210809002"), 0)                       # screened record is not re-fetched

    def test_every_target_city_facet_is_paginated_and_deduplicated(self):
        bangalore = 300000099999999
        boot = jpm_boot(facets=(("Bengaluru, Karnataka, India", BLR_FACET), ("Bangalore, Karnataka, India", bangalore),
                                ("Karnataka, India", 300000081134215)))
        second = [jpm_req("210804767"), jpm_req("210809011")]          # one role in both facets
        routes = [("GET", R.JPM_SEARCH.format(offset=0, facet=""), boot)] + jpm_listing_routes(self.reqs)[1:] + [
            ("GET", R.JPM_SEARCH.format(offset=0, facet=R.JPM_FACET.format(id=bangalore)),
             jresp({"items": [{"TotalJobsCount": 2, "requisitionList": second}]})), jpm_details()]
        res, rows, cs, removed, t = run_source("JPMorganChase", routes)
        self.assertTrue(res.ok)
        self.assertEqual(len(res.listed), 36)
        self.assertEqual(t.count("selectedLocationsFacet=300000081134215"), 0)   # state facet is not a city
        self.assertIn("210809011", by_id(res.added))
        failing = routes[:-2] + [("GET", R.JPM_SEARCH.format(offset=0, facet=R.JPM_FACET.format(id=bangalore)),
                                  resp(503, "down")), jpm_details()]
        res, rows, cs, removed, _t = run_source("JPMorganChase", failing)
        self.assertFalse(res.ok)                                          # one target facet failing holds all
        self.assertEqual(rows, self.rows)

    def test_required_section_parsing(self):
        pref = "<p><b>Preferred qualifications, capabilities, and skills</b></p><ul><li>6+ years of experience in banking</li></ul>"
        cases = {   # description -> (qualifies, band)
            "inline heading, 3+ years": (
                "<p>Required qualifications, capabilities, and skills: 3+ years of experience in operations</p>" + pref,
                (False, None)),
            "inline heading, no cue": (
                "<p><b>Required qualifications, capabilities and skills -</b> 3+ years in finance</p>" + pref,
                (False, None)),
            "heading then list, 1 year": (
                "<p><b>Required qualifications, capabilities, and skills</b></p><ul><li>1 year in trade support</li></ul>" + pref,
                (True, "1")),
            "years only in summary": (
                "<p>As an analyst you bring 3+ years of experience in reconciliations.</p>"
                "<p><b>Required qualifications, capabilities, and skills</b></p><ul><li>Bachelor's degree</li></ul>" + pref,
                (False, None)),
            "years only in preferred": (
                "<p><b>Required qualifications, capabilities, and skills</b></p><ul><li>Experience with Excel</li></ul>" + pref,
                (True, "n")),
            "preferred before required": (
                pref + "<p><b>Required qualifications, capabilities, and skills</b></p><ul><li>1 year of experience</li></ul>",
                (True, "1")),
            "no headings": ("<p>3-5 years of experience in operations.</p>", (False, None)),
            "no closing heading: About-us years need a cue": (
                "<p><b>Required qualifications, capabilities, and skills</b></p><ul><li>1 year of experience</li></ul>"
                "<p>About us: we are a global firm with offices in many countries and a long history of "
                "serving clients for over 25 years.</p>", (True, "1")),
        }
        config = env()[0]
        for name, (desc, (want_ok, want_band)) in cases.items():
            with self.subTest(name):
                info = json.loads(jpm_detail("210809050", desc=desc)[2])["items"][0]
                text, cue = R._jpm_requirements(info)
                self.assertNotIn("banking", text)                         # preferred section never assessed
                ok, band, _s = R.qualifies("Analyst", text, TODAY, ["Bengaluru"], TODAY, config, require_cue=cue)
                self.assertEqual(ok, want_ok)
                if want_ok:
                    self.assertEqual(band, want_band)
        # End to end: the inline-heading 3+ years role is screened, never shown as "no number of years".
        new = [jpm_req("210809051"), jpm_req("210809052")]
        routes = jpm_listing_routes(self.reqs + new) + [jpm_details({
            "210809051": jpm_detail("210809051", desc=cases["inline heading, 3+ years"][0]),
            "210809052": jpm_detail("210809052", desc=cases["years only in preferred"][0])})]
        res, _rows, cs, _rm, _t = run_source("JPMorganChase", routes)
        self.assertEqual([r.job_id for r in res.added], ["210809052"])
        self.assertTrue(res.added[0].experience_text.startswith("No number of years stated"))
        self.assertIn("210809051", cs.screened)

    def test_listing_total_counts_overlapping_facets_once(self):
        bangalore = 300000099999999
        boot = jpm_boot(facets=(("Bengaluru, Karnataka, India", BLR_FACET), ("Bangalore, Karnataka, India", bangalore)))
        overlap = self.reqs[:3]                                         # three roles listed under both facets
        routes = [("GET", R.JPM_SEARCH.format(offset=0, facet=""), boot)] + jpm_listing_routes(self.reqs)[1:] + [
            ("GET", R.JPM_SEARCH.format(offset=0, facet=R.JPM_FACET.format(id=bangalore)),
             jresp({"items": [{"TotalJobsCount": 3, "requisitionList": overlap}]})), jpm_details()]
        res, _rows, cs, _rm, t = run_source("JPMorganChase", routes)
        self.assertTrue(res.ok)
        self.assertEqual((res.listing_total, cs.listing_total, res.detail), (35, 35, "listing 35"))
        self.assertEqual(t.count(f"selectedLocationsFacet={bangalore}"), 1)   # each facet still fetched in full
        # A previous unique total of 35 with the same overlap is not a shrink on the next run.
        _r, _rows, cs2, _rm, _t = run_source("JPMorganChase", routes, cs=cs, now=NOW + R.REFRESH_CADENCE)
        self.assertEqual(cs2.status, "ok")
        short = routes[:-2] + [("GET", R.JPM_SEARCH.format(offset=0, facet=R.JPM_FACET.format(id=bangalore)),
                                jresp({"items": [{"TotalJobsCount": 4, "requisitionList": overlap}]})), jpm_details()]
        res, _rows, _cs, _rm, _t = run_source("JPMorganChase", short)
        self.assertFalse(res.ok)                                        # an incomplete facet still holds
        self.assertEqual(res.detail, "partial pagination")

    def test_invalid_qualifying_row_is_counted_not_screened(self):
        new = [jpm_req("210809061"), jpm_req("210809062")]
        routes = jpm_listing_routes(self.reqs + new) + [jpm_details({
            "210809061": jpm_detail("210809061", title="Analyst, write to hr" + "@example.org"),   # split: repo scan
            "210809062": jpm_detail("210809062", required="5+ years of experience in risk")})]
        res, _rows, cs, _rm, _t = run_source("JPMorganChase", routes)
        self.assertEqual(res.added, [])
        self.assertEqual(res.counts["invalid_rows"], 1)
        self.assertNotIn("210809061", cs.screened)                        # retried on a later run
        self.assertIn("210809062", cs.screened)                           # definitive non-qualifying
        self.assertEqual(cs.detail, "listing 37; 1 invalid row held")
        self.assertNotIn("example.org", cs.detail)

    def test_empty_items_without_count_is_not_a_close(self):
        listed = [r for r in self.reqs if r["Id"] != "210804276"]
        routes = jpm_listing_routes(listed) + [jpm_details({"210804276": jresp({"items": []})})]
        _res, rows, _cs, removed, _t = run_source("JPMorganChase", routes)
        self.assertEqual(removed, [])
        self.assertEqual(by_id(rows)["210804276"].liveness, "unconfirmed")

    def test_outages_and_missing_target_facet_hold(self):
        for name, routes in {
            "search 503": [("GET", R.JPM_SEARCH.format(offset=0, facet=""), resp(503, "down"))],
            "challenge": [("GET", "https://jpmc.fa.oraclecloud.com/*", resp(200, "<html><title>Just a moment...</title></html>"))],
            "no facets": [("GET", R.JPM_SEARCH.format(offset=0, facet=""), jresp({"items": [{"TotalJobsCount": 1, "requisitionList": []}]}))],
            "no target city": [("GET", R.JPM_SEARCH.format(offset=0, facet=""), jpm_boot(facets=(("Mumbai, Maharashtra, India", 1),)))],
            "over max_total": jpm_listing_routes(self.reqs, total=601),
        }.items():
            with self.subTest(name):
                res, rows, cs, removed, _t = run_source("JPMorganChase", routes)
                self.assertEqual(cs.status, "stale")
                self.assertEqual(rows, self.rows)
                self.assertEqual(removed, [])
        state = dataclasses.replace(self.state, jobs=[r for r in self.state.jobs if r.company != "JPMorganChase"])
        res, _rows, _cs, _rm, _t = run_source("JPMorganChase", [
            ("GET", R.JPM_SEARCH.format(offset=0, facet=""), jpm_boot(facets=(("Mumbai, Maharashtra, India", 1),)))],
            state=state)
        self.assertEqual((res.ok, res.detail), (False, "target location facet missing"))   # even with no rows


def gehc_post(jid, path, loc="Bengaluru", posted="Posted Yesterday", title="Graduate Engineer Trainee"):
    return {"title": title, "externalPath": path, "locationsText": loc, "postedOn": posted}


def gehc_list_route(posts, total=None, pages=None):
    total = len(posts) if total is None else total

    def handler(_url, body):
        offset = json.loads(body)["offset"]
        if pages and offset in pages:
            return pages[offset]
        return jresp({"total": total if offset == 0 else 0, "jobPostings": posts[offset:offset + R.WD_PAGE]})
    return ("POST", R.GEHC_API + "/jobs", handler)


def gehc_detail(jid, **kw):
    info = {"jobReqId": jid, "title": "Graduate Engineer Trainee", "posted": True, "canApply": True,
            "startDate": "2026-10-30", "location": "Bengaluru"}
    info.update(kw)
    return jresp({"jobPostingInfo": info})


def gehc_details(overrides=None):
    overrides = overrides or {}

    def handler(url, _body):
        jid = re.search(r"_(R\d+)(?:-\d+)?$", url).group(1)
        return overrides.get(jid, gehc_detail(jid))
    return ("GET", R.GEHC_API + "/job/*", handler)


class GehcWorkdayTests(unittest.TestCase):
    def setUp(self):
        _c, _e, self.state, _t = env()
        self.rows = rows_of(self.state, "GE HealthCare")
        self.paths = {r.job_id: r.canonical_url[len(R.GEHC_FRONT):] for r in self.rows}
        self.posts = [gehc_post(j, p) for j, p in self.paths.items()] + [
            gehc_post(f"R40{i:05d}", f"/job/Chicago/Engineer_R40{i:05d}", loc="Chicago") for i in range(40)]

    def test_complete_global_pagination_liveness_and_exclusive_closing(self):
        routes = [gehc_list_route(self.posts), gehc_details({"R4048338": gehc_detail("R4048338", endDate="2026-11-10")})]
        res, rows, cs, removed, t = run_source("GE HealthCare", routes)
        self.assertTrue(res.ok and res.listing_complete)
        self.assertEqual(sum(1 for m, u in t.calls if m == "POST"), 3)     # 45 postings: 3 pages of 20
        self.assertEqual(res.live, set(self.paths))
        self.assertEqual(res.closing, {"R4048338": "2026-11-09"})          # exclusive endDate: previous day
        self.assertEqual([r.job_id for r in rows], list(self.paths))
        self.assertEqual({r.job_id: r.canonical_url for r in rows}, {r.job_id: r.canonical_url for r in self.rows})
        self.assertEqual(t.count("Chicago"), 0)                            # non-target postings: no details

    def test_partial_or_malformed_listing_holds(self):
        cases = {
            "empty page": {20: jresp({"total": 0, "jobPostings": []})},
            "duplicated page": {20: jresp({"total": 0, "jobPostings": self.posts[:20]})},
            "maintenance": {20: resp(200, "<html><title>Workday</title>down for maintenance</html>")},
            "blocked": {20: resp(403, "<html><title>Access Denied</title></html>")},
            "over max_total": {0: jresp({"total": 2001, "jobPostings": self.posts[:20]})},
        }
        for name, pages in cases.items():
            with self.subTest(name):
                res, rows, cs, removed, _t = run_source("GE HealthCare", [gehc_list_route(self.posts, pages=pages),
                                                                          gehc_details()])
                self.assertFalse(res.ok)
                self.assertEqual(cs.status, "stale")
                self.assertEqual(rows, self.rows)

    def unlisted(self, *ids):
        return [p for p in self.posts if not any(i in p["externalPath"] for i in ids)]

    def test_410_or_false_flags_plus_listing_absence_remove(self):
        # 410 removes even while listed; false flags remove only when the complete listing lacks the ID.
        routes = [gehc_list_route(self.unlisted("R4048338")), gehc_details({
            "R4048338": gehc_detail("R4048338", posted=False), "R4048320": resp(410, "gone")})]
        res, rows, cs, removed, _t = run_source("GE HealthCare", routes)
        self.assertEqual(sorted(removed), ["R4048320", "R4048338"])
        self.assertEqual(set(by_id(rows)), {"R4046068", "R4047718", "R4047774"})
        routes = [gehc_list_route(self.unlisted("R4046068")),
                  gehc_details({"R4046068": gehc_detail("R4046068", canApply=False)})]
        _res, rows, _cs, removed, _t = run_source("GE HealthCare", routes)
        self.assertEqual(removed, ["R4046068"])

    def test_false_flags_while_listed_are_unconfirmed_not_removed(self):
        for name, kw in {"posted false": {"posted": False}, "canApply false": {"canApply": False},
                         "both false": {"posted": False, "canApply": False}}.items():
            with self.subTest(name):
                routes = [gehc_list_route(self.posts), gehc_details({"R4048338": gehc_detail("R4048338", **kw)})]
                res, rows, cs, removed, _t = run_source("GE HealthCare", routes)
                self.assertEqual(removed, [])
                self.assertEqual(res.counts["contradictory"], 1)
                self.assertNotIn("R4048338", res.live | res.closed)
                row = by_id(rows)["R4048338"]
                self.assertEqual((row.liveness, row.absent_runs), ("unconfirmed", 0))
                self.assertEqual(cs.status, "ok")
        # The same contradiction on the next run still never removes; delisting plus false flags does.
        routes = [gehc_list_route(self.posts), gehc_details({"R4048338": gehc_detail("R4048338", posted=False)})]
        _res, rows, cs, _rm, _t = run_source("GE HealthCare", routes)
        state = dataclasses.replace(self.state, jobs=[r for r in self.state.jobs if r.company != "GE HealthCare"] + rows,
                                    companies={**self.state.companies, "GE HealthCare": cs})
        _res, rows2, _cs, removed, _t = run_source("GE HealthCare", routes, state=state, now=NOW + R.REFRESH_CADENCE)
        self.assertEqual((removed, by_id(rows2)["R4048338"].liveness), ([], "unconfirmed"))
        delisted = [gehc_list_route(self.unlisted("R4048338")), routes[1]]
        _res, _rows3, _cs, removed, _t = run_source("GE HealthCare", delisted, state=state, now=NOW + R.REFRESH_CADENCE)
        self.assertEqual(removed, ["R4048338"])

    def test_mass_close_is_held_by_the_removal_guard(self):
        chicago = [p for p in self.posts if "Chicago" in p["externalPath"]]
        routes = [gehc_list_route(chicago), gehc_details({j: gehc_detail(j, posted=False) for j in self.paths})]
        res, rows, cs, removed, _t = run_source("GE HealthCare", routes)
        self.assertEqual(cs.detail, R.REMOVAL_DETAIL)
        self.assertEqual(rows, self.rows)

    def test_unclear_detail_never_removes(self):
        unlisted = [p for p in self.posts if "R4048338" not in p["externalPath"]]
        for name, out in {"maintenance": resp(200, "<html>down for maintenance</html>"), "500": resp(500, "x"),
                          "non-json": resp(200, "plain", "text/plain"), "flags missing": gehc_detail(
                              "R4048338", posted=None, canApply=None)}.items():
            with self.subTest(name):
                res, rows, cs, removed, _t = run_source("GE HealthCare", [gehc_list_route(unlisted), gehc_details(
                    {"R4048338": out})])
                self.assertEqual(removed, [])
                self.assertEqual(by_id(rows)["R4048338"].liveness, "unconfirmed")
                self.assertEqual(by_id(rows)["R4048338"].absent_runs, 0)

    def test_404_and_two_complete_absences_remove(self):
        unlisted = [p for p in self.posts if "R4048338" not in p["externalPath"]]
        routes = [gehc_list_route(unlisted), gehc_details({"R4048338": resp(404, "{}", "application/json")})]
        _res, rows, cs, removed, _t = run_source("GE HealthCare", routes)
        self.assertEqual((by_id(rows)["R4048338"].absent_runs, removed), (1, []))
        state = dataclasses.replace(self.state, jobs=[r for r in self.state.jobs if r.company != "GE HealthCare"] + rows,
                                    companies={**self.state.companies, "GE HealthCare": cs})
        _res, rows, _cs, removed, _t = run_source("GE HealthCare", routes, state=state,
                                                  now=NOW + R.REFRESH_CADENCE)
        self.assertEqual(removed, ["R4048338"])

    def test_additions_target_and_multi_location_with_exclusions(self):
        new = [gehc_post("R4049001", "/job/IND19-01-Bengaluru-EPIP-122-Phase-II/Trainee_R4049001",
                         loc="IND19-01-Bengaluru-EPIP 122 (Phase II)"),
               gehc_post("R4049002", "/job/Pune/Engineer_R4049002", loc="2 Locations"),
               gehc_post("R4049003", "/job/Bengaluru/Engineer_R4049003"),
               gehc_post("R4049004", "/job/Bengaluru/Lead-Engineer_R4049004", title="Lead Engineer"),
               gehc_post("R4049005", "/job/Bengaluru/Old_R4049005", posted="Posted 30+ Days Ago")]
        routes = [gehc_list_route(self.posts + new), gehc_details({
            "R4049001": gehc_detail("R4049001", endDate="2026-11-15", location="IND19-01-Bengaluru-EPIP 122 (Phase II)",
                                    jobDescription="<p>0-1 years of experience in service.</p>"),
            "R4049002": gehc_detail("R4049002", location="Pune", additionalLocations=["Bengaluru"],
                                    jobDescription="<p>1 year of relevant experience.</p>"),
            "R4049004": gehc_detail("R4049004", title="Lead Engineer",
                                    jobDescription="<p>8+ years of engineering experience.</p>")})]
        res, rows, cs, removed, t = run_source("GE HealthCare", routes, excluded={"R4049003"})
        added = by_id(res.added)
        self.assertEqual(set(added), {"R4049001", "R4049002"})
        self.assertEqual((added["R4049001"].closing_date, added["R4049001"].location), ("2026-11-14", "Bengaluru"))
        self.assertEqual(added["R4049002"].location, "Bengaluru and Pune")
        self.assertEqual(added["R4049001"].canonical_url,
                         R.GEHC_FRONT + "/job/IND19-01-Bengaluru-EPIP-122-Phase-II/Trainee_R4049001")
        self.assertIn("R4049004", cs.screened)
        self.assertEqual(t.count("R4049003"), 0)                           # excluded: never fetched
        self.assertEqual(t.count("R4049005"), 0)                           # 30+ days: never fetched
        self.assertEqual(t.count("Chicago"), 0)

    def test_site_code_locations_show_clean_city_names(self):
        config = env()[0]
        self.assertEqual(R._gehc_place("IND19-01-Bengaluru-EPIP 122 (Phase II)", config), "Bengaluru")
        self.assertEqual(R._gehc_place("IND23-01-Pune-Hinjewadi Phase 1", config), "Pune")
        self.assertEqual(R._gehc_place("IND07-02-New Delhi", config), "New Delhi")
        self.assertEqual(R._gehc_place("IND23-01", config), "")              # bare code: dropped
        self.assertEqual(R._gehc_place("IND23-01-Navi Mumbai (Ghansoli)", config), "Navi Mumbai")
        self.assertEqual(R._gehc_place("Chicago, Illinois", config), "Chicago")
        new = [gehc_post("R4049011", "/job/IND36-01-Bengaluru-Kalyani-Platina-Magnolia/Engineer_R4049011",
                         loc="3 Locations")]
        routes = [gehc_list_route(self.posts + new), gehc_details({"R4049011": gehc_detail(
            "R4049011", location="IND36-01-Bengaluru-Kalyani Platina Magnolia",
            additionalLocations=["IND23-01-Pune-Hinjewadi Phase 1", "IND19-01-Bengaluru-EPIP 122 (Phase II)",
                                 "IND44-02"],
            jobDescription="<p>1 year of relevant experience.</p>")})]
        res, _rows, _cs, _rm, _t = run_source("GE HealthCare", routes)
        row = by_id(res.added)["R4049011"]
        self.assertEqual(row.location, "Bengaluru and Pune")
        self.assertNotRegex(row.location, r"[A-Z]{3}\d")

    def test_invalid_qualifying_row_is_counted_not_screened(self):
        new = [gehc_post("R4049021", "/job/Bengaluru/Eng.ineer_R4049021"),           # URL shape the row rejects
               gehc_post("R4049022", "/job/Bengaluru/Engineer_R4049022"),            # forbidden text in title
               gehc_post("R4049023", "/job/Bengaluru/Engineer_R4049023")]            # 6 years: not qualifying
        routes = [gehc_list_route(self.posts + new), gehc_details({
            "R4049022": gehc_detail("R4049022", title="Engineer, write to hr" + "@example.org"),   # split: repo scan
            "R4049023": gehc_detail("R4049023", jobDescription="<p>6+ years of experience.</p>")})]
        res, _rows, cs, _rm, _t = run_source("GE HealthCare", routes)
        self.assertEqual(res.added, [])
        self.assertEqual(res.counts["invalid_rows"], 2)
        self.assertNotIn("R4049021", cs.screened)
        self.assertNotIn("R4049022", cs.screened)
        self.assertIn("R4049023", cs.screened)                             # definitive non-qualifying
        self.assertTrue(cs.detail.endswith("; 2 invalid rows held"), cs.detail)
        self.assertNotIn("example.org", cs.detail + json.dumps(res.counts))
        _r, _rows, _cs, _rm, t2 = run_source("GE HealthCare", routes, cs=cs)
        self.assertEqual((t2.count("R4049021"), t2.count("R4049023")), (1, 0))   # invalid row retried

    def test_ambiguous_run_resets_the_absence_streak(self):
        unlisted = self.unlisted("R4048338")
        gone = [gehc_list_route(unlisted), gehc_details({"R4048338": resp(404, "{}", "application/json")})]
        unclear = [gehc_list_route(unlisted), gehc_details({"R4048338": resp(200, "plain", "text/plain")})]
        failed = [gehc_list_route(self.posts, pages={0: resp(503, "down")}), gehc_details()]
        for name, middle in {"unclear detail": unclear, "failed listing": failed}.items():
            with self.subTest(name):
                state, now = self.state, NOW
                for routes in (gone, middle, gone):
                    _res, rows, cs, removed, _t = run_source("GE HealthCare", routes, state=state, now=now)
                    self.assertEqual(removed, [])
                    state = dataclasses.replace(state, jobs=[r for r in state.jobs if r.company != "GE HealthCare"]
                                                + rows, companies={**state.companies, "GE HealthCare": cs})
                    now += R.REFRESH_CADENCE
                self.assertEqual(by_id(rows)["R4048338"].absent_runs, 1)      # streak restarted after the gap
                _res, _rows, _cs, removed, _t = run_source("GE HealthCare", gone, state=state, now=now)
                self.assertEqual(removed, ["R4048338"])                      # two consecutive healthy absences


def zs_job(rid, city="Bengaluru", posted="2026-10-30T04:41:00+0000", open_=True, quals="", slug=None, extra=()):
    return {"slug": slug or rid, "req_id": rid, "title": "Business Technology Associate", "city": city,
            "country": "India", "additional_locations": [{"city": c, "country": "India"} for c in extra],
            "posted_date": posted, "searchable": open_, "applyable": open_, "qualifications": quals,
            "meta_data": {"canonical_url": f"https://jobs.zs.com/jobs/{slug or rid}?lang=en-us"}}


def zs_list_route(jobs, total=None, pages=None):
    total = len(jobs) if total is None else total

    def handler(url, _body):
        page = int(re.search(r"page=(\d+)", url).group(1))
        if pages and page in pages:
            return pages[page]
        chunk = jobs[(page - 1) * R.ZS_PAGE: page * R.ZS_PAGE]
        return resp(200, json.dumps({"totalCount": total, "count": total, "jobs": [{"data": d} for d in chunk]}),
                    "application/json; charset=utf-8")
    return ("GET", "https://jobs.zs.com/api/jobs?page=*", handler)


def zs_page(jid):
    return resp(200, f"<html><head><title>Job</title></head><body>Job {jid}</body></html>")


ZS_404 = resp(404, "<html><head><title>Not found</title></head><body>/jobs/24123</body></html>")


class ZsJibeTests(unittest.TestCase):
    def setUp(self):
        _c, _e, self.state, _t = env()
        self.rows = rows_of(self.state, "ZS")
        self.others = [zs_job(str(60000 + i), city="Pune", posted="2026-09-01T00:00:00+0000") for i in range(24)]

    def run_zs(self, jobs, *extra, **kw):
        return run_source("ZS", [zs_list_route(jobs, **{k: v for k, v in kw.items() if k in ("total", "pages")}),
                                 *extra], **{k: v for k, v in kw.items() if k in ("state", "cs", "excluded", "now")})

    def test_complete_pagination_keeps_24123_live_without_detail(self):
        jobs = self.others[:12] + [zs_job("24123", posted="2026-10-29T04:41:00+0000")] + self.others[12:]
        res, rows, cs, removed, t = self.run_zs(jobs)
        self.assertTrue(res.ok and res.listing_complete)
        self.assertEqual(t.count("/api/jobs?page="), 3)                   # 25 jobs, 10 per page
        self.assertEqual(res.live, {"24123"})
        self.assertEqual(rows[0].to_dict() | {"last_seen": "2026-10-31"}, self.rows[0].to_dict())
        self.assertEqual(t.count("/jobs/24123"), 0)

    def test_partial_shifting_or_blocked_listing_holds(self):
        jobs = self.others[:12] + [zs_job("24123")] + self.others[12:]
        cases = {
            "total changed": {"pages": {2: resp(200, json.dumps({"totalCount": 26, "jobs": [{"data": d} for d in jobs[10:20]]}),
                                                "application/json")}},
            "empty page": {"pages": {3: resp(200, json.dumps({"totalCount": 25, "jobs": []}), "application/json")}},
            "duplicate page": {"pages": {3: resp(200, json.dumps({"totalCount": 25, "jobs": [{"data": d} for d in jobs[:5]]}),
                                                 "application/json")}},
            "403 edge block": {"pages": {1: resp(403, "<html><title>Access Denied</title></html>")}},
            "challenge": {"pages": {1: resp(200, "<html><title>Just a moment...</title></html>")}},
        }
        for name, kw in cases.items():
            with self.subTest(name):
                res, rows, cs, removed, _t = self.run_zs(jobs, **kw)
                self.assertFalse(res.ok)
                self.assertEqual(cs.status, "stale")
                self.assertEqual(rows, self.rows)

    def test_missing_detail_plus_two_absences_remove(self):
        routes_detail = ("GET", "https://jobs.zs.com/jobs/24123*", ZS_404)
        _res, rows, cs, removed, _t = self.run_zs(self.others, routes_detail)
        self.assertEqual((rows[0].absent_runs, rows[0].liveness, removed), (1, "absent", []))
        state = dataclasses.replace(self.state, jobs=[r for r in self.state.jobs if r.company != "ZS"] + rows,
                                    companies={**self.state.companies, "ZS": cs})
        _res, rows, _cs, removed, _t = self.run_zs(self.others, routes_detail, state=state, now=NOW + R.REFRESH_CADENCE)
        self.assertEqual(removed, ["24123"])

    def test_absent_but_live_or_blocked_detail_keeps_row(self):
        # A live-looking page is not live evidence without the listing: kept, unconfirmed, never counted.
        res, rows, cs, removed, _t = self.run_zs(self.others, ("GET", "https://jobs.zs.com/jobs/24123*", zs_page("24123")))
        self.assertEqual((rows[0].liveness, rows[0].absent_runs, removed), ("unconfirmed", 0, []))
        self.assertNotIn("24123", res.live)
        for name, out in {"403": resp(403, "<html><title>Access Denied</title></html>"),
                          "challenge": resp(200, "<html><title>Just a moment...</title></html>"),
                          "timeout": TimeoutError("slow")}.items():
            with self.subTest(name):
                res, rows, cs, removed, _t = self.run_zs(self.others, ("GET", "https://jobs.zs.com/jobs/24123*", out))
                self.assertFalse(res.ok)
                self.assertEqual(rows, self.rows)
        not_open = self.others + [zs_job("24123", open_=False)]
        res, rows, cs, removed, _t = self.run_zs(not_open, ("GET", "https://jobs.zs.com/jobs/24123*", zs_page("24123")))
        self.assertEqual((rows[0].liveness, removed), ("unconfirmed", []))   # listed but not applyable
        # Present in the listing (not applyable) with a 404 page: not an absence, so never counted.
        res, rows, cs, removed, _t = self.run_zs(not_open, ("GET", "https://jobs.zs.com/jobs/24123*", ZS_404))
        self.assertEqual((rows[0].liveness, rows[0].absent_runs, removed), ("unconfirmed", 0, []))

    def chain(self, details, jobs=None):
        """Consecutive ZS runs, one cadence apart, each with its own 24123 job-page response."""
        state, now, removed_all, rows = self.state, NOW, [], self.rows
        for out in details:
            _res, rows, cs, removed, _t = self.run_zs(self.others if jobs is None else jobs,
                                                      ("GET", "https://jobs.zs.com/jobs/24123*", out),
                                                      state=state, now=now)
            removed_all += removed
            state = dataclasses.replace(state, jobs=[r for r in state.jobs if r.company != "ZS"] + rows,
                                        companies={**state.companies, "ZS": cs})
            now += R.REFRESH_CADENCE
        return rows, removed_all

    SHELLS = {
        "closed phrase": "<html><head><title>Job</title></head><body>This job is no longer available 24123</body></html>",
        "shell with i18n strings": ('<html><head><title>Careers</title><link rel="canonical" '
                                    'href="https://jobs.zs.com/jobs/24123"></head><body><div id="root"></div>'
                                    '<script>var t={"gone":"This job is no longer available"}</script></body></html>'),
        "url echo only": '<html><head><title>Careers</title></head><body>Loading /jobs/24123?lang=en-us</body></html>',
        "generic shell": '<html><head><title>Careers</title></head><body><div id="root"></div></body></html>',
    }

    def test_closed_looking_or_shell_pages_never_count_or_remove(self):
        for name, body in self.SHELLS.items():
            with self.subTest(name):
                rows, removed = self.chain([resp(200, body)] * 3)
                self.assertEqual(removed, [])
                self.assertEqual((rows[0].liveness, rows[0].absent_runs), ("unconfirmed", 0))

    def test_exact_gone_needs_two_consecutive_complete_absences(self):
        gone410 = resp(410, "<html><head><title>Gone</title></head><body>gone</body></html>")
        self.assertEqual(self.chain([gone410, gone410])[1], ["24123"])
        self.assertEqual(self.chain([ZS_404, gone410])[1], ["24123"])
        for name, body in self.SHELLS.items():          # a page in between resets the streak
            with self.subTest(name):
                rows, removed = self.chain([ZS_404, resp(200, body), ZS_404])
                self.assertEqual((removed, rows[0].absent_runs), ([], 1))
        # A blocked run in between holds the source and also resets the streak, keeping the row.
        rows, removed = self.chain([ZS_404, resp(403, "<html><title>Access Denied</title></html>"), ZS_404])
        self.assertEqual((removed, rows[0].absent_runs), ([], 1))
        # Listed again (even unapplyable) between two 404s: not consecutive absences.
        state_rows, removed = self.chain([ZS_404])
        self.assertEqual(state_rows[0].absent_runs, 1)
        rows, removed = self.chain([ZS_404, ZS_404], jobs=self.others + [zs_job("24123", open_=False)])
        self.assertEqual((removed, rows[0].absent_runs), ([], 0))

    def test_candidate_with_closed_looking_page_is_neither_added_nor_screened(self):
        quals = "<p>0-2 years of relevant work experience.</p>"
        jobs = [zs_job("24123"), zs_job("68990", quals=quals)]
        for name, body in self.SHELLS.items():           # closed phrase, ID only in a link/script/URL echo
            with self.subTest(name):
                shell = resp(200, body.replace("24123", "68990"))
                res, _rows, cs, _rm, _t = self.run_zs(jobs, ("GET", "https://jobs.zs.com/jobs/68990*", shell))
                self.assertEqual((res.added, cs.screened), ([], {}))
        res, _rows, cs, _rm, _t = self.run_zs(jobs, ("GET", "https://jobs.zs.com/jobs/68990*", ZS_404))
        self.assertEqual(res.added, [])
        self.assertIn("68990", cs.screened)              # exact 404: verified gone

    def test_additions_are_open_target_verified_and_not_excluded(self):
        quals = "<p>What you'll bring: 0-2 years of relevant work experience in analytics.</p>"
        new = [zs_job("68999", quals=quals, extra=["Pune"]), zs_job("68998", quals=quals),
               zs_job("68997", quals=quals, open_=False), zs_job("68996", city="Pune", quals=quals),
               zs_job("68995", quals=quals, slug="other-slug"), zs_job("68994", quals="<p>5+ years of experience</p>"),
               zs_job("68993", quals=quals)]
        routes = [("GET", "https://jobs.zs.com/jobs/68999?lang=en-us", zs_page("68999")),
                  ("GET", "https://jobs.zs.com/jobs/68998?lang=en-us", ZS_404)]
        res, rows, cs, removed, t = self.run_zs([zs_job("24123")] + new, *routes, excluded={"68993"})
        self.assertEqual([r.job_id for r in res.added], ["68999"])
        row = res.added[0]
        self.assertEqual((row.location, row.canonical_url, row.closing_date, row.experience_band),
                         ("Bengaluru and Pune", "https://jobs.zs.com/jobs/68999", None, "2"))
        self.assertEqual(t.count("68993"), 0)
        self.assertEqual(t.count("/jobs/68997"), 0)
        self.assertEqual(t.count("/jobs/68996"), 0)
        self.assertEqual(t.count("/jobs/68994"), 0)
        self.assertIn("68998", cs.screened)                              # verified gone: not re-checked
        _r, _rows, _cs, _rm, t2 = self.run_zs([zs_job("24123")] + new, *routes, excluded={"68993"}, cs=cs)
        self.assertEqual(t2.count("/jobs/68998"), 0)

    def test_budget_covers_a_full_listing_at_the_crawl_delay(self):
        config, _e, state, _t = env()
        cfg = config.sources["ZS"]
        self.assertGreaterEqual(cfg["budget_s"], 60)
        self.assertLessEqual(source_minimum("ZS", config, state), cfg["budget_s"])

    def test_zs_requests_respect_crawl_delay(self):
        config, _e, _s, _t = env()
        self.assertGreaterEqual(config.hosts["jobs.zs.com"].min_delay_s, 5.0)
        self.assertTrue(config.hosts["jobs.zs.com"].robots)
        now = [0.0]
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            now[0] += s
        t = FakeTransport([zs_list_route(self.others)], robots=resp(200, "User-agent: *\nAllow: /\n", "text/plain"))
        f = R.Fetcher(config, 300, transport=t, sleep=sleep, clock=lambda: now[0], rand=lambda: 0.0)
        self.assertIsInstance(R._zs_listing(config.sources["ZS"], f), tuple)
        self.assertEqual(len(sleeps), 3)                                 # robots + 3 pages: 3 waits
        self.assertTrue(all(s >= 5.0 for s in sleeps))


LATENCY_S = 0.5      # assumed server time per request, on top of the enforced per-host delay


def _per_request(config, host):
    return config.hosts[host].min_delay_s * 1.25 + LATENCY_S      # 25% maximum jitter


def source_minimum(company, config, state):
    """Seconds a source needs for a full run at its configured caps: robots.txt, every listing page
    up to max_total, one detail per current row, and every new-detail check. It is a lower bound for
    budget_s; a run cut short by its deadline is partial and keeps last-known-good rows."""
    cfg, rows = config.sources[company], len(rows_of(state, company))

    def pages(total, size):
        return -(-total // size)
    if company == "Siemens":
        n = (1 + rows + 2 + cfg["max_list_pages"] + cfg["max_list_pages"] // cfg["probe_every_pages"] + 1
             + cfg["max_new_details"])
        return n * _per_request(config, "jobs.siemens.com")
    if company == "JPMorganChase":       # boot search plus two target-city facets (Bengaluru, Bangalore)
        n = 1 + 1 + 2 * pages(cfg["max_total"], R.JPM_PAGE) + rows + cfg["max_new_details"]
        return n * _per_request(config, "jpmc.fa.oraclecloud.com")
    if company == "GE HealthCare":
        n = 1 + pages(cfg["max_total"], R.WD_PAGE) + rows + cfg["max_new_details"]
        return n * _per_request(config, "gehc.wd5.myworkdayjobs.com")
    if company == "GE Vernova":
        n = 1 + 1 + pages(cfg["max_total"], R.GE_PAGE) + rows + cfg["max_new_checks"]
        return n * _per_request(config, "careers.gevernova.com")
    if company == "IQVIA":               # listing + ID search per row on Workday; page checks on the front site
        n = 2 + len(cfg["search_terms"]) * pages(cfg["max_total"], R.WD_PAGE) + 2 * rows + 2 * cfg["max_new_details"]
        return n * _per_request(config, "iqvia.wd1.myworkdayjobs.com")
    if company == "ZS":
        n = 1 + pages(cfg["max_total"], R.ZS_PAGE) + rows + cfg["max_new_checks"]
        return n * _per_request(config, "jobs.zs.com")
    raise AssertionError(company)


class AdapterIsolationTests(unittest.TestCase):
    def test_one_failing_adapter_does_not_affect_the_others(self):
        config, excluded, state, _t = env()
        gehc_rows = rows_of(state, "GE HealthCare")
        posts = [gehc_post(r.job_id, r.canonical_url[len(R.GEHC_FRONT):]) for r in gehc_rows]
        t = FakeTransport([
            ("GET", "https://jpmc.fa.oraclecloud.com/*", resp(503, "down")),
            gehc_list_route(posts), gehc_details(),
            zs_list_route([zs_job("24123")]),
        ])
        after, outcomes = R.run_refresh(state, config, excluded, NOW, factory_for(config, t))
        status = {c: s.status for c, s in after.companies.items()}
        self.assertEqual((status["JPMorganChase"], status["GE HealthCare"], status["ZS"]), ("stale", "ok", "ok"))
        self.assertEqual(rows_of(after, "JPMorganChase"), rows_of(state, "JPMorganChase"))
        self.assertEqual(len(rows_of(after, "GE HealthCare")), 5)
        self.assertEqual(rows_of(after, "ZS")[0].liveness, "live")

    def test_adapter_exception_is_contained(self):
        config, excluded, state, _t = env()
        original = R.RUNNERS["zs_jibe_api"]
        R.RUNNERS["zs_jibe_api"] = lambda *a: (_ for _ in ()).throw(KeyError("boom"))
        try:
            after, _o = R.run_refresh(state, config, excluded, NOW, factory_for(config, FakeTransport(
                [zs_list_route([zs_job("24123")])])))
        finally:
            R.RUNNERS["zs_jibe_api"] = original
        self.assertEqual(after.companies["ZS"].detail, "source error (KeyError)")
        self.assertEqual(rows_of(after, "ZS"), rows_of(state, "ZS"))

    def test_run_budget_caps_sources_and_holds_late_ones(self):
        config, excluded, state, _t = env()
        self.assertEqual(R.source_budget({"budget_s": 600}, 1200, 150), 600)
        self.assertEqual(R.source_budget({"budget_s": 600}, 650, 210), 650 - 210)
        self.assertEqual(R.source_floor(config.sources["ZS"]), 60)      # mode floor, not the 30s default
        self.assertEqual(R.source_floor(config.sources["JPMorganChase"]), 30)
        budgets = {}

        def factory(company, cfg):
            budgets[company] = cfg["budget_s"]
            return R.Fetcher(config, cfg["budget_s"], transport=FakeTransport(), sleep=lambda _s: None)
        ticks = iter([0.0, 0.0] + [R.RUN_BUDGET_S - 10] * 50)        # time jumps after the first source
        after, _o = R.run_refresh(state, config, excluded, NOW, factory, clock=lambda: next(ticks))
        self.assertEqual(list(budgets), ["Siemens"])
        self.assertEqual(budgets["Siemens"], config.sources["Siemens"]["budget_s"])
        for c in ("JPMorganChase", "GE HealthCare", "GE Vernova", "IQVIA", "ZS"):
            self.assertEqual(after.companies[c].detail, "run time budget exhausted", c)
            self.assertEqual(rows_of(after, c), rows_of(state, c))

    def test_configured_budgets_fit_the_run_budget_and_workflow_timeout(self):
        config, _e, state, _t = env()
        timeout = int(re.search(r"timeout-minutes: (\d+)", R.read_text(
            os.path.join(ROOT, ".github", "workflows", "refresh.yml"))).group(1))
        self.assertLessEqual(R.RUN_BUDGET_S, (timeout - 4) * 60)
        live = [c for c in config.companies if config.sources[c]["mode"] != "static"]
        self.assertLessEqual(sum(config.sources[c]["budget_s"] for c in live), R.RUN_BUDGET_S)
        for c in live:
            with self.subTest(c):
                self.assertLessEqual(source_minimum(c, config, state), config.sources[c]["budget_s"])
        # With every source spending its whole budget, each still starts with its full allowance.
        left = R.RUN_BUDGET_S
        for i, c in enumerate(live):
            reserve = sum(R.source_floor(config.sources[x]) for x in live[i + 1:])
            self.assertEqual(R.source_budget(config.sources[c], left, reserve), config.sources[c]["budget_s"])
            left -= config.sources[c]["budget_s"]

    def test_late_source_below_its_own_floor_is_held(self):
        config, excluded, state, _t = env()
        started = []

        def factory(company, cfg):
            started.append(company)
            return R.Fetcher(config, cfg["budget_s"], transport=FakeTransport(), sleep=lambda _s: None)
        # 45s left when ZS starts: above the 30s default floor, below the ZS crawl-delay floor of 60s.
        ticks = iter([0.0] * 6 + [R.RUN_BUDGET_S - 45] * 50)     # run deadline + five sources, then ZS
        clock = lambda: next(ticks)   # noqa: E731
        after, _o = R.run_refresh(state, config, excluded, NOW, factory, clock=clock)
        self.assertNotIn("ZS", started)
        self.assertEqual(after.companies["ZS"].detail, "run time budget exhausted")
        self.assertEqual(rows_of(after, "ZS"), rows_of(state, "ZS"))

    def test_config_rejects_oversubscribed_budgets(self):
        data = R.read_json(os.path.join(ROOT, R.CONFIG_PATH))
        data["companies"][1]["budget_s"] += 30
        with self.assertRaises(R.ConfigError) as ctx:
            R.load_config(data)
        self.assertIn("sum", str(ctx.exception))
        data = R.read_json(os.path.join(ROOT, R.CONFIG_PATH))
        data["companies"][5]["budget_s"] = 45                       # below the ZS crawl-delay floor
        with self.assertRaises(R.ConfigError):
            R.load_config(data)


class SeedConversionTests(unittest.TestCase):
    def test_converted_sources_keep_rows_dates_and_page(self):
        config, state, template = R.load_all(ROOT)
        for c in ("JPMorganChase", "GE HealthCare", "ZS"):
            self.assertIn(state.companies[c].status, ("seed", "ok", "stale"), c)
        doc = R.render(state, template, config)
        if all(state.companies[c].status == "seed" for c in ("JPMorganChase", "GE HealthCare", "ZS")):
            # Until their first real refresh the page shows them exactly as before.
            for c in ("JPMorganChase", "GE HealthCare", "ZS"):
                self.assertIn(f'"{c}":"not auto-checked"', doc)
        _fc, frozen, _ft = frozen_env()
        self.assertEqual({c: s.status for c, s in frozen.companies.items()
                          if c in ("JPMorganChase", "GE HealthCare", "ZS")},
                         {"JPMorganChase": "seed", "GE HealthCare": "seed", "ZS": "seed"})

    def test_first_successful_refresh_clears_the_label(self):
        config, _e, state, template = env()
        state.companies["ZS"] = dataclasses.replace(state.companies["ZS"], status="ok",
                                                    last_success="2026-10-31T18:00+05:30")
        self.assertNotIn('"ZS":', R.render(state, template, config).split("STALE=")[1].split(";")[0])


# --------------------------------------------------------------------------- source health panel

REF = dt.datetime(2026, 10, 31, 22, 47, tzinfo=R.IST)


def iso(when: dt.datetime) -> str:
    return when.astimezone(R.IST).isoformat(timespec="seconds")


def health_section(doc: str) -> str:
    return re.search(r'<section class="health".*?</section>', doc, re.S).group(0)


def health_summary(doc: str) -> str:
    """The source-array summary line inside the API terminal (summary spans, #h-next and #h-late)."""
    return re.search(r'<p class="hs">(.*?)</p>', doc, re.S).group(1)


def status_block(doc: str) -> str:
    """The rows of the terminal's `signal status` block (Last refresh, Result, Openings, Freshness, Next run)."""
    return re.search(r'<dl class="kvs">(.*?)</dl>', doc, re.S).group(1)


def api_dialog(doc: str) -> str:
    return re.search(r'<dialog id="api".*?</dialog>', doc, re.S).group(0)


def main_script(doc: str) -> str:
    return re.findall(r"<script>(.*?)</script>", doc, re.S)[-1]


def page_outside_dialog(doc: str) -> str:
    """The rendered markup with the <style>, the <script>s and the Requests dialog removed."""
    body = doc.replace(style_of(doc), "").replace(api_dialog(doc), "")
    return re.sub(r"<script>.*?</script>", "", body, flags=re.S)


def dashboard_fragments(doc: str) -> dict:
    """The Python-rendered status fragments: the terminal status block and the search-panel LED."""
    return {"summary": status_block(doc),
            "led": re.search(r'<p class="label-sys src-led".*?</p>', doc).group(0)}


CHANNEL = re.compile(r'<li class="ch" data-h="([a-z]+)"><span class="ch-n" aria-hidden="true">CH\d{2}</span>'
                     r'<span class="ch-id"><strong class="ch-name">([^<]+)</strong>(.*?)</li>', re.S)


def channels(section: str) -> dict:
    """{company: (freshness state, rest of the channel markup)} in page order."""
    return {c: (s, rest) for s, c, rest in CHANNEL.findall(section)}


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        self.config, _e, self.state, self.template = env()
        self.state.checked_at = iso(REF)

    def set_company(self, name, **kw):
        self.state.companies[name] = dataclasses.replace(self.state.companies[name], **kw)

    def all_states_state(self):
        self.set_company("Siemens", status="ok", last_success=iso(REF), last_attempt=iso(REF))
        self.set_company("JPMorganChase", status="stale", last_success=iso(REF - dt.timedelta(hours=2)),
                         last_attempt=iso(REF))
        self.set_company("GE HealthCare", status="ok", last_success=iso(REF - dt.timedelta(hours=7)),
                         last_attempt=iso(REF - dt.timedelta(hours=7)))
        self.set_company("GE Vernova", status="stale", last_success=None, last_attempt=iso(REF))
        self.set_company("IQVIA", status="seed", last_success=None, last_attempt=None)
        self.set_company("ZS", status="stale", last_success=iso(REF - dt.timedelta(days=3)), last_attempt=iso(REF))

    def test_classification_states(self):
        c, ref = R.classify_source, REF
        fresh, old = iso(ref - dt.timedelta(hours=1)), iso(ref - dt.timedelta(hours=8))
        self.assertEqual(c("ok", fresh, iso(ref), ref), "live")
        self.assertEqual(c("stale", fresh, iso(ref), ref), "degraded")
        self.assertEqual(c("ok", old, old, ref), "stale")
        self.assertEqual(c("stale", old, iso(ref), ref), "stale")
        self.assertEqual(c("stale", None, iso(ref), ref), "failed")
        for status in ("seed", "static", "ok"):
            self.assertEqual(c(status, None, None, ref), "unknown", status)
        self.assertEqual(c("seed", fresh, None, ref), "unknown")
        self.assertEqual(c("static", None, None, None), "unknown")
        self.assertEqual(c("ok", fresh, fresh, None), "unknown")

    def test_threshold_is_named_and_exact_boundary_is_fresh(self):
        self.assertEqual(R.FRESHNESS_THRESHOLD, dt.timedelta(hours=6, minutes=30))
        exact = iso(REF - R.FRESHNESS_THRESHOLD)
        over = iso(REF - R.FRESHNESS_THRESHOLD - dt.timedelta(seconds=1))
        self.assertEqual(R.classify_source("ok", exact, exact, REF), "live")
        self.assertEqual(R.classify_source("stale", exact, iso(REF), REF), "degraded")
        self.assertEqual(R.classify_source("ok", over, over, REF), "stale")
        self.assertEqual(R.classify_source("stale", over, iso(REF), REF), "stale")

    def test_future_success_is_clamped(self):
        future = iso(REF + dt.timedelta(hours=3))
        self.set_company("Siemens", status="ok", last_success=future, last_attempt=future)
        h = R.source_health(self.state, self.config, REF)[0]
        self.assertEqual((h.state, h.age), ("live", dt.timedelta(0)))
        self.assertEqual(R.age_text(dt.timedelta(minutes=-90)), "0m ago")
        self.assertIn('<span class="age">0m ago</span>', R.render(self.state, self.template, self.config))

    def test_age_text(self):
        self.assertEqual(R.age_text(dt.timedelta(minutes=59)), "59m ago")
        self.assertEqual(R.age_text(dt.timedelta(hours=6, minutes=30)), "6h 30m ago")
        self.assertEqual(R.age_text(dt.timedelta(hours=47, minutes=59)), "47h 59m ago")
        self.assertEqual(R.age_text(dt.timedelta(days=3, hours=1)), "3d ago")

    def test_schedule_slots_and_date_boundaries(self):
        self.assertEqual(R.SCHEDULE_CRON, "23 */6 * * *")
        workflow = R.read_text(os.path.join(ROOT, ".github", "workflows", "refresh.yml"))
        self.assertIn(f'cron: "{R.SCHEDULE_CRON}"', workflow)
        self.assertEqual(R.SCHEDULE_UTC_HOURS, tuple(range(0, 24, 6)))
        ist = lambda *a: dt.datetime(*a, tzinfo=R.IST)   # noqa: E731
        cases = [
            (ist(2026, 10, 31, 5, 52), ist(2026, 10, 31, 5, 53)),
            (ist(2026, 10, 31, 5, 53), ist(2026, 10, 31, 11, 53)),     # strictly after a slot
            (ist(2026, 10, 31, 11, 53, 1), ist(2026, 10, 31, 17, 53)),
            (ist(2026, 10, 31, 17, 53), ist(2026, 10, 31, 23, 53)),
            (ist(2026, 10, 31, 22, 47), ist(2026, 10, 31, 23, 53)),
            (ist(2026, 10, 31, 23, 53), ist(2026, 11, 1, 5, 53)),       # IST date rolls over
            (ist(2026, 12, 31, 23, 54), ist(2027, 1, 1, 5, 53)),        # year rolls over
            (ist(2026, 11, 1, 0, 0), ist(2026, 11, 1, 5, 53)),
            (dt.datetime(2026, 11, 1, 0, 23, tzinfo=dt.timezone.utc), ist(2026, 11, 1, 11, 53)),
        ]
        for ref, expected in cases:
            with self.subTest(ref):
                got = R.next_scheduled(ref)
                self.assertEqual(got, expected)
                self.assertEqual(got.utcoffset(), dt.timedelta(hours=5, minutes=30))
        self.assertEqual({f"{R.next_scheduled(ist(2026, 10, 31, h, 0)):%H:%M}" for h in (1, 7, 13, 19)},
                         {"05:53", "11:53", "17:53", "23:53"})

    def test_next_scheduled_uses_cron_not_last_attempt(self):
        self.set_company("Siemens", status="ok", last_success=iso(REF), last_attempt=iso(REF))
        doc = R.render(self.state, self.template, self.config)
        section, block = health_section(doc), status_block(doc)
        self.assertIn('<dt>Next run</dt><dd><time id="h-next" datetime="2026-10-31T23:53:00+05:30">'
                      '31 Oct 23:53 IST</time><span id="h-late"></span> · scheduled, not guaranteed</dd>', block)
        self.assertIn('<dt>Last refresh</dt><dd><time id="st-at" datetime="2026-10-31T22:47:00+05:30">'
                      '31 Oct 22:47 IST</time> <span class="age" id="st-age">0m ago</span></dd>', block)
        self.assertNotIn("04:47", section + block)  # last_attempt + 6h
        for frag in ('id="h-next"', 'id="h-late"', 'id="st-at"', 'id="st-age"'):
            self.assertEqual(doc.count(frag), 1, frag)
        self.assertNotIn("h-next", section)
        self.state.checked_at = None
        for name in self.config.companies:
            self.set_company(name, last_success=None, last_attempt=None)
        doc = R.render(self.state, self.template, self.config)
        block = status_block(doc)
        self.assertIn("<dt>Next run</dt><dd>05:53, 11:53, 17:53, 23:53 IST · scheduled, not guaranteed</dd>", block)
        self.assertIn("<dt>Last refresh</dt><dd>Auto-check pending</dd>", block)
        for frag in ('id="h-next"', 'id="h-late"', 'id="st-at"', 'id="st-age"'):
            self.assertNotIn(frag, doc, frag)

    def test_published_counts_match_rendered_rows_not_listing_total(self):
        self.set_company("JPMorganChase", status="ok", listing_total=987654, last_success=iso(REF),
                         last_attempt=iso(REF))
        old = dataclasses.replace(rows_of(self.state, "JPMorganChase")[0],
                                  posted_date=(TODAY - dt.timedelta(days=45)).isoformat())
        self.state.jobs[self.state.jobs.index(rows_of(self.state, "JPMorganChase")[0])] = old
        doc = R.render(self.state, self.template, self.config)
        rows = json.loads(re.search(r"\nconst jobs=(\[\n.*?\n\]);\n", doc, re.S).group(1))
        visible = [r for r in rows if r[5] <= self.config.max_age_days]
        section = health_section(doc)
        chs = channels(section)
        for company in self.config.companies:
            n = sum(1 for r in visible if r[0] == company)
            self.assertIn(f'<div class="ch-p"><dt>Published</dt><dd class="num">{n}</dd></div>', chs[company][1])
        self.assertNotIn("987654", doc)
        self.assertIn(f'<span class="hs-r">{len(visible)} records</span>', section)
        self.assertIn(f'<dd id="total">{len(visible)}</dd>', doc)
        self.assertLess(len(visible), len(rows))

    def test_private_and_unsafe_fields_never_render(self):
        self.set_company("Siemens", status="stale", detail="zzdetailsentinel source error (KeyError)",
                         last_success=iso(REF - dt.timedelta(hours=1)), last_attempt=iso(REF),
                         stale_since="2026-10-29T03:17+05:30", last_discovery_attempt="2026-10-28T04:19+05:30",
                         listing_total=876543, screened={"999991": "2026-10-30"},
                         removal_hold={"at": "2026-10-30T09:11+05:30", "ids": ["999992", "999993"]})
        doc = R.render(self.state, self.template, self.config)
        for needle in ("zzdetailsentinel", "KeyError", "876543", "999991", "999992", "999993",
                       "2026-10-29T03:17", "29 Oct 03:17", "2026-10-28T04:19", "28 Oct 04:19",
                       "2026-10-30T09:11", "30 Oct 09:11", "removal", "screened", "listing_total",
                       "stale_since", "last_discovery", "excluded_public", "EXCLUDED_PUBLIC_IDS_JSON", "alerts.json"):
            self.assertNotIn(needle, doc, needle)
        self.assertEqual([f.name for f in dataclasses.fields(R.SourceHealth)],
                         ["company", "state", "published", "attempt_at", "attempt", "success_at", "age"])
        self.assertEqual(set(json.loads(R.health_js())),
                         {"fresh_ms", "grace_ms", "severity", "glyphs", "labels", "late", "stale"})
        self.assertIn("Refresh held, cached data kept", health_section(doc))

    def test_raw_detail_never_reaches_html_for_any_source(self):
        # CompanyState.detail holds raw reasons (exceptions, HTTP codes, URLs); none of it may render,
        # even when it mimics fixed page wording or a connector label.
        hosts = sorted(self.config.hosts)
        for i, name in enumerate(self.config.companies):
            self.set_company(name, status="stale", last_success=iso(REF - dt.timedelta(hours=i)),
                             last_attempt=iso(REF),
                             detail=f"zzraw{i} https://{hosts[i % len(hosts)]}/wday/cxs ValueError HTTP 429")
        doc = R.render(self.state, self.template, self.config)
        for i in range(len(self.config.companies)):
            self.assertNotIn(f"zzraw{i}", doc)
        for needle in ("ValueError", "HTTP 429", "/wday/cxs", "https://"):
            self.assertNotIn(needle, health_section(doc), needle)

    def test_semantic_text_and_glyph_markup(self):
        self.all_states_state()
        doc = R.render(self.state, self.template, self.config)
        section = health_section(doc)
        expected = {"Siemens": "live", "JPMorganChase": "degraded", "GE HealthCare": "stale",
                    "GE Vernova": "failed", "IQVIA": "unknown", "ZS": "stale"}
        chs = channels(section)
        self.assertEqual({c: s for c, (s, _r) in chs.items()}, expected)
        for s, (glyph, label) in {"live": ("■", "LIVE"), "degraded": ("▲", "DEGRADED/CACHED"), "stale": ("◷", "STALE"),
                                  "failed": ("×", "FAILED"), "unknown": ("◇", "UNKNOWN/UNCHECKED")}.items():
            self.assertIn(f'<span class="st st-{s}"><span class="g" aria-hidden="true">{glyph}</span> {label}</span>',
                          section)
        glyphs = list(re.finditer("[■▲◷×◇]", section))
        self.assertTrue(glyphs)
        for m in glyphs:      # every decorative glyph sits alone inside an aria-hidden span
            self.assertTrue(section[:m.start()].endswith('<span class="g" aria-hidden="true">'))
            self.assertTrue(section[m.end():].startswith("</span>"))
        self.assertIn('<dt>Freshness</dt><dd>worst <span class="st st-failed">', status_block(doc))
        self.assertNotIn('class="hc"', section)
        self.assertRegex(chs["GE Vernova"][1], r'<time datetime="[^"]+">[^<]+</time> · <span class="oc" '
                                               r'data-o="failed">No successful automated refresh yet, snapshot '
                                               r'shown</span>')
        self.assertIn('</time> · <span class="oc" data-o="accepted">Successful refresh</span>', chs["Siemens"][1])
        self.assertIn('</time> · <span class="oc" data-o="held">Refresh held, cached data kept</span>',
                      chs["JPMorganChase"][1])
        self.assertIn('<dd><span class="oc" data-o="pending">No attempt recorded</span></dd>', chs["IQVIA"][1])
        self.assertIn("<dd>None yet</dd>", chs["IQVIA"][1])
        # Every channel names its fields with dt/dd pairs, so each value has a programmatic label.
        for company, (_s, rest) in chs.items():
            self.assertEqual(re.findall(r"<dt>([^<]+)</dt>", rest), ["Published", "Latest attempt", "Last good data"],
                             company)
        self.assertIn('<ol class="ch-list" aria-label="Automated source connectors">', section)
        for value in re.findall(r'<time[^>]* datetime="([^"]+)"', section):
            self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+05:30$")
            self.assertIsNotNone(dt.datetime.fromisoformat(value))
        words = re.findall(r'</time> · <span class="oc" data-o="[a-z]+">([A-Za-z ,;]+)<', section)
        self.assertEqual(len(words), 5)                      # every recorded attempt, IQVIA has none
        for word in words:
            self.assertIn(word, R.ATTEMPT_TEXT.values())
        for gone in ("Not completed", "overdue"):
            self.assertNotIn(gone, doc, gone)
        for gone in ("Completed", "outage", " down", "unavailable"):
            self.assertNotIn(gone, section.lower() if gone.islower() else section, gone)

    def test_company_labels_match_health_panel(self):
        self.all_states_state()
        self.set_company("GE HealthCare", status="static", last_success=None, last_attempt=None)
        doc = R.render(self.state, self.template, self.config)
        labels = json.loads(re.search(r",STALE=(\{.*?\}),H=", doc).group(1))
        self.assertEqual(labels, {
            "JPMorganChase": "cached data",                    # held 2h ago: fresh, so not "stale"
            "GE Vernova": "automated check unverified",       # failed: no success yet
            "IQVIA": "not auto-checked",                       # seed snapshot
            "GE HealthCare": "not auto-checked",               # static
            "ZS": "stale since Oct 28",                        # held, data older than 6h30
        })
        self.assertEqual(labels, R.stale_labels(self.state, self.config))
        rows = {c: s for c, (s, _r) in channels(health_section(doc)).items()}
        for company, label in labels.items():
            if label.startswith("stale"):
                self.assertEqual(rows[company], "stale")
            else:
                self.assertNotEqual(rows[company], "stale", company)
        self.assertNotIn("Siemens", labels)                    # LIVE: no label
        for word in ("outage", "down", "error", "unavailable", "failed"):
            self.assertFalse(any(word in v for v in labels.values()), word)
        self.assertLessEqual(set(labels.values()) - {"stale since Oct 28"}, set(R.COMPANY_LABELS.values()))

    def test_company_label_turns_stale_only_past_threshold(self):
        exact = REF - R.FRESHNESS_THRESHOLD
        self.set_company("JPMorganChase", status="stale", last_success=iso(exact), last_attempt=iso(REF))
        self.assertEqual(R.stale_labels(self.state, self.config)["JPMorganChase"], "cached data")
        self.set_company("JPMorganChase", last_success=iso(exact - dt.timedelta(minutes=1)))
        self.assertEqual(R.stale_labels(self.state, self.config)["JPMorganChase"], "stale since Oct 31")
        # A scheduler gap ages an accepted source too, with the same wording.
        self.set_company("Siemens", status="ok", last_success=iso(REF - dt.timedelta(hours=7)),
                         last_attempt=iso(REF - dt.timedelta(hours=7)))
        self.assertEqual(R.stale_labels(self.state, self.config)["Siemens"], "stale since Oct 31")

    def test_attempt_wording_is_fixed(self):
        self.assertEqual(R.ATTEMPT_TEXT, {
            "accepted": "Successful refresh", "held": "Refresh held, cached data kept",
            "first_snapshot": "No successful automated refresh yet, snapshot shown",
            "first_empty": "No successful automated refresh yet", "unrecorded": "Outcome not recorded",
            "none": "No attempt recorded"})
        t = iso(REF)
        self.assertEqual(R.attempt_outcome("ok", t, t, 3), "accepted")
        self.assertEqual(R.attempt_outcome("stale", t, t, 3), "held")
        self.assertEqual(R.attempt_outcome("stale", t, t, 0), "held")
        self.assertEqual(R.attempt_outcome("stale", None, t, 3), "first_snapshot")
        self.assertEqual(R.attempt_outcome("stale", None, t, 0), "first_empty")
        self.assertEqual(R.attempt_outcome("seed", None, t, 3), "unrecorded")
        self.assertEqual(R.attempt_outcome("ok", t, None, 3), "none")
        # A first failure with no published rows, and its reason never reaches the page.
        self.set_company("ZS", status="stale", detail="zzreason HTTP 503", last_success=None, last_attempt=t)
        self.state.jobs = [r for r in self.state.jobs if r.company != "ZS"]
        doc = R.render(self.state, self.template, self.config)
        self.assertIn('</time> · <span class="oc" data-o="failed">No successful automated refresh yet</span></dd>',
                      channels(health_section(doc))["ZS"][1])
        self.assertNotIn("zzreason", doc)
        self.assertNotIn("503", health_section(doc))

    def test_scope_note_contextualises_failed(self):
        doc = R.render(self.state, self.template, self.config)
        section = health_section(doc)
        self.assertIn('<p class="h-scope">Statuses describe this ledger’s automated source connectors and cached '
                      'data, not employer site availability. Freshness and the latest refresh outcome are shown '
                      'separately.</p>', section)
        self.assertLess(section.index('class="h-scope"'), section.index('<ol class="ch-list"'))
        self.assertEqual(R.HEALTH_LABELS["failed"], "FAILED")
        self.assertEqual(R.FRESHNESS_THRESHOLD, dt.timedelta(hours=6, minutes=30))
        dialog = api_dialog(doc)
        self.assertIn("no refresh history kept; only the latest recorded attempt is shown", dialog)
        # The key lives once, in the Requests terminal's `signal sources --help` block, after the source rows.
        self.assertEqual(doc.count('<dl class="h-key">'), 1)
        self.assertNotIn("h-key", page_outside_dialog(doc))
        self.assertNotIn('id="key"', doc)
        self.assertLess(dialog.index('<ol class="ch-list"'), dialog.index('<dl class="h-key">'))
        card = dialog[dialog.index("signal sources --help</p>"):]
        card = card[:card.index("</div>", card.index('<p class="h-note">'))]
        self.assertIn('<h3 class="vh">What the freshness statuses mean</h3><dl class="h-key">', card)
        self.assertIn('<p class="h-note">Data counts as fresh for 6h 30m after its last successful check; the next '
                      'scheduled time is not guaranteed.</p>', card)
        self.assertEqual(doc.count("every 6"), 1)
        key = re.search(r'<dl class="h-key">(.*?)</dl>', card, re.S).group(1)
        self.assertEqual(re.findall(r'<div data-s="([a-z]+)"><dt>', key), list(R.HEALTH_STATES))
        for s in R.HEALTH_STATES:
            self.assertIn(f'{R.badge_html(s)}</dt><dd>{R.HEALTH_MEANINGS[s].format(limit="6h 30m")}</dd>', key)
        self.assertIn("FAILED</span></dt><dd>Refresh was attempted, but no successful automated refresh is recorded "
                      "yet.", key)
        self.assertEqual(key.count("6h 30m"), 3)                       # live, degraded, stale

    def test_source_order_matches_config(self):
        section = health_section(R.render(self.state, self.template, self.config))
        self.assertEqual(list(channels(section)), list(self.config.companies))
        self.assertEqual(re.findall(r'<span class="ch-n" aria-hidden="true">(CH\d{2})</span>', section),
                         [f"CH{i:02d}" for i in range(1, len(self.config.companies) + 1)])

    def test_no_external_assets_and_restrained_effects(self):
        doc = R.render(self.state, self.template, self.config)
        self.assertEqual(set(re.findall(r'\brole="([^"]+)"', doc)), {"status", "search", "group"})
        self.assertEqual(doc.count('role="status"'), 1)
        style = style_of(doc)
        for banned in ("@import", "url(", "@font-face", "radial-gradient", "conic-gradient", "backdrop-filter",
                       "scanline"):
            self.assertNotIn(banned, style.lower(), banned)
        self.assertNotRegex(doc, r"(?i)<link\b|<img\b|<iframe\b|<svg\b|<script\s+src|\bfetch\(|XMLHttpRequest|"
                                 r"setInterval|requestAnimationFrame|<table")
        self.assertNotRegex(R.read_text(os.path.join(ROOT, R.TEMPLATE_PATH)), r"https?://")
        # Fonts are named and fall back to system faces; nothing is downloaded.
        fonts = dict(re.findall(r"--font-(sans|display|mono):([^;]+);", style))
        self.assertEqual(set(fonts), {"sans", "display", "mono"})
        self.assertTrue(fonts["sans"].startswith('"IBM Plex Sans",system-ui'))
        self.assertTrue(fonts["sans"].endswith("sans-serif") and fonts["display"].endswith("sans-serif"))
        self.assertTrue(fonts["mono"].startswith('"JetBrains Mono",ui-monospace') and fonts["mono"].endswith("monospace"))
        self.assertIn('font-feature-settings:"liga" 0,"calt" 0,"zero","ss01"', style)
        # Square corners everywhere except the round status LED.
        self.assertIn("*,:before,:after{box-sizing:border-box;border-radius:0}", style)
        self.assertEqual(re.findall(r"border-radius:([^;}]+)", style), ["0", "50%", "50%"])
        self.assertIn(".led{display:inline-block;flex:none;width:6px;height:6px;border-radius:50%;", style)
        self.assertIn(".dot{position:relative;display:block;flex:none;width:12px;height:12px;padding:0;border:0;"
                      "border-radius:50%;", style)                       # the terminal's traffic lights
        # Flat dim backdrop, no blur behind the dialog.
        self.assertEqual(re.findall(r"::backdrop\{([^}]*)\}", style), ["background:hsl(var(--void-deep)/.72)",
                                                                       "animation:overlay-fade-in .2s ease-out",
                                                                       "animation:none!important"])
        # Blank brand marks: empty bordered boxes, no glyph or image.
        self.assertIn('<span class="wm">job signal</span><span class="mark" aria-hidden="true"></span>', doc)
        self.assertEqual(doc.count("job signal"), 1)             # header wordmark only; no Ledger card
        for gone in ("av48", "idn", "tagrow", "#bengaluru", "Ledger summary"):
            self.assertNotIn(gone, doc, gone)
        rules = dict((tuple(s), b) for s, b in css_rules(media_blocks(style)[0]))
        self.assertIn("width:100px;height:13px;border:1px dashed hsl(var(--hull))", rules[(".mark",)])
        # Chamfered panels and buttons with a signal hairline; the header status panel is gone.
        self.assertIn("clip-path:polygon(0 0,calc(100% - 14px) 0,100% 14px,100% 100%,0 100%)", rules[(".panel",)])
        self.assertIn("clip-path:polygon(0 0,calc(100% - 8px) 0,100% 8px,100% 100%,8px 100%,0 calc(100% - 8px))",
                      rules[(".chamfer-btn",)])
        self.assertNotIn("hud-corners status", doc)
        self.assertIn('<section class="panel panel-signal srch" id="cmd"', doc)

    def test_source_dialog_structure(self):
        doc = R.render(self.state, self.template, self.config)
        # Everything backend-facing lives in one native "Requests" dialog that starts closed at every width.
        self.assertIn('<dialog id="api" class="term" aria-labelledby="api-h"><div class="term-win" tabindex="-1">', doc)
        self.assertNotRegex(doc, r"<dialog\b[^>]*\bopen\b")
        self.assertEqual(doc.count("<dialog"), 1)
        dialog = api_dialog(doc)
        # macOS-style title bar: a real close button (red) and two decorative lights, then a centred title.
        self.assertIn('<div class="term-bar"><span class="dots"><button class="dot dot-x" id="api-x" type="button" '
                      'aria-label="Close"></button><span class="dot dot-m" aria-hidden="true"></span><span class="dot '
                      'dot-z" aria-hidden="true"></span></span><span id="api-h">ops@job-signal — requests — zsh</span>'
                      '<span class="term-esc" aria-hidden="true">esc</span></div>', dialog)
        self.assertEqual(dialog.count("<button"), 1)
        # Four commands and a final prompt, in order; each block is one staggered output group. The status
        # block comes first, so the reader sees when the latest refresh ran and what it found.
        prompt = '<p class="pl"><span class="pu">ops@job-signal</span> <span class="pp">~ %</span> '
        order = [prompt + "signal status</p>", '<dl class="kvs">', "<dt>Last refresh</dt>", "<dt>Result</dt>",
                 "<dt>Openings</dt>", "<dt>Freshness</dt>", "<dt>Next run</dt>", 'id="h-next"', 'id="h-late"',
                 prompt + "signal sources --verbose</p>", '<section class="health" id="health"', '<p class="hs">',
                 '<p class="h-scope">', '<div class="ch-head"',
                 '<ol class="ch-list"', prompt + "signal sources --help</p>", '<dl class="h-key">',
                 '<p class="h-note">', prompt + "signal schedule</p>",
                 '<p class="kv">every 6 h · cron 23 */6 * * * UTC · no refresh history kept; only the latest recorded '
                 'attempt is shown</p>', prompt + '<span class="caret" aria-hidden="true"></span></p>']
        self.assertEqual([dialog.index(s) for s in order], sorted(dialog.index(s) for s in order))
        self.assertEqual(re.findall(r'<div class="tb" style="--d:(\d)">', dialog), list("01234"))
        self.assertIn('<div class="tb" style="--d:0">' + prompt + 'signal status</p>\n<dl class="kvs">', dialog)
        self.assertLess(doc.index('<dialog id="api"'), doc.index('<dl class="kvs">'))
        self.assertLess(doc.index("</dl></div>", doc.index('<dl class="kvs">')),
                        doc.index(prompt + "signal sources --verbose</p>"))
        self.assertEqual(doc.count('<dl class="kvs">'), 1)
        for frag in ('id="health"', 'id="h-next"', 'id="h-late"', 'id="st-at"', 'id="st-age"', 'class="h-key"',
                     'class="kvs"'):
            self.assertEqual(doc.count(frag), dialog.count(frag), frag)
            self.assertIn(frag, dialog, frag)
        for gone in ('id="st-pre"', "signal clock", "signal window", 'class="pre"', 'class="ps"'):
            self.assertNotIn(gone, doc, gone)
        # Two openers (header, compact header), each progressively enabled; every close path returns focus
        # to the opener used.
        self.assertIn('<button class="chamfer-btn req" id="req-btn" type="button" aria-haspopup="dialog" '
                      'aria-controls="api" hidden><span class="gl" aria-hidden="true">&gt;_</span> Requests</button>', doc)
        self.assertIn('<button class="th-m" id="req-btn-m" type="button" aria-haspopup="dialog" aria-controls="api" '
                      'hidden>[ requests ]</button>', doc)
        self.assertEqual(doc.count('aria-controls="api"'), 2)
        self.assertEqual(doc.count('aria-haspopup="dialog"'), 2)
        for gone in ('id="api-btn"', 'id="api-btn2"', 'id="api-btn3"', 'class="x"', 'class="cta-s"', 'id="qv-req"'):
            self.assertNotIn(gone, doc, gone)
        script = main_script(doc)
        self.assertEqual(script.count("showModal()"), 1)
        for snippet in ('const dlg=$("#api"),win=$("#api .term-win");',
                        '[$("#req-btn"),$("#req-btn-m")].forEach(b=>{b.hidden=false;'
                        'b.addEventListener("click",()=>{opener=b;dlg.showModal();win.focus()})})',
                        'dlg.addEventListener("click",e=>{if(e.target===dlg)dlg.close()})',
                        '$("#api-x").addEventListener("click",()=>dlg.close())',
                        'dlg.addEventListener("close",()=>{if(opener)opener.focus()})'):
            self.assertIn(snippet, script)
        style = style_of(doc)
        base, blocks = media_blocks(style)
        rules = dict((tuple(s), b) for s, b in css_rules(base))
        self.assertIn("body:has(dialog[open]){overflow:hidden}", base)
        for decl in ("position:fixed", "margin:auto", "background:transparent"):
            self.assertIn(decl, rules[("dialog.term",)], decl)
        for decl in ("display:flex;flex-direction:column", "width:min(920px,94vw)", "max-height:86vh",
                     "border:1px solid hsl(var(--hull-2))", "background:hsl(264 28% 5%/.98)",
                     "box-shadow:var(--shadow-dialog)"):
            self.assertIn(decl, rules[(".term-win",)], decl)
        self.assertIn("dialog.term[open]{display:flex;animation:dialog-pop-in .2s", base)
        for decl in ("height:36px", "background:var(--btn-grad)", "border-bottom:1px solid hsl(var(--hull))"):
            self.assertIn(decl, rules[(".term-bar",)], decl)
        self.assertEqual((rules[(".dot-x",)], rules[(".dot-m",)], rules[(".dot-z",)]),
                         ("--dc:var(--bad);cursor:pointer", "--dc:var(--warn)", "--dc:var(--ok)"))
        for decl in ("overflow:auto", "padding:14px 16px", "font-size:12px", "line-height:1.6"):
            self.assertIn(decl, rules[(".term-out",)], decl)
        self.assertFalse([s for s, _b in css_rules(re.sub(r"@media[^{]*\{", "", style))
                          if any(x in (".pre", ".ps", ".term .pre") or x.startswith(".pre ") for x in s)])
        self.assertIn("grid-template-columns:8rem minmax(0,1fr)", rules[(".h-key div",)])
        self.assertIn("border-top:1px solid hsl(var(--hull)/.6)", rules[(".h-key div",)])
        # Phones get a full-screen sheet with a sticky title bar and stacked source rows.
        sheet = blocks[SHEET_MQ]
        sheet_rules = dict((tuple(s), b) for s, b in css_rules(sheet))
        for decl in ("inset:0", "width:100vw", "height:100dvh", "max-height:none", "margin:0"):
            self.assertIn(decl, sheet_rules[("dialog.term",)], decl)
        self.assertIn("box-shadow:none", sheet_rules[(".term-win",)])
        self.assertIn("position:sticky;top:0", sheet_rules[(".term-bar",)])
        self.assertIn("grid-template-columns:auto minmax(0,1fr) auto", sheet_rules[(".term-bar",)])  # title fits at 320
        self.assertIn("font-size:11px;line-height:1.55", sheet_rules[(".term-out",)])
        self.assertIn(".h-key div{grid-template-columns:minmax(0,1fr);", sheet)
        self.assertIn("dialog.term[open]{animation:sheet-up .2s", sheet)
        self.assertIn(".hdr-r{display:none}", sheet)
        self.assertIn(".ch-head{display:none}", sheet)
        self.assertIn(".ch-d{grid-column:2/-1;grid-row:2;display:flex;flex-wrap:wrap", sheet)
        # Desktop: one grid line per source on shared columns.
        self.assertIn(".ch-head,.ch{display:grid;grid-template-columns:var(--chc);", base)
        self.assertEqual(len(re.search(r"--chc:([^;]+);", base).group(1).split(" ")), len(R.CHANNEL_COLUMNS))
        self.assertIn("grid-template-columns:subgrid", rules[(".ch-d",)])
        section = health_section(doc)
        self.assertIn('<div class="ch-head" aria-hidden="true">' + "".join(f"<span>{c}</span>" for c in R.CHANNEL_COLUMNS)
                      + "</div>", section)
        # The summary is one wrapped mono line (title and records); every fragment stays visible.
        self.assertIn("display:flex;flex-wrap:wrap", rules[(".hs",)])
        self.assertIn("overflow-wrap:anywhere", rules[(".ch-name",)])
        for cls in (".hs-t", ".hs-r", ".kvs", "#st-at", "#st-age"):
            self.assertNotIn(cls, hidden_selectors(style), cls)
        summary = health_summary(doc)
        self.assertEqual(re.findall(r'<span class="(hs-[a-z]|hc)"', summary), ["hs-t", "hs-r"])
        self.assertEqual(doc.count('<span class="hs-t">'), 1)
        self.assertNotIn("h-next", summary)
        self.assertEqual(doc.count('id="h-late"'), 1)
        for gone in ("hs-a", "hs-n", 'class="hc"', ".hc", "h-rw", "hs-w", "hs-c", "hs-x", "h-roll", "h-rl"):
            self.assertNotIn(gone, doc, gone)
        # Status block: a two-column key/value grid on desktop, one column on phones.
        self.assertEqual(rules[(".kvs",)], "display:grid;grid-template-columns:7.5rem minmax(0,1fr);gap:4px 12px;"
                                           "margin:0;font-size:12px;line-height:1.6")
        self.assertEqual(rules[(".kvs div",)], "display:contents")
        self.assertIn("overflow-wrap:anywhere", rules[(".kvs dd",)])
        self.assertIn(".kvs{grid-template-columns:minmax(0,1fr);gap:0}.kvs dt{margin-top:6px}", sheet)

    def test_compact_breakpoint_is_shared_and_selects_stay_visible(self):
        doc = R.render(self.state, self.template, self.config)
        js_mq = re.search(r'mq=matchMedia\("([^"]+)"\)', doc).group(1)
        self.assertEqual(js_mq, COMPACT_MQ)
        style = style_of(doc)
        base, blocks = media_blocks(style)
        self.assertIn(COMPACT_MQ, blocks)                                 # same spelling in CSS and JS
        for (w, h), compact in {(320, 568): True, (390, 844): True, (768, 1024): True, (1023, 768): True,
                                (1024, 768): False, (1440, 900): False}.items():
            with self.subTest(f"{w}x{h}"):
                self.assertEqual(media_matches(js_mq, w, h), compact)
        # The script reads it only when Enter is pressed in the search box, to jump to the results.
        script = main_script(doc)
        self.assertIn('q.addEventListener("keydown",e=>{if(e.key==="Enter"&&mq.matches){e.preventDefault();q.blur();'
                      '$("#feed").scrollIntoView({behavior:RM.matches?"auto":"smooth",block:"start"})}})', script)
        self.assertEqual(script.count("scrollIntoView"), 1)
        for banned in ("mq.addEventListener", "addListener", '"resize"', "onresize", ".onchange"):
            self.assertNotIn(banned, script, banned)
        # On phones the three selects start collapsed behind a Filters toggle; the search box, Reset, the
        # stats line and the active-filter chips stay visible. Desktop never collapses them, and nothing
        # hides the dialog's controls. The toggle sets the initial state once (no resize listener).
        self.assertIn('<button class="ob ftog" id="ftog" type="button" aria-controls="fsel" aria-expanded="false" '
                      'hidden><span>Filters (<span id="fn">0</span>)</span><span class="ftog-c" aria-hidden="true">▾'
                      '</span></button>', doc)
        self.assertIn(".ftog{display:none}", base)
        self.assertNotIn("data-fc", base)
        self.assertIn(".srch[data-fc] .fsel{display:none}", blocks[SHEET_MQ])
        self.assertIn(".ftog:not([hidden]){display:flex;justify-content:space-between;width:100%;min-height:32px;",
                      blocks[SHEET_MQ])
        self.assertIn('cmd.toggleAttribute("data-fc",matchMedia("(max-width:767px)").matches);ftog.hidden=false;',
                      script)
        self.assertIn('ftog.addEventListener("click",()=>{const open=cmd.toggleAttribute("data-fc");'
                      'ftog.setAttribute("aria-expanded",String(!open));if(!open)$("#age").focus()})', script)
        self.assertIn('$("#fn").textContent=on.filter(([k])=>k!=="query"&&k!=="company").length;', script)
        self.assertFalse({s for s in hidden_selectors(style)
                          if any(k in s for k in ("api", "term", "field", "query", "reset"))})
        self.assertIn("[hidden]{display:none!important}", base)
        # The older page skeletons do not come back; the terminal chrome exists only inside the dialog.
        for gone in ('class="rail"', 'class="top"', 'class="stk"', 'class="lg-head"', 'class="sec-h"',
                     'class="tile"', 'id="sync"', 'class="pulse"', 'id="srcs"', 'class="sr"', 'id="key"',
                     'class="st-note"', 'id="src-led-text"'):
            self.assertNotIn(gone, doc, gone)
        outside = page_outside_dialog(doc)
        # The only disclosure widgets are the terminal's per-source pull histories.
        self.assertNotIn("<details", outside)
        self.assertEqual(api_dialog(doc).count("<details"), api_dialog(doc).count('<details class="pulls">'))
        for chrome in ('class="term-win"', 'class="dot', 'class="caret"', 'class="tb"', "ops@job-signal"):
            self.assertNotIn(chrome, outside, chrome)
            self.assertIn(chrome, api_dialog(doc), chrome)

    def test_theme_text_contrast(self):
        style = style_of(R.render(self.state, self.template, self.config))
        dark, light = theme_tokens(style)
        text = ("steel", "steel-2", "steel-muted", "signal", "ok", "bad", "warn", "info", "band-n", "band-1", "band-2")
        for name, t in (("dark", dark), ("light", light)):
            # Page background (cards, aside cards): every text colour meets 4.5:1.
            for fg in text:
                with self.subTest(f"{name} {fg} on void"):
                    self.assertGreaterEqual(contrast(t[fg], t["void"]), 4.5)
            # Panel and header-strip fills: neutral and state text meet 4.5:1.
            for fg in text:
                if fg != "signal":
                    with self.subTest(f"{name} {fg} on plate"):
                        self.assertGreaterEqual(contrast(t[fg], t["plate"]), 4.5)
            for fg in ("steel", "steel-2"):
                self.assertGreaterEqual(contrast(t[fg], t["plate-2"]), 4.5, f"{name} {fg}")
            # The specified signal token is about 4.3:1 on dark plates: fine for its large and UI uses (3:1).
            self.assertGreaterEqual(contrast(t["signal"], t["plate"]), 3.0, name)
            self.assertGreaterEqual(contrast(t["void"], t["signal"]), 4.5, name)     # chamfer button
        rules = dict((tuple(s), b) for s, b in css_rules(media_blocks(style)[0]))
        self.assertIn("background:hsl(var(--signal));color:hsl(var(--void))", rules[(".chamfer-btn",)])
        self.assertNotIn((".tag",), rules)

    def test_live_region_is_debounced_and_stats_mirror(self):
        doc = R.render(self.state, self.template, self.config)
        self.assertIn('<div class="readout vh" role="status" aria-live="polite">', doc)
        base, _blocks = media_blocks(style_of(doc))
        clip = dict((tuple(s), b) for s, b in css_rules(base))[(".vh",)]
        for decl in ("position:absolute", "width:1px", "height:1px", "overflow:hidden", "clip-path:inset(50%)"):
            self.assertIn(decl, clip, decl)
        all_hidden = hidden_selectors(style_of(doc))
        self.assertNotIn(".readout", all_hidden)
        self.assertNotIn(".vh", all_hidden)
        # Visible totals update at once; the live region is debounced to 450ms while filtering.
        script = main_script(doc)
        for snippet in ('put("total",visibleJobs.length)', 'put("companies",cc)', 'put("today",today)',
                        'announce(visibleJobs.length,cc,f.join(" / "))',
                        'clearTimeout(annT);if(booted)annT=setTimeout(w,450)', '$("#status-total").textContent=t',
                        '$("#fbar").hidden=!on.length',
                        'const put=(id,v)=>{const s=String(v),e=document.getElementById(id);'
                        'if(e.textContent!==s)e.textContent=s};'):
            self.assertIn(snippet, script, snippet)
        self.assertLess(script.index('put("total",'), script.index("announce(visibleJobs.length"))
        self.assertEqual(script.count("setTimeout("), 1)
        # The search-panel counters are the only visible copy; the left-aside mirrors are gone.
        for gone in ("-side", "week-side", "in the last 7 days"):
            self.assertNotIn(gone, doc, gone)
        for mid in ("total", "companies", "today"):
            self.assertEqual(len(re.findall(rf'<dd id="{mid}">\d+</dd>', doc)), 1, mid)
        self.assertIn('<dl class="stats"><div class="stat"><dt>Openings</dt><dd id="total">', doc)
        self.assertIn('<div class="fbar" id="fbar" hidden><span id="fcount">No active filters</span>'
                      '<ul class="chips" id="chips" aria-label="Active filters"></ul></div>', doc)
        self.assertLess(doc.index('id="cmd"'), doc.index('id="fbar"'))
        self.assertLess(doc.index('id="fbar"'), doc.index('id="feed"'))

    def test_phone_health_summary_is_bounded_for_any_state_mix(self):
        self.all_states_state()
        doc = R.render(self.state, self.template, self.config)
        summary, block = health_summary(doc), status_block(doc)
        self.assertIn('<dt>Freshness</dt><dd>worst <span class="st st-failed">'
                      '<span class="g" aria-hidden="true">×</span> FAILED</span></dd>', block)
        # Per-state counts live only in the rows; the summary carries no count spans.
        self.assertNotIn('class="hc"', summary)
        for cls in (".hs-t", ".st", "#h-next", "#h-late", ".hs-r", ".kvs", "#st-at", "#st-age"):
            self.assertNotIn(cls, hidden_selectors(style_of(doc)), cls)
        # The terminal sheet wraps the summary and the status block at any width; their text is fixed wording.
        records = sum(R.published_counts(self.state, self.config).values())
        self.assertEqual(re.sub(r"<[^>]+>", "", summary), f"// 01 Source array{records} records")
        new_today = sum(1 for r in self.state.jobs if r.first_seen == self.state.as_of.isoformat()
                        and (self.state.as_of - dt.date.fromisoformat(r.posted_date)).days <= self.config.max_age_days)
        self.assertEqual(re.sub(r"<[^>]+>", "", block),
                         "Last refresh31 Oct 22:47 IST 0m ago"
                         "Result2/6 sources accepted · 2 held · 1 failed · 1 pending"
                         f"Openings{records} published · {new_today} first seen today"
                         "Freshnessworst × FAILED"
                         "Next run31 Oct 23:53 IST · scheduled, not guaranteed")

    def test_last_good_data_has_visual_separator(self):
        self.set_company("Siemens", status="ok", last_success=iso(REF), last_attempt=iso(REF))
        section = health_section(R.render(self.state, self.template, self.config))
        self.assertRegex(section, r'<div class="h-data"><dt>Last good data</dt><dd><time datetime="[^"]+">'
                                  r'[^<]+</time> <span class="age">0m ago</span></dd></div>')
        rules = dict((tuple(s), b) for s, b in css_rules(media_blocks(style_of(R.render(
            self.state, self.template, self.config)))[0]))
        self.assertEqual(rules[(".h-data .age:before",)],
                         'content:"· ";content:"· "/"";color:hsl(var(--steel-muted))')

    def test_placeholder_accounting(self):
        names = set(R.PLACEHOLDER.findall(self.template))
        self.assertTrue({"SOURCE_COUNT", "EMPTY_COUNT", "HEALTH_REF", "HEALTH_SUMMARY", "HEALTH_ROWS", "HEALTH_JS",
                         "FRESH_LIMIT", "CRON", "WORST_STATE", "CHECKED"} <= names)
        for gone in ("SOURCE_TILES", "SRC_LED", "PULSE_ROWS", "SOURCE_ROWS", "STATUS_PRE", "DAY_UPPER"):
            self.assertNotIn(gone, names, gone)
        self.assertEqual(self.template.count("{{CHECKED}}"), 1)          # search-panel LED line only
        self.assertIn('id="src-led" data-s="{{WORST_STATE}}"><span class="led" aria-hidden="true"></span>'
                      '{{CHECKED}}</p>', self.template)
        for gone in ("source_tiles_html", "src_led_html", "pulse_rows_html", "source_rows_html", "PULSE_LABELS",
                     "status_pre_html"):
            self.assertFalse(hasattr(R, gone), gone)
        # Fragments with ids or script data appear exactly once in the template and the renderer enforces it.
        self.assertLessEqual(R.ONCE_PLACEHOLDERS, names)
        for name in sorted(R.ONCE_PLACEHOLDERS):
            self.assertEqual(self.template.count("{{" + name + "}}"), 1, name)
            with self.subTest(f"twice {name}"), self.assertRaises(ValueError):
                R.render(self.state, self.template.replace("</footer>", "{{" + name + "}}</footer>"), self.config)
        doc = R.render(self.state, self.template, self.config)
        self.assertIn("{{CRON}}", self.template)
        self.assertIn("· cron 23 */6 * * * UTC ·", api_dialog(doc))
        self.assertEqual(doc.count(f"cron {R.SCHEDULE_CRON} UTC"), 1)
        self.assertIsNone(R.PLACEHOLDER.search(doc))
        for name in sorted(names):
            with self.subTest(name), self.assertRaises(ValueError):
                R.render(self.state, self.template.replace("{{" + name + "}}", ""), self.config)
        self.assertNotIn("Companies checked", doc)
        self.assertIn('<h2 class="vh" id="src-h">Source health: 6 automated sources</h2>', doc)
        self.assertIn(f'<span class="label-sys" id="empty-h">Informational · {len(self.state.empty_companies)} '
                      'companies</span>', doc)
        self.assertIn('<dl class="info-dl" id="empty-list"></dl>', doc)
        self.assertIn('<div><dt>${esc(c)}</dt><dd>${esc(n)}</dd></div>', doc)
        self.assertEqual(len(self.state.empty_companies), 7)

    def test_all_stale_scheduler_silence(self):
        last = REF - dt.timedelta(hours=20)
        for name in self.config.companies:
            self.set_company(name, status="stale", last_success=iso(last), last_attempt=iso(REF))
        doc = R.render(self.state, self.template, self.config)
        section = health_section(doc)
        self.assertEqual(re.findall(r'<li class="ch" data-h="([a-z]+)">', section), ["stale"] * 6)
        self.assertNotIn("Worst", section)
        self.assertIn('<span class="age">20h 0m ago</span>', section)
        # The terminal status block and the search-panel LED carry the same worst state.
        frags = dashboard_fragments(doc)
        self.assertIn("<dt>Freshness</dt><dd>worst " + R.badge_html("stale") + "</dd>", frags["summary"])
        self.assertIn('data-s="stale"', frags["led"])
        # A silent scheduler leaves an old page up: the browser re-ages it and flags the late update right
        # after the next-run time in the status block.
        self.assertTrue(frags["summary"].endswith('</time><span id="h-late"></span> · scheduled, not guaranteed'
                                                  '</dd></div>'))
        script = main_script(doc)
        self.assertIn('$("#src-led").dataset.s=worst;', script)
        for gone in ("#srcs", ".sr-n", ".sr-s", "src-led-text", "#pulse"):
            self.assertNotIn(gone, script, gone)
        h = json.loads(re.search(r",H=(\{.*?\});\n", doc).group(1))
        self.assertEqual(h["fresh_ms"], 6.5 * 3600 * 1000)
        self.assertEqual(h["grace_ms"], 30 * 60 * 1000)
        self.assertEqual(h["severity"], list(R.HEALTH_SEVERITY))
        for snippet in ('if(!(now>ref))return', 'd>H.fresh_ms&&(li.dataset.h==="live"||li.dataset.h==="degraded")',
                        'badge(li.querySelector(".st"),"stale")', 'now>Date.parse(nx.dateTime)+H.grace_ms',
                        'document.querySelector("#h-late").textContent=H.late',
                        'STALE[li.querySelector(".ch-name").textContent]=`${H.stale}',
                        'document.querySelectorAll(".kvs .st").forEach(el=>badge(el,ws))',
                        'const sa=$("#st-at");if(sa)$("#st-age").textContent=ago(now-Date.parse(sa.dateTime));',
                        'const nx=document.querySelector("#h-next");'
                        'if(nx&&now>Date.parse(nx.dateTime)+H.grace_ms)document.querySelector("#h-late")'
                        '.textContent=H.late'):
            self.assertIn(snippet, script)
        self.assertNotIn(".hs-w", script)
        # Re-aging runs before the search-panel LED and the cards read HS/STALE.
        self.assertLess(script.index("H.grace_ms"), script.index("const HS={}"))
        self.assertNotIn(".tile", doc)
        self.assertEqual(h["late"], " · update not yet published")
        self.assertEqual(h["stale"], "stale since ")
        self.assertNotIn("overdue", doc)
        self.assertEqual(R.stale_labels(self.state, self.config),
                         {c: "stale since Oct 31" for c in self.config.companies})

    def test_alert_shape_and_status_unchanged(self):
        self.assertEqual(R.ALERT_FIELDS, (
            "schema", "refresh_id", "checked_at", "as_of", "total_jobs", "company_count", "new_count",
            "new_jobs", "remaining_new_count", "removed_count", "closing_soon_count", "closing_soon",
            "notification_title", "notification_body", "ledger_url", "source_status"))
        self.all_states_state()
        outcome = {"schema": 1, "run_at": iso(REF), "mode": "offline",
                   "state_sha256": R.sha256_text(self.state.to_json()), "added": [], "removed_count": 0}
        a = R.build_alert(self.state, outcome, self.config, "https://example-owner.github.io/job-openings-ledger/")
        self.assertEqual(tuple(a), R.ALERT_FIELDS)
        self.assertEqual(a["source_status"], {"Siemens": "ok", "JPMorganChase": "stale", "GE HealthCare": "ok",
                                              "GE Vernova": "stale", "IQVIA": "static", "ZS": "stale"})

    def test_committed_index_health_matches_state(self):
        config, state, template = R.load_all(ROOT)
        doc = R.read_text(os.path.join(ROOT, R.INDEX_PATH))
        self.assertEqual(doc, R.render(state, template, config))
        section = health_section(doc)
        counts = R.published_counts(state, config)
        ref = R.health_reference(state)
        for h in R.source_health(state, config, ref):
            self.assertEqual(channels(section)[h.company][0], h.state, h.company)
            self.assertEqual(h.published, counts[h.company])
        self.assertIn(f'<span class="hs-r">{sum(counts.values())} records</span>', section)
        self.assertIn(f"<dt>Openings</dt><dd><b>{sum(counts.values())}</b> published · ", status_block(doc))

    def test_connector_labels_are_a_positive_allowlist(self):
        self.assertEqual(R.CONNECTOR_LABELS, {
            "siemens_avature_html": "Avature HTML", "jpm_oracle_ce": "Oracle Careers API",
            "gehc_workday_cxs": "Workday CXS API", "iqvia_workday_cxs": "Workday CXS API",
            "gevernova_paradox_api": "Paradox Jobs API", "zs_jibe_api": "Jibe Jobs API",
            "static": "Static snapshot"})
        section = health_section(R.render(self.state, self.template, self.config))
        expected = {"Siemens": "Avature HTML", "JPMorganChase": "Oracle Careers API", "GE HealthCare": "Workday CXS API",
                    "GE Vernova": "Paradox Jobs API", "IQVIA": "Workday CXS API", "ZS": "Jibe Jobs API"}
        for company, (_s, rest) in channels(section).items():
            self.assertTrue(rest.startswith(f'<span class="ch-c"><span class="vh">, source connector </span>'
                                            f'{expected[company]}</span></span>'), company)
        for mode in {self.config.sources[c]["mode"] for c in self.config.companies}:
            self.assertNotIn(mode, section, mode)                       # internal mode keys stay private
        for host in self.config.hosts:
            self.assertNotIn(host, section, host)
        # An unlisted mode fails the render rather than leaking its key.
        with mock.patch.dict(self.config.sources["ZS"], {"mode": "zs_future_api"}), self.assertRaises(ValueError):
            R.render(self.state, self.template, self.config)

    def test_latest_refresh_rollup_counts(self):
        self.all_states_state()
        sources = R.source_health(self.state, self.config, R.health_reference(self.state))
        self.assertEqual(R.attempt_rollup(sources), {"accepted": 2, "held": 2, "failed": 1, "pending": 1})
        doc = R.render(self.state, self.template, self.config)
        section, block = health_section(doc), status_block(doc)
        self.assertIn('<div><dt>Result</dt><dd><b data-o="accepted">2</b>/6 sources accepted · <span data-o="held">'
                      '<b>2</b> held</span> · <span data-o="failed"><b>1</b> failed</span> · <span data-o="pending">'
                      '<b>1</b> pending</span></dd></div>', block)
        self.assertEqual(doc.count("sources accepted"), 1)               # status block only
        for gone in ("h-rw", "hs-a", "h-roll", "Latest refresh", "worst freshness", "/6"):
            self.assertNotIn(gone, section, gone)
        self.assertNotIn("Worst", doc)
        self.assertEqual(block.count("worst "), 1)                       # Freshness row badge only
        self.assertEqual(sum(R.attempt_rollup(sources).values()), len(self.config.companies))
        self.assertEqual(set(R.ROLLUP_OF), set(R.ATTEMPT_TEXT))

    def test_status_block_openings_counts_first_seen_today(self):
        def expected_new(state):
            return sum(1 for r in state.jobs if r.first_seen == state.as_of.isoformat() and
                       max(0, (state.as_of - dt.date.fromisoformat(r.posted_date)).days) <= self.config.max_age_days)

        visible = sum(R.published_counts(self.state, self.config).values())
        n = expected_new(self.state)
        doc = R.render(self.state, self.template, self.config)
        self.assertIn(f"<div><dt>Openings</dt><dd><b>{visible}</b> published · <b>{n}</b> first seen today</dd></div>",
                      status_block(doc))
        self.assertEqual(visible, int(re.search(r'<dd id="total">(\d+)</dd>', doc).group(1)))
        # Bump one visible, not-yet-new row's first_seen to as_of: the count rises by one. The fixture may
        # mark every row new, so one visible row is first moved to yesterday to give a stable baseline.
        today = self.state.as_of.isoformat()
        i = next(i for i, r in enumerate(self.state.jobs)
                 if (self.state.as_of - dt.date.fromisoformat(r.posted_date)).days <= self.config.max_age_days)
        if self.state.jobs[i].first_seen == today:
            yesterday = (self.state.as_of - dt.timedelta(days=1)).isoformat()
            self.state.jobs[i] = dataclasses.replace(self.state.jobs[i], first_seen=yesterday)
            n = expected_new(self.state)
            self.assertIn(f"<dd><b>{visible}</b> published · <b>{n}</b> first seen today</dd>",
                          status_block(R.render(self.state, self.template, self.config)))
        self.state.jobs[i] = dataclasses.replace(self.state.jobs[i], first_seen=today)
        self.assertEqual(expected_new(self.state), n + 1)
        self.assertIn(f"<dd><b>{visible}</b> published · <b>{n + 1}</b> first seen today</dd>",
                      status_block(R.render(self.state, self.template, self.config)))
        # A row first seen today but past the age window is not published and not counted.
        old = (self.state.as_of - dt.timedelta(days=self.config.max_age_days + 5)).isoformat()
        self.state.jobs[i] = dataclasses.replace(self.state.jobs[i], posted_date=old)
        self.assertIn(f"<dd><b>{visible - 1}</b> published · <b>{n}</b> first seen today</dd>",
                      status_block(R.render(self.state, self.template, self.config)))

    def test_status_block_rows_and_placeholder(self):
        self.assertEqual(self.template.count("{{STATUS_BLOCK}}"), 1)
        self.assertIn('<dl class="kvs">{{STATUS_BLOCK}}</dl>', self.template)
        self.assertIn("STATUS_BLOCK", R.ONCE_PLACEHOLDERS)
        block = R.status_block_html(self.state, self.config)
        self.assertEqual(re.findall(r"<div><dt>([^<]+)</dt><dd>", block),
                         ["Last refresh", "Result", "Openings", "Freshness", "Next run"])
        self.assertEqual(block.count("<div>"), 5)
        self.assertEqual(status_block(R.render(self.state, self.template, self.config)), block)

    def test_purple_theme_has_no_yellow_tokens(self):
        doc = R.render(self.state, self.template, self.config)
        style = style_of(doc)
        self.assertNotRegex(style.lower(), r"#[0-9a-f]{3,8}\b|rgb\(|oklch\(|--type-")
        self.assertNotRegex(doc.lower(), r"yellow|gold|amber|lime")
        dark, light = theme_tokens(style)
        literals = re.findall(r"hsl\((\d+ \d+% \d+%)", style)           # inline fallbacks, e.g. the backdrop
        for value in [*dark.values(), *light.values(), *literals]:
            h, s, _l = (float(x) for x in value.replace("%", "").split())
            if s >= 10:                                                 # chromatic values only
                self.assertFalse(40 <= h <= 70, f"{value} reads as yellow/gold")
        for t in (dark, light):
            h, s, _l = (float(x) for x in t["signal"].replace("%", "").split())
            self.assertTrue(260 <= h <= 280 and s >= 80, t["signal"])                     # vivid purple
            for n in ("void", "plate", "plate-2", "plate-3", "hull", "hull-2", "steel", "steel-muted"):
                self.assertTrue(255 <= float(t[n].split()[0]) <= 270, n)                 # violet neutrals
            self.assertTrue(300 <= float(t["warn"].split()[0]) <= 340)                   # pink warning
        self.assertEqual({k: dark[k] for k in ("band-n", "band-1", "band-2")},
                         {"band-n": "262 45% 76%", "band-1": "150 42% 68%", "band-2": "188 45% 70%"})
        self.assertGreater(style.count("var(--signal)"), 30)
        self.assertIn('html[data-theme=light]{color-scheme:light;', style)

    def test_header_has_only_the_requests_button(self):
        self.all_states_state()
        doc = R.render(self.state, self.template, self.config)
        self.assertIn('<div class="wm-row"><span class="wm">job signal</span><span class="mark" aria-hidden="true">'
                      '</span><span class="label-sys-bright live"><span class="led led-pulse" aria-hidden="true">'
                      '</span>live · automated</span><button class="th-m" id="theme-m" type="button" aria-label="Switch '
                      'to light mode" hidden>[ theme ]</button><button class="th-m" id="req-btn-m" type="button" '
                      'aria-haspopup="dialog" aria-controls="api" hidden>[ requests ]</button></div>', doc)
        # Two short lines on desktop; both are visually hidden (still read out) on phones.
        self.assertIn('<h1><span class="h1a">New openings every six hours</span><span class="h1b"> · 0–2 years · '
                      'Bengaluru · Kochi</span></h1>\n<p class="lede">First-party employer portals · deduplicated · '
                      'last 30 days</p>', doc)
        self.assertNotIn("A public ledger of fresh roles", doc)
        header = re.search(r'<header class="hdr">.*?</header>', doc, re.S).group(0)
        right = header[header.index('<div class="hdr-r">'):]
        self.assertEqual(right, '<div class="hdr-r"><button class="chamfer-btn req" id="req-btn" type="button" '
                                'aria-haspopup="dialog" aria-controls="api" hidden><span class="gl" aria-hidden="true">'
                                '&gt;_</span> Requests</button></div>\n</header>')
        for gone in ('class="panel', "status", "<pre", 'id="st-', "Details", "Status key", "signal status"):
            self.assertNotIn(gone, header, gone)
        # No `$ signal …` command text, status block or status key outside the dialog.
        outside = page_outside_dialog(doc)
        text = re.sub(r"<[^>]+>", "", outside)
        for gone in ("$ signal", "signal status", "signal clock", "signal window", "Refresh status", "Status key",
                     "Freshness key", "source pulse", "Source details", "Source status"):
            self.assertNotIn(gone, text, gone)
        for gone in ('id="st-pre"', 'id="h-next"', 'id="h-late"', 'id="health"', 'class="h-key"', 'class="ps"'):
            self.assertNotIn(gone, outside, gone)
        base, blocks = media_blocks(style_of(doc))
        rules = dict((tuple(s), b) for s, b in css_rules(base))
        self.assertIn("max-width:44rem", rules[(".hdr-l",)])
        self.assertIn("align-self:flex-start", rules[(".hdr-r",)])
        self.assertEqual(rules[(".req",)], "gap:8px;height:32px")
        self.assertIn("font:600 15px/1.35 var(--font-sans)", rules[("h1",)])          # no display-size hero
        self.assertNotIn("clamp(", rules[("h1",)])
        self.assertIn(".th-m:not([hidden]){display:inline-flex;", blocks[SHEET_MQ])
        self.assertIn("h1,.lede{position:absolute;width:1px;height:1px;margin:0;overflow:hidden;clip-path:inset(50%);"
                      "white-space:nowrap}", blocks[SHEET_MQ])
        self.assertRegex(status_block(doc), r'<div><dt>Next run</dt><dd><time id="h-next" datetime="[^"]+">'
                                            r'31 Oct 23:53 IST</time><span id="h-late"></span> · scheduled, not '
                                            r'guaranteed</dd></div>$')
        # The search panel shows only the checked time; its LED takes the worst freshness state.
        self.assertEqual(dashboard_fragments(doc)["led"],
                         '<p class="label-sys src-led" id="src-led" data-s="failed"><span class="led" aria-hidden="true">'
                         '</span>Checked 31 Oct 22:47 IST</p>')
        self.assertIn(".src-led .led{color:hsl(var(--sc))}", base)
        self.assertIn("[data-s=failed]{--sc:var(--bad)}", base)
        self.state.checked_at = None
        self.assertIn('<span class="led" aria-hidden="true"></span>Auto-check pending</p>',
                      dashboard_fragments(R.render(self.state, self.template, self.config))["led"])
        self.assertNotIn((".ps",), rules)
        self.assertNotIn((".pre",), rules)
        # In-page anchors stay in this tab despite <base target="_blank">.
        for href in re.findall(r'<a [^>]*href="#[^"]*"[^>]*>', doc):
            self.assertIn('target="_self"', href)

    def test_terminal_never_shows_detail_or_urls(self):
        hosts = sorted(self.config.hosts)
        for i, name in enumerate(self.config.companies):
            self.set_company(name, status="stale", last_success=iso(REF - dt.timedelta(hours=i)), last_attempt=iso(REF),
                             detail=f"zzterm{i} https://{hosts[i % len(hosts)]}/wday/cxs KeyError HTTP 503",
                             listing_total=765432 + i, screened={"999981": "2026-10-30"},
                             removal_hold={"at": "2026-10-30T09:11+05:30", "ids": ["999982"]})
        doc = R.render(self.state, self.template, self.config)
        dialog = api_dialog(doc)
        for needle in ("zzterm", "https://", "http", "/wday", "KeyError", "503", "765432", "999981", "999982",
                       "09:11", "removal", "screened", "listing", *hosts,
                       *{self.config.sources[c]["mode"] for c in self.config.companies}):
            self.assertNotIn(needle, dialog, needle)
        self.assertNotIn("zzterm", doc)
        self.assertEqual(list(channels(dialog)), list(self.config.companies))
        with mock.patch.dict(self.config.sources["ZS"], {"mode": "zs_future_api"}), self.assertRaises(ValueError):
            R.render(self.state, self.template, self.config)

    def test_motion_is_short_finite_and_reduced(self):
        doc = R.render(self.state, self.template, self.config)
        style = style_of(doc)
        effects, reduced = style.split("@media(prefers-reduced-motion:reduce)")
        for kill in ("animation:none!important", "transition:none!important", "dialog.term::backdrop{animation:none!important}"):
            self.assertIn(kill, reduced)
        secs = lambda t: float(t[:-2]) / 1000 if t.endswith("ms") else float(t[:-1])   # noqa: E731
        durations = dict(re.findall(r"--(dur-[a-z]+):([\d.]+m?s)", style))
        self.assertEqual(durations, {"dur-press": "90ms", "dur-fast": ".15s", "dur-base": ".2s", "dur-slow": ".32s"})
        # The LED pulse is the only infinite animation; everything else is one short run (the caret blinks
        # six times and stops).
        anims = re.findall(r"animation:([^;}]+)", effects)
        self.assertEqual([a for a in anims if "infinite" in a], ["led-pulse 2s ease-in-out infinite"])
        self.assertEqual(style.count("infinite"), 1)
        self.assertIn("blink .5s steps(1) 6", anims)
        self.assertIn("pxin .24s steps(3) backwards", anims)
        self.assertIn("@keyframes blink{50%{opacity:0}}", style)
        for decl in anims:
            if "infinite" in decl:
                continue
            times = re.findall(r"(?<![\w.])\d*\.?\d+m?s\b", decl)
            self.assertTrue(times, decl)
            self.assertLess(max(secs(t) for t in times), 0.6, decl)
        for name in ("boot-in", "led-pulse", "dialog-pop-in", "overlay-fade-in", "sheet-up", "pxin", "blink"):
            self.assertIn(f"@keyframes {name}{{", style)
        # The reveal stagger is 40ms per card, applied to the first 12 cards on the first render only; the
        # terminal's four output blocks stagger by 90ms and all finish within .7s.
        self.assertEqual(re.findall(r"animation-delay:([^;}]+)", effects), ["calc(var(--i)*40ms)",
                                                                            "calc(var(--d)*90ms)"])
        steps = [int(d) for d in re.findall(r'<div class="tb" style="--d:(\d+)">', doc)]
        self.assertEqual(steps, [0, 1, 2, 3, 4])
        self.assertLess(max(steps) * 0.09 + 0.24, 0.7)
        self.assertIn("anim=!booted&&i<12", doc)
        self.assertIn('<main id="main" class="container boot-in">', doc)
        for decl in re.findall(r"transition:([^;}]+)", effects):
            for part in decl.split(","):
                part = re.sub(r"var\(--(dur-[a-z]+)\)", lambda m: durations[m.group(1)], part)
                for t in re.findall(r"(?<![\w.])\d*\.?\d+m?s\b", part):
                    self.assertLessEqual(secs(t), 0.32, decl)
        # Exactly two pulsing LEDs (header and feed toolbar), both in static markup.
        markup = doc.replace(style, "")
        self.assertEqual(markup.count("led-pulse"), 2)
        self.assertEqual(markup.count('<span class="led led-pulse" aria-hidden="true"></span>'), 2)
        self.assertNotIn("led-pulse", main_script(doc))
        self.assertNotIn("offsetWidth", doc)
        self.assertNotIn("getBoundingClientRect", doc)
        self.assertIn('behavior:RM.matches?"auto":"smooth"', doc)

    def test_stats_markup_and_target_sizes(self):
        doc = R.render(self.state, self.template, self.config)
        self.assertIn('<div class="stat"><dt><span class="vh">Posted </span>Today</dt><dd id="today">', doc)
        self.assertEqual(re.findall(r"<dl class=\"stats\">(.*?)</dl>", doc)[0].count("<dt>"), 3)
        style = style_of(doc)
        base, blocks = media_blocks(style)
        rules = dict((tuple(s), b) for s, b in css_rules(base))
        px = lambda body, prop: int(re.search(rf"(?:^|;){prop}:(\d+)px", body).group(1))   # noqa: E731
        for sel, prop, least in (((".ob", ".ci", ".chip"), "min-height", 28), ((".chamfer-btn",), "min-height", 32),
                                 (("#query",), "height", 24), ((".field select",), "height", 28)):
            self.assertGreaterEqual(px(rules[sel], prop), least, (sel, prop))
        # The 12px red close light has a 24px hit area: a pseudo-element extends it by 6px on every side.
        self.assertIn("width:12px;height:12px", rules[(".dot",)])
        self.assertEqual(rules[(".dot-x:after",)], 'content:"";position:absolute;inset:-6px')
        self.assertIn("position:relative", rules[(".dot",)])
        self.assertEqual(px(rules[(".req",)], "height"), 32)
        wide = dict((tuple(s), b) for s, b in css_rules(blocks["(min-width:768px)"]))
        self.assertIn("min-width:44px;height:44px", wide[(".thm:not([hidden])",)])
        sheet = dict((tuple(s), b) for s, b in css_rules(blocks[SHEET_MQ]))
        self.assertGreaterEqual(px(sheet[(".th-m:not([hidden])",)], "min-height"), 28)
        for gone in (".lk", ".go", ".cta"):
            self.assertNotIn(gone, style, gone)
        # On phones the card title link covers the whole card.
        self.assertIn('.job:after{content:"";position:absolute;inset:0}', blocks[PHONE_MQ])
        self.assertTrue(rules[(".card",)].startswith("position:relative;"))
        # Absolutely positioned screen-reader text inside scrolling chips stays inside its chip.
        self.assertIn(".ci,.chip{position:relative}", base)

    def test_dashboard_fragments_are_allowlisted(self):
        hosts = sorted(self.config.hosts)
        for i, name in enumerate(self.config.companies):
            self.set_company(name, status="stale", last_success=iso(REF - dt.timedelta(hours=i)), last_attempt=iso(REF),
                             detail=f"zzdash{i} https://{hosts[i % len(hosts)]}/wday/cxs KeyError HTTP 503",
                             listing_total=765432 + i, screened={"999981": "2026-10-30"},
                             stale_since="2026-10-29T03:17+05:30",
                             removal_hold={"at": "2026-10-30T09:11+05:30", "ids": ["999982"]})
        doc = R.render(self.state, self.template, self.config)
        frags = dashboard_fragments(doc)
        modes = {self.config.sources[c]["mode"] for c in self.config.companies}
        for key, frag in frags.items():
            for needle in ("zzdash", "https://", "http", "/wday", "KeyError", "503", "765432", "999981", "999982",
                           "09:11", "03:17", "removal", "screened", "listing", "stale_since", "excluded", *hosts, *modes):
                self.assertNotIn(needle, frag, f"{key}: {needle}")
        sources = R.source_health(self.state, self.config, R.health_reference(self.state))
        worst = R.worst_state(h.state for h in sources)
        self.assertEqual(frags["led"], f'<p class="label-sys src-led" id="src-led" data-s="{worst}"><span class="led" '
                                       'aria-hidden="true"></span>Checked 31 Oct 22:47 IST</p>')
        # Outside the dialog, no connector names, per-source states or refresh counts are shown at all.
        outside = page_outside_dialog(doc)
        for needle in ("Avature", "Oracle Careers", "Workday", "Paradox", "Jibe", "Static snapshot", "accepted",
                       "held", "connector", 'data-h="', "st st-", *modes, *hosts):
            self.assertNotIn(needle, outside, needle)
        # The seeded state renders the same LED shape with no check yet.
        seeded = dashboard_fragments(R.render(env()[2], self.template, self.config))["led"]
        self.assertRegex(seeded, r'^<p class="label-sys src-led" id="src-led" data-s="(live|degraded|stale|failed|'
                                 r'unknown)"><span class="led" aria-hidden="true"></span>Auto-check pending</p>$')
        with mock.patch.dict(self.config.sources["ZS"], {"mode": "zs_future_api"}), self.assertRaises(ValueError):
            R.render(self.state, self.template, self.config)

    def test_feed_cards_sorting_and_company_chips(self):
        doc = R.render(self.state, self.template, self.config)
        script = main_script(doc)
        # One flat stream of cards; only the first 12 animate, and only on the first render.
        for snippet in ('const card=(r,i)=>{const[c,id,t,e,d,a,l,u,b,cd]=r,', 'anim=!booted&&i<12',
                        '<article class="card${anim?" reveal":""}"${anim?` style="--i:${i}"`:""}>',
                        '<span class="av" aria-hidden="true">${esc(ini(c))}</span>',
                        '<h3 class="c-l"><span class="c-co">${esc(c)}</span> posted <a class="job" href="${esc(u)}" '
                        'rel="noopener noreferrer">${esc(t)}</a></h3>',
                        'ini=c=>{const w=c.split(/\\s+/).filter(Boolean);return(w.length>1?w[0][0]+w[1][0]:c.slice(0,2))'
                        '.toUpperCase()}',
                        'const BAND={n:"No number",1:"≤ 1 year",2:"≤ 2 years"}',
                        '${ns?"":`<p class="c-x"><span class="vh">Minimum experience stated: </span>${esc(e)}</p>`}',
                        '<span class="vh">Job ID </span>${esc(id)}</span>${cd?',
                        '</p>`}</div></div></article>`};',
                        'out.innerHTML=visibleJobs.map(card).join("")||\'<p class="none">No openings match these '
                        'filters.</p>\''):
            self.assertIn(snippet, script, snippet)
        for gone in ('class="sec"', "groups.set(", "<ol class=\"rows\">", 'class="c-f"', 'class="tags"', 'class="tg',
                     'class="go"', "open ↗", "slug", "lb=STALE"):
            self.assertNotIn(gone, script, gone)
        # The title is the card's only link.
        self.assertEqual(script[script.index("const card="):script.index("function render()")].count("<a "), 1)
        # Sorting: newest = age then configured company order; closing soon = known deadlines first.
        self.assertIn("byNew=(x,y)=>x[5]-y[5]||oi(x[0])-oi(y[0]),byClose=(x,y)=>{const a=daysToClose(x[9]),"
                      "b=daysToClose(y[9]);return a===b?byNew(x,y):a===null?1:b===null?-1:a-b}", script)
        self.assertIn('jobs.filter(match).sort(sortBy==="close"?byClose:byNew)', script)
        self.assertIn('<div class="tgl" role="group" aria-label="Sort openings"><button class="ob" id="sort-new" '
                      'type="button" aria-pressed="true">Newest</button><button class="ob" id="sort-close" type="button" '
                      'aria-pressed="false">Closing soon</button></div>', doc)
        self.assertIn('$("#sort-l").textContent=sortBy==="close"?"· closing soon":"· newest first"', script)
        # Company chips: "All companies" plus one per configured company, kept in sync with #company, which is a
        # non-visible state holder placed just before the phone Filters toggle and the three visible filters.
        self.assertIn('<select id="company" class="vh" aria-hidden="true" tabindex="-1"><option value="">All '
                      'companies</option></select>\n<button class="ob ftog" id="ftog" type="button" aria-controls="fsel" '
                      'aria-expanded="false" hidden><span>Filters (<span id="fn">0</span>)</span><span class="ftog-c" '
                      'aria-hidden="true">▾</span></button>\n<div class="fsel" id="fsel"><div class="field">'
                      '<label for="age">', doc)
        self.assertNotIn('<label for="company">', doc)
        self.assertEqual(re.search(r'<div class="fsel" id="fsel">.*?</div>\n', doc).group(0).count('class="field"'), 3)
        self.assertIn('ci.innerHTML=`<li><button class="ci" type="button" data-co="" aria-pressed="true">All companies '
                      '<b>0</b></button></li>`+order.map(c=>`<li><button class="ci" type="button" data-co="${esc(c)}" '
                      'aria-pressed="false">', script)
        self.assertIn('cf.value=b.dataset.co&&cf.value!==b.dataset.co?b.dataset.co:"";render()', script)
        self.assertIn('b.setAttribute("aria-pressed",String(co===cv))', script)
        base = media_blocks(style_of(doc))[0]
        self.assertIn(".ob[aria-pressed=true],.ci[aria-pressed=true]{border-color:hsl(var(--signal)/.7);"
                      "background:hsl(var(--signal)/.08);color:hsl(var(--signal))}", base)
        self.assertIn('<p class="note">Discovery list, not a fit ranking.', doc)

    def test_theme_toggle_defaults_dark(self):
        doc = R.render(self.state, self.template, self.config)
        self.assertIn('<html lang="en">', doc)
        self.assertIn('<meta name="color-scheme" content="dark">', doc)
        self.assertIn(':root{color-scheme:dark;', doc)
        head = doc[:doc.index("<style>")]
        self.assertIn('<script>try{if(localStorage.getItem("theme")==="light"){document.documentElement.dataset.theme='
                      '"light";document.querySelector(\'meta[name="color-scheme"]\').content="light"}}catch(e){}</script>',
                      head)
        script = main_script(doc)
        self.assertIn('root.dataset.theme=t;csm.content=t;try{localStorage.setItem("theme",t)}catch(e){}', script)
        self.assertIn('<button class="thm" id="theme" type="button" hidden><span class="thm-g" aria-hidden="true">◐'
                      '</span><span class="thm-l">Light mode</span></button>', doc)
        self.assertIn('<button class="th-m" id="theme-m" type="button" aria-label="Switch to light mode" hidden>'
                      '[ theme ]</button>', doc)
        style = style_of(doc)
        base, blocks = media_blocks(style)
        self.assertIn(".thm,.th-m{display:none}", base)
        self.assertIn(".thm-l{max-width:0;overflow:hidden;", base)
        self.assertIn(".thm:not([hidden]){position:fixed;right:16px;bottom:16px;", blocks["(min-width:768px)"])
        self.assertIn(".thm:hover .thm-l,.thm:focus-visible .thm-l{max-width:12rem;padding-left:10px}",
                      blocks["(min-width:768px)"])
        self.assertIn(".th-m:not([hidden]){display:inline-flex;", blocks[SHEET_MQ])

    def test_right_aside_holds_informational_only(self):
        doc = R.render(self.state, self.template, self.config)
        self.assertEqual(doc.count("<aside"), 1)
        self.assertNotIn('class="al"', doc)
        aside = re.search(r'<aside class="ar".*?</aside>', doc, re.S).group(0)
        self.assertTrue(aside.startswith('<aside class="ar" aria-label="Informational companies"><div class="ar-in">\n'
                                         '<section class="side" id="empty" aria-labelledby="empty-h">'))
        self.assertEqual(re.findall(r'<section class="side" id="([a-z]+)"', aside), ["empty"])
        self.assertEqual(aside.count("<section"), 1)
        self.assertIn(f'Informational · {len(self.state.empty_companies)} companies', aside)
        for gone in ("led-pulse", "h-key", "pulse", "srcs", "Status key", "Source details", "<button"):
            self.assertNotIn(gone, aside, gone)
        # Quick views and the left aside's Reset filters are gone; the sort toggle and the search-panel
        # Reset remain the only controls for those actions.
        script = main_script(doc)
        for snippet in ('setSort=s=>{sortBy=s;sN.setAttribute("aria-pressed",String(sortBy==="new"));',
                        '[sN,sC].forEach(b=>b.addEventListener("click",()=>setSort(b===sC?"close":"new")))',
                        '$("#reset").addEventListener("click",reset);render();'):
            self.assertIn(snippet, script, snippet)
        for gone in ('id="qv"', "Quick views", "data-f=", "qv-req", 'id="reset2"', "Reset filters", 'class="cta"'):
            self.assertNotIn(gone, doc, gone)
        for gone in ('$("#qv")', "dataset.f", "#reset2"):
            self.assertNotIn(gone, script, gone)
        self.assertEqual(doc.count('id="reset"'), 1)


if __name__ == "__main__":
    unittest.main()
