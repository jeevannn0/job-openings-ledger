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
    config = R.load_config(R.read_json(os.path.join(ROOT, R.CONFIG_PATH)))
    template = R.read_text(os.path.join(ROOT, R.TEMPLATE_PATH))
    return config, R.load_state(R.read_json(FROZEN_STATE), config), template


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
                    "https://evil.example/x", "ftp://jobs.siemens.com/x", "https://jobs.zs.com/jobs/1",
                    "https://jobs.siemens.com/a b"):
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
                                              "iqvia.wd1.myworkdayjobs.com"})

    def test_config_rejects_unsafe_changes(self):
        mutations = {
            "extra host": lambda d: d["hosts"].__setitem__("evil.example", {"fetch": True, "robots": False,
                                                                            "min_delay_s": 1.0}),
            "static fetch": lambda d: d["hosts"]["jobs.zs.com"].__setitem__("fetch", True),
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
        self.assertEqual(added, {"528001", "R1580001", "R5060001"})
        new_iq = next(r for r in after.jobs if r.job_id == "R1580001")
        self.assertEqual(new_iq.closing_date, "2026-11-15")      # Workday endDate 2026-11-16 is exclusive
        self.assertEqual(next(r for r in after.jobs if r.job_id == "R5060001").closing_date, "2026-11-20")
        ids = {r.job_id for r in after.jobs}
        self.assertNotIn("500001", ids)                          # excluded ID seen in the Siemens listing
        self.assertNotIn("R1590001", ids)                        # excluded ID seen in the IQVIA listing
        self.assertIn("520000", after.companies["Siemens"].screened)
        self.assertEqual({c: s.status for c, s in after.companies.items()},
                         {"Siemens": "ok", "JPMorganChase": "static", "GE HealthCare": "static",
                          "GE Vernova": "ok", "IQVIA": "ok", "ZS": "static"})
        self.assertEqual(R.read_text(os.path.join(tmp, "index.html")), R.render(after, template, config))
        with open(out_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "changed=true\nas_of=2026-10-31\n")
        self.assertIn("| Siemens | ok |", out)


if __name__ == "__main__":
    unittest.main()
