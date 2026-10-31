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
            re.search(r"<style>.*?</style>", legacy, re.S).group(0),
        ]
        for s in snippets:
            self.assertIn(s, doc)

    def test_checked_and_stale_metadata(self):
        config, _e, state, template = env()
        doc = R.render(state, template, config)
        self.assertIn('<span class="il" id="checked">Auto-check pending</span>', doc)
        state.checked_at = "2026-10-31T15:34+05:30"
        state.companies["IQVIA"] = dataclasses.replace(state.companies["IQVIA"], status="stale",
                                                       last_success="2026-10-30T09:23+05:30")
        doc = R.render(state, template, config)
        self.assertIn('id="checked">Checked 31 Oct 15:34 IST</span>', doc)
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
            del c["removal_hold"]
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
    def test_meaningful_ignores_volatile_timestamps(self):
        config, _e, state, _t = env()
        later = dataclasses.replace(state, checked_at="2026-10-31T21:23+05:30",
                                    companies={c: dataclasses.replace(s, last_attempt="2026-10-31T21:23+05:30")
                                               for c, s in state.companies.items()})
        self.assertFalse(R.meaningful(state, later))
        changed = dataclasses.replace(later, jobs=later.jobs[1:])
        self.assertTrue(R.meaningful(state, changed))
        disc = dataclasses.replace(later, companies={**later.companies, "Siemens": dataclasses.replace(
            later.companies["Siemens"], last_discovery_attempt="2026-10-31T21:23+05:30")})
        self.assertTrue(R.meaningful(state, disc))


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


if __name__ == "__main__":
    unittest.main()
