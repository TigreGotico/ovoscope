"""ovoscope pytest plugin — provides the ``minicroft`` fixture and bus-coverage hooks.

Registered automatically via the ``pytest11`` entry point in ``pyproject.toml``.
Import directly in a ``conftest.py`` if you need to customise the fixture scope::

    # conftest.py
    from ovoscope.pytest_plugin import minicroft  # noqa: F401

Usage in tests::

    class TestMySkill:
        skill_ids = ["my-skill.author"]

        def test_something(self, minicroft):
            from ovoscope import End2EndTest
            from ovos_bus_client.message import Message
            from ovos_bus_client.session import Session

            session = Session("test-1")
            message = Message(
                "recognizer_loop:utterance",
                {"utterances": ["hello"], "lang": "en-US"},
                {"session": session.serialize(), "source": "A", "destination": "B"},
            )
            test = End2EndTest(
                minicroft=minicroft,
                skill_ids=self.skill_ids,
                source_message=message,
                expected_messages=[...],
            )
            test.execute(timeout=10)

Bus coverage opt-in::

    class TestMySkill:
        skill_ids = ["my-skill.author"]

        def test_something(self, minicroft, bus_coverage_session):
            test = End2EndTest(
                minicroft=minicroft,
                skill_ids=self.skill_ids,
                source_message=message,
                expected_messages=[...],
                track_bus_coverage=True,
            )
            test.execute()
            bus_coverage_session.add(test.bus_coverage_report)
"""

from typing import TYPE_CHECKING, Iterator, List, Optional, Union

if TYPE_CHECKING:
    from ovoscope.bus_coverage import BusCoverageReport

from pathlib import Path

import pytest

from ovoscope import MiniCroft, get_minicroft, End2EndTest

# Global collector for autouse fixture and monkey-patched End2EndTest
_SESSION_COLLECTOR: Optional["BusCoverageCollector"] = None


def pytest_addoption(parser):
    """Add CLI options for ovoscope bus coverage."""
    group = parser.getgroup("ovoscope")
    group.addoption(
        "--ovoscope-bus-cov",
        action="store_true",
        default=False,
        help="Enable bus-level coverage tracking for all End2EndTests.",
    )
    group.addoption(
        "--ovoscope-bus-cov-file",
        action="store",
        default=None,
        metavar="PATH",
        help="Save the merged bus coverage report to a JSON file.",
    )
    group.addoption(
        "--ovoscope-bus-cov-verbose",
        action="store_true",
        default=False,
        help="Show detailed list of covered/uncovered message types in the terminal.",
    )
    group.addoption(
        "--ovoscope-bus-cov-include",
        action="store",
        default=None,
        metavar="PATTERN",
        help="Only include skills/components matching this regex in the coverage report.",
    )
    group.addoption(
        "--ovoscope-bus-cov-exclude",
        action="store",
        default=None,
        metavar="PATTERN",
        help="Exclude skills/components matching this regex from the coverage report.",
    )
    group.addoption(
        "--ovoscope-accuracy-report",
        action="store",
        default=None,
        metavar="PATH",
        help="Write per-(pipeline, lang, intent) accuracy report as JSON.",
    )
    group.addoption(
        "--ovoscope-accuracy-min",
        action="store",
        type=float,
        default=None,
        metavar="RATIO",
        help=("Minimum overall intent-case pass rate (0.0-1.0). If set and "
              "the rate falls below, the session exits non-zero — useful "
              "as a CI gate on regression in intent routing accuracy."),
    )
    group.addoption(
        "--ovoscope-accuracy-baseline",
        action="store",
        default=None,
        metavar="PATH",
        help=("Path to a previous --ovoscope-accuracy-report JSON. The "
              "session fails if overall accuracy is lower than the "
              "baseline (helpful for blocking PRs that lower accuracy)."),
    )
    group.addoption(
        "--ovoscope-accuracy-tolerant",
        action="store_true",
        default=False,
        help=("Downgrade individual intent-case failures to xfail so they "
              "don't fail the build; only the aggregate accuracy gate "
              "(--ovoscope-accuracy-min / --ovoscope-accuracy-baseline) "
              "can block the session."),
    )
    group.addoption(
        "--ovoscope-accuracy-md",
        action="store",
        default=None,
        metavar="PATH",
        help=("Write a Markdown intent-case accuracy report (the format "
              "consumed by the OpenVoiceOS gh-automations PR-comment "
              "workflow). Pairs naturally with --ovoscope-accuracy-report."),
    )
    group.addoption(
        "--ovoscope-accuracy-top-n",
        action="store",
        type=int,
        default=10,
        metavar="N",
        help=("Show the N hardest utterances (lowest cross-pipeline pass "
              "rate) in the Markdown report. Default: 10."),
    )


@pytest.fixture(scope="class")
def minicroft(request) -> Iterator[MiniCroft]:
    """Class-scoped pytest fixture that provides a ready ``MiniCroft`` instance.

    Reads ``skill_ids`` from the test class attribute ``skill_ids: List[str]``.
    The MiniCroft is started once for the entire test class and stopped in teardown.

    Example::

        class TestMySkill:
            skill_ids = ["my-skill.author"]

            def test_intent(self, minicroft):
                ...  # minicroft is already started and READY
    """
    skill_ids: Union[List[str], str] = getattr(request.cls, "skill_ids", [])
    if isinstance(skill_ids, str):
        skill_ids = [skill_ids]
    mc: MiniCroft = None
    mc = get_minicroft(skill_ids)
    try:
        yield mc
    finally:
        if mc is not None:
            mc.stop()


class BusCoverageCollector:
    """Accumulates :class:`~ovoscope.bus_coverage.BusCoverageReport` objects
    across a pytest session and merges them for the terminal summary.

    Usage::

        # In a test
        def test_something(self, minicroft, bus_coverage_session):
            test = End2EndTest(..., track_bus_coverage=True)
            test.execute()
            bus_coverage_session.add(test.bus_coverage_report)
    """

    def __init__(self) -> None:
        self._reports: List["BusCoverageReport"] = []

    def add(self, report: Optional["BusCoverageReport"]) -> None:
        """Add a :class:`~ovoscope.bus_coverage.BusCoverageReport` to the collector.

        Silently ignores ``None`` so callers do not need to guard against tests
        where ``track_bus_coverage=False``.

        Args:
            report: A :class:`~ovoscope.bus_coverage.BusCoverageReport` or ``None``.
        """
        if report is not None:
            self._reports.append(report)

    def merged_report(self) -> Optional[object]:
        """Return a merged :class:`~ovoscope.bus_coverage.BusCoverageReport`.

        Merges all accumulated reports by summing per-skill listener invocations,
        observed counts, and asserted counts.

        Returns:
            A merged report, or ``None`` if no reports have been added.
        """
        if not self._reports:
            return None
        try:
            from ovoscope.bus_coverage import (
                BusCoverageReport,
                SkillBusCoverage,
                HandlerEntry,
                EmitterEntry,
            )
        except ImportError:
            return None

        # Merge by skill_id
        listener_data: dict = {}  # skill_id -> {msg_type -> (handler_count, invocation_count)}
        observed_data: dict = {}  # skill_id -> {msg_type -> count}
        asserted_data: dict = {}  # skill_id -> {msg_type -> count}

        for report in self._reports:
            for skill in report.skills:
                sid = skill.skill_id
                if sid not in listener_data:
                    listener_data[sid] = {}
                for h in skill.listeners:
                    existing = listener_data[sid].get(h.msg_type, (h.handler_count, 0))
                    listener_data[sid][h.msg_type] = (
                        existing[0],
                        existing[1] + h.invocation_count,
                    )
                if sid not in observed_data:
                    observed_data[sid] = {}
                for e in skill.emitters:
                    observed_data[sid][e.msg_type] = (
                        observed_data[sid].get(e.msg_type, 0) + e.observed_count
                    )
                if sid not in asserted_data:
                    asserted_data[sid] = {}
                for e in skill.emitters:
                    asserted_data[sid][e.msg_type] = (
                        asserted_data[sid].get(e.msg_type, 0) + e.asserted_count
                    )

        skills = []
        for skill_id in sorted(set(listener_data) | set(observed_data)):
            listeners = [
                HandlerEntry(
                    msg_type=mt,
                    handler_count=hc,
                    invocation_count=ic,
                    covered=ic > 0,
                )
                for mt, (hc, ic) in sorted(listener_data.get(skill_id, {}).items())
            ]
            all_emitted = set(observed_data.get(skill_id, {}).keys()) | set(
                asserted_data.get(skill_id, {}).keys()
            )
            emitters = [
                EmitterEntry(
                    msg_type=mt,
                    observed_count=observed_data.get(skill_id, {}).get(mt, 0),
                    asserted_count=asserted_data.get(skill_id, {}).get(mt, 0),
                    observed=observed_data.get(skill_id, {}).get(mt, 0) > 0,
                    asserted=asserted_data.get(skill_id, {}).get(mt, 0) > 0,
                )
                for mt in sorted(all_emitted)
            ]
            skills.append(SkillBusCoverage(skill_id=skill_id, listeners=listeners, emitters=emitters))

        return BusCoverageReport(skills=skills)


@pytest.fixture(scope="session", autouse=True)
def bus_coverage_session(request) -> Iterator[BusCoverageCollector]:
    """Session-scoped fixture that collects bus coverage reports from all tests.

    Automatically enabled if ``--ovoscope-bus-cov`` is passed to pytest.
    When enabled, it monkey-patches ``End2EndTest`` to
    automatically add reports to the session collector after ``execute()``.

    Tests can also opt in manually without the CLI flag by requesting this
    fixture and calling ``bus_coverage_session.add(test.bus_coverage_report)``.
    """
    import ovoscope
    global _SESSION_COLLECTOR
    enabled = request.config.getoption("--ovoscope-bus-cov")
    cov_file = request.config.getoption("--ovoscope-bus-cov-file")

    collector = BusCoverageCollector()
    _SESSION_COLLECTOR = collector

    original_execute = End2EndTest.execute

    if enabled:
        ovoscope.GLOBAL_BUS_COVERAGE = True
        ovoscope.GLOBAL_BUS_COVERAGE_FILE = cov_file
        # Initialize the global collector to catch boot-time events
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = ovoscope.GlobalBusCoverageCollector()

        # Auto-collect report after execution
        def patched_execute(self, *args, **kwargs):
            res = original_execute(self, *args, **kwargs)
            if self.bus_coverage_report:
                collector.add(self.bus_coverage_report)
            return res

        End2EndTest.execute = patched_execute

    try:
        yield collector
    finally:
        _SESSION_COLLECTOR = None
        if enabled:
            ovoscope.GLOBAL_BUS_COVERAGE = False
            ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = None
            # Restore original behavior
            End2EndTest.execute = original_execute
            # Note: we don't restore track_bus_coverage default because it's a
            # class attribute and we might have stepped on a manual True/False
            # in some tests, but in pytest context it usually doesn't matter
            # after the session ends.

    # Store the merged report on the config object so the terminal hook can
    # retrieve it without touching private pytest internals.
    report = collector.merged_report()
    if report is not None:
        # Apply filters
        include = request.config.getoption("--ovoscope-bus-cov-include")
        exclude = request.config.getoption("--ovoscope-bus-cov-exclude")
        report = report.filter(include=include, exclude=exclude)

        if not hasattr(request.config, "_bus_coverage_reports"):
            request.config._bus_coverage_reports = []
        request.config._bus_coverage_reports.append(report)

        # Save to file if requested
        cov_file = request.config.getoption("--ovoscope-bus-cov-file")
        if cov_file:
            import os
            try:
                os.makedirs(os.path.dirname(os.path.abspath(cov_file)), exist_ok=True)
                with open(cov_file, "w", encoding="utf-8") as f:
                    f.write(report.to_json())
            except Exception as exc:
                print(f"\nERROR: Failed to save bus coverage report to {cov_file}: {exc}")


def _bus_coverage_summary(terminalreporter, config):
    """Print the merged bus coverage report (factored out so the combined
    ``pytest_terminal_summary`` below can call both reporters)."""
    reports = getattr(config, "_bus_coverage_reports", None)
    if not reports:
        return
    verbose = config.getoption("--ovoscope-bus-cov-verbose")
    for report in reports:
        terminalreporter.write_sep("=", "Bus Coverage Report")
        report.print_report(verbose=verbose)
        terminalreporter.write_line("")


# ---------------------------------------------------------------------------
# Intent-case accuracy reporter
#
# Aggregates pass/fail per (pipeline, lang, intent) for tests generated by
# :func:`ovoscope.intent_cases.register_intent_case_tests`. Each generated
# method carries an ``_intent_case`` attribute; we read it during the
# ``pytest_runtest_logreport`` hook, accumulate, then emit a report and an
# optional pass/fail gate during ``pytest_terminal_summary``.
# ---------------------------------------------------------------------------
def _resolve_intent_case_meta(item):
    """Return (IntentCase, pipeline_name, skill_id) or None if not generated."""
    func = getattr(item, "function", None)
    if func is None:
        return None
    case = getattr(func, "_intent_case", None)
    if case is None:
        return None
    return (case,
            getattr(func, "_intent_case_pipeline", "Unknown"),
            getattr(func, "_intent_case_skill_id", ""))


def _autodiscover_intent_cases(config):
    """Walk pytest's loaded test modules and trigger auto-discovery.

    Pytest collects tests from files matching ``test_*.py`` (or
    ``*_test.py``) — *not* from ``conftest.py``. So the auto-discovery
    target is a thin shim module like ``test_intent_cases.py`` that
    declares::

        ovoscope_intent_cases = dict(skill_id=..., handlers=...)

    On the very first ``pytest_pycollect_makemodule`` we resolve the
    module, scan it for that declaration, and inject the generated
    TestCase classes into its namespace before pytest collects items
    from it. The user writes one variable assignment, no
    ``register_intent_case_tests`` call, no ``globals()`` argument.

    Skills already calling :func:`register_intent_case_tests` explicitly
    are skipped via the ``_ovoscope_intent_cases_registered`` marker.
    """
    # Tracked in pytest_pycollect_makemodule; this helper kept for symmetry
    # / future use (e.g. a CLI hook).
    return None


@pytest.hookimpl(wrapper=True)
def pytest_pycollect_makemodule(module_path, parent):
    """Auto-register intent-case tests on shim modules that declare
    ``ovoscope_intent_cases = {...}``.

    The hook wrapper imports the module first, lets pytest build the
    collector, then injects the generated TestCase classes into the
    module's namespace so the standard Python-class collector finds them.

    Uses the modern ``wrapper=True`` hook-wrapper protocol (pluggy >=1.2 /
    pytest >=7.2): the downstream result arrives from ``yield`` and is
    returned unchanged. The legacy ``hookwrapper=True`` /
    ``outcome.get_result()`` protocol is removed in pytest 10, so we adopt
    the new one to stay loadable under pytest 8 and 9.
    """
    from ovoscope.intent_cases import autodiscover_from_conftest

    collector = yield
    if collector is None:
        return collector
    try:
        mod = collector.obj  # imports the module if not already loaded
    except KeyboardInterrupt:
        raise
    except BaseException:
        # Importing the module here is a side effect of auto-discovery,
        # not the real collection attempt. A module-level
        # ``pytest.skip()``/``pytest.importorskip()`` raises
        # ``pytest.skip.Exception``, which subclasses ``BaseException``
        # (via ``_pytest.outcomes.OutcomeException``), not ``Exception`` -
        # a plain ``except Exception`` here lets it escape the hook
        # wrapper uncaught and abort the whole collection session. Swallow
        # it (and any other import-time failure) here and let pytest's own
        # protected collection call re-import the module later, where it
        # is reported as a normal per-module skip or error instead.
        return collector
    if not hasattr(mod, "ovoscope_intent_cases"):
        return collector
    if getattr(mod, "_ovoscope_intent_cases_registered", False):
        return collector
    try:
        autodiscover_from_conftest(Path(mod.__file__).parent, mod.__dict__)
    except Exception as exc:  # noqa: BLE001
        print(f"ovoscope auto-discovery skipped for {mod.__file__}: {exc}")
    return collector


def pytest_collection_modifyitems(config, items):
    """In tolerant mode, mark every intent-case test as ``xfail(strict=False)``.

    That lets the suite measure per-case routing accuracy without forcing
    every CI run to be green only when 100% of cases match. The aggregate
    gate (``--ovoscope-accuracy-min`` / ``--ovoscope-accuracy-baseline``)
    is the real blocker.
    """
    if not config.getoption("--ovoscope-accuracy-tolerant"):
        return
    for item in items:
        if _resolve_intent_case_meta(item) is not None:
            item.add_marker(pytest.mark.xfail(reason="intent-case (tolerant)",
                                              strict=False, run=True))


def pytest_runtest_logreport(report):
    """Record intent-case outcomes on the session config."""
    if report.when != "call":
        return
    item_func = getattr(report, "_intent_case_func", None)
    # We can't get the item directly from a logreport; fall back to nodeid.
    # The accumulator is keyed by nodeid -> (case, pipeline) which we set in
    # ``pytest_runtest_setup``.
    accum = getattr(pytest_runtest_logreport, "_accum", None)
    if accum is None:
        return
    meta = accum["meta"].get(report.nodeid)
    if meta is None:
        return
    case, pipeline, skill_id = meta
    passed = report.outcome == "passed" or (
        report.outcome == "skipped"
        and isinstance(report.longrepr, tuple)
        and "XPASS" in str(report.longrepr))
    # xfail/xpass handling: in tolerant mode a "failure" surfaces as xfail.
    if report.outcome == "skipped" and hasattr(report, "wasxfail"):
        passed = False
    accum["results"].append({
        "nodeid": report.nodeid,
        "skill_id": skill_id,
        "pipeline": pipeline,
        "lang": case.lang,
        "intent": case.intent or "no_match",
        "utterance": case.utterance,
        "source": str(case.source),
        "passed": passed,
    })


def pytest_runtest_setup(item):
    """Index intent-case metadata by nodeid for the logreport hook."""
    meta = _resolve_intent_case_meta(item)
    if meta is None:
        return
    accum = getattr(pytest_runtest_logreport, "_accum", None)
    if accum is None:
        accum = {"results": [], "meta": {}}
        pytest_runtest_logreport._accum = accum
    accum["meta"][item.nodeid] = meta


def _accuracy_summary(results):
    """Aggregate results into pivot tables for reporting.

    Adds, on top of the previous pivots, a per-utterance roll-up that
    feeds the "hardest utterances" section of the Markdown report.
    """
    by_pipeline: Dict[str, Dict[str, int]] = {}
    by_pipeline_lang: Dict[tuple, Dict[str, int]] = {}
    by_pipeline_intent: Dict[tuple, Dict[str, int]] = {}
    # Cross-pipeline rollup: (lang, intent, utterance) -> {pass, total,
    # failing_pipelines}. Lets us surface "which exact phrasing routes
    # poorly across the whole stack" in one place.
    by_utterance: Dict[tuple, Dict[str, object]] = {}
    total_pass = total = 0
    for r in results:
        total += 1
        if r["passed"]:
            total_pass += 1
        for bucket, key in (
                (by_pipeline, r["pipeline"]),
                (by_pipeline_lang, (r["pipeline"], r["lang"])),
                (by_pipeline_intent, (r["pipeline"], r["intent"])),
        ):
            d = bucket.setdefault(key, {"pass": 0, "total": 0})
            d["total"] += 1
            if r["passed"]:
                d["pass"] += 1
        utt_key = (r["lang"], r["intent"], r["utterance"])
        u = by_utterance.setdefault(utt_key, {
            "lang": r["lang"], "intent": r["intent"],
            "utterance": r["utterance"],
            "pass": 0, "total": 0, "failing_pipelines": []})
        u["total"] += 1
        if r["passed"]:
            u["pass"] += 1
        else:
            u["failing_pipelines"].append(r["pipeline"])
    overall = (total_pass / total) if total else 0.0
    return {
        "overall_accuracy": overall,
        "passed": total_pass,
        "total": total,
        "by_pipeline": by_pipeline,
        "by_pipeline_lang": {f"{p}|{l}": v for (p, l), v in by_pipeline_lang.items()},
        "by_pipeline_intent": {f"{p}|{i}": v for (p, i), v in by_pipeline_intent.items()},
        "by_utterance": list(by_utterance.values()),
    }


# ---------------------------------------------------------------------------
# Baseline diff
# ---------------------------------------------------------------------------
def _result_key(r: dict) -> tuple:
    """Stable identity for a single (pipeline, lang, intent, utterance) case."""
    return (r.get("pipeline", ""), r.get("lang", ""),
            r.get("intent", ""), r.get("utterance", ""))


def _baseline_diff(baseline_results, current_results):
    """Compare two result lists by case key, returning a structural diff.

    Returns ``{"regressed": [...], "recovered": [...], "added": [...],
    "removed": [...], "baseline_accuracy": float}``. Each item in
    ``regressed`` / ``recovered`` is the matching *current* result dict
    so callers can quote the offending utterance verbatim.
    """
    base_by_key = {_result_key(r): r for r in baseline_results or []}
    cur_by_key = {_result_key(r): r for r in current_results or []}

    regressed, recovered, added, removed = [], [], [], []
    for k, cur in cur_by_key.items():
        if k not in base_by_key:
            added.append(cur)
            continue
        base = base_by_key[k]
        if base.get("passed") and not cur.get("passed"):
            regressed.append(cur)
        elif (not base.get("passed")) and cur.get("passed"):
            recovered.append(cur)
    for k, base in base_by_key.items():
        if k not in cur_by_key:
            removed.append(base)

    base_total = len(baseline_results or [])
    base_pass = sum(1 for r in (baseline_results or []) if r.get("passed"))
    base_acc = (base_pass / base_total) if base_total else 0.0

    return {
        "regressed": regressed, "recovered": recovered,
        "added": added, "removed": removed,
        "baseline_accuracy": base_acc, "baseline_passed": base_pass,
        "baseline_total": base_total,
    }


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------
def _accuracy_markdown(summary, results, baseline_diff=None, top_n=10):
    """Render the intent-case accuracy summary as Markdown.

    Mirrors the table-and-collapsible-section style used by the existing
    bus-coverage section in the gh-automations PR-comment workflow:
    headline line, summary table, per-(pipeline,lang) and
    per-(pipeline,intent) breakdowns in ``<details>`` blocks, then a
    "hardest utterances" call-out and, when present, a baseline diff.
    """
    overall = summary["overall_accuracy"]
    icon = "✅" if overall >= 0.9 else ("⚠️" if overall >= 0.7 else "❌")
    lines = [
        f"{icon} **{summary['passed']}/{summary['total']}** intent-case "
        f"checks passed — overall accuracy **{overall:.1%}**.",
        "",
    ]

    # --- Per-pipeline summary table (always visible) ---
    lines.append("| Pipeline | Pass / Total | Accuracy |")
    lines.append("|---|---:|---:|")
    for pipe, d in sorted(summary["by_pipeline"].items()):
        ratio = d["pass"] / d["total"] if d["total"] else 0.0
        lines.append(f"| `{pipe}` | {d['pass']} / {d['total']} | {ratio:.1%} |")
    lines.append("")

    # --- Per-(pipeline, lang) ---
    lines.append("<details><summary>Per-pipeline × language</summary>")
    lines.append("")
    lines.append("| Pipeline | Lang | Pass / Total | Accuracy |")
    lines.append("|---|---|---:|---:|")
    for key in sorted(summary["by_pipeline_lang"]):
        pipe, lang = key.split("|", 1)
        d = summary["by_pipeline_lang"][key]
        ratio = d["pass"] / d["total"] if d["total"] else 0.0
        lines.append(f"| `{pipe}` | `{lang}` | {d['pass']} / {d['total']} | {ratio:.1%} |")
    lines.append("")
    lines.append("</details>")
    lines.append("")

    # --- Per-(pipeline, intent) ---
    lines.append("<details><summary>Per-pipeline × intent</summary>")
    lines.append("")
    lines.append("| Pipeline | Intent | Pass / Total | Accuracy |")
    lines.append("|---|---|---:|---:|")
    for key in sorted(summary["by_pipeline_intent"]):
        pipe, intent = key.split("|", 1)
        d = summary["by_pipeline_intent"][key]
        ratio = d["pass"] / d["total"] if d["total"] else 0.0
        lines.append(f"| `{pipe}` | `{intent}` | {d['pass']} / {d['total']} | {ratio:.1%} |")
    lines.append("")
    lines.append("</details>")
    lines.append("")

    # --- Hardest utterances (lowest cross-pipeline pass rate) ---
    utts = list(summary.get("by_utterance", []))
    # Only utterances that failed at least once and aren't no_match
    hard = [u for u in utts
            if u["total"] > 0 and u["pass"] < u["total"]
            and u["intent"] != "no_match"]
    hard.sort(key=lambda u: (u["pass"] / u["total"], -u["total"]))
    if hard:
        lines.append(f"<details><summary>Hardest utterances "
                     f"(top {min(top_n, len(hard))})</summary>")
        lines.append("")
        lines.append("| Lang | Intent | Utterance | Pass / Total | Failing pipelines |")
        lines.append("|---|---|---|---:|---|")
        for u in hard[:top_n]:
            fail_pipes = ", ".join(f"`{p}`" for p in sorted(set(u["failing_pipelines"])))
            lines.append(f"| `{u['lang']}` | `{u['intent']}` "
                         f"| {u['utterance']} "
                         f"| {u['pass']} / {u['total']} | {fail_pipes} |")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # --- Baseline diff (when supplied) ---
    if baseline_diff is not None:
        regressed = baseline_diff["regressed"]
        recovered = baseline_diff["recovered"]
        delta = overall - baseline_diff["baseline_accuracy"]
        delta_str = f"{delta:+.1%}"
        head = (f"vs baseline ({baseline_diff['baseline_passed']}/"
                f"{baseline_diff['baseline_total']} = "
                f"{baseline_diff['baseline_accuracy']:.1%}): "
                f"{len(regressed)} regressed, {len(recovered)} recovered, "
                f"Δ {delta_str}")
        lines.append(f"**Baseline diff** — {head}")
        lines.append("")
        if regressed:
            lines.append("<details open><summary>❌ Regressed "
                         f"({len(regressed)})</summary>")
            lines.append("")
            lines.append("| Pipeline | Lang | Intent | Utterance |")
            lines.append("|---|---|---|---|")
            for r in regressed[:50]:
                lines.append(f"| `{r['pipeline']}` | `{r['lang']}` | "
                             f"`{r['intent']}` | {r['utterance']} |")
            if len(regressed) > 50:
                lines.append(f"| … | … | … | _+{len(regressed)-50} more_ |")
            lines.append("")
            lines.append("</details>")
            lines.append("")
        if recovered:
            lines.append("<details><summary>✅ Recovered "
                         f"({len(recovered)})</summary>")
            lines.append("")
            lines.append("| Pipeline | Lang | Intent | Utterance |")
            lines.append("|---|---|---|---|")
            for r in recovered[:50]:
                lines.append(f"| `{r['pipeline']}` | `{r['lang']}` | "
                             f"`{r['intent']}` | {r['utterance']} |")
            if len(recovered) > 50:
                lines.append(f"| … | … | … | _+{len(recovered)-50} more_ |")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _gate_state(config):
    """Compute (and cache) the accuracy summary, baseline diff, and gate result.

    Called from ``pytest_sessionfinish`` — which runs BEFORE
    ``pytest_terminal_summary`` — so the exit status can be set from the same
    numbers the summary prints. The result is cached on *config*, so the
    summary reuses it instead of re-reading the baseline file.

    Returns:
        Dict with ``summary``, ``baseline_diff``, ``baseline_warning`` and
        ``failures``, or ``None`` when no intent-case results were collected.
    """
    cached = getattr(config, "_ovoscope_gate_state", None)
    if cached is not None:
        return cached

    accum = getattr(pytest_runtest_logreport, "_accum", None)
    if not accum or not accum["results"]:
        return None
    summary = _accuracy_summary(accum["results"])

    baseline_path = config.getoption("--ovoscope-accuracy-baseline")
    baseline_diff = None
    baseline_warning = None
    if baseline_path:
        try:
            import json as _json
            with open(baseline_path, "r", encoding="utf-8") as fh:
                baseline_doc = _json.load(fh)
            baseline_diff = _baseline_diff(
                baseline_doc.get("results") or [],
                accum["results"])
        except Exception as exc:
            baseline_warning = (f"could not read baseline "
                                f"{baseline_path}: {exc}")

    min_acc = config.getoption("--ovoscope-accuracy-min")
    failures = []
    if min_acc is not None and summary["overall_accuracy"] < min_acc:
        failures.append(
            f"overall accuracy {summary['overall_accuracy']:.1%} < "
            f"required {min_acc:.1%}")
    if baseline_diff is not None and baseline_diff["regressed"]:
        top_regression = baseline_diff["regressed"][0]
        failures.append(
            f"{len(baseline_diff['regressed'])} cases regressed vs "
            f"baseline (first: `{top_regression['pipeline']}` / "
            f"`{top_regression['lang']}` / "
            f"`{top_regression['intent']}` / "
            f"{top_regression['utterance']!r})")

    state = {"summary": summary,
             "baseline_diff": baseline_diff,
             "baseline_warning": baseline_warning,
             "failures": failures}
    config._ovoscope_gate_state = state
    return state


def pytest_terminal_summary(terminalreporter, exitstatus, config):  # noqa: ARG001
    """Combined session summary: bus coverage + intent-case accuracy."""
    _bus_coverage_summary(terminalreporter, config)
    accum = getattr(pytest_runtest_logreport, "_accum", None)
    if not accum or not accum["results"]:
        return
    state = _gate_state(config)
    summary = state["summary"]
    baseline_diff = state["baseline_diff"]
    baseline_warning = state["baseline_warning"]

    tr = terminalreporter
    tr.write_sep("=", "ovoscope Intent-Case Accuracy")
    tr.write_line(
        f"Overall: {summary['passed']}/{summary['total']} "
        f"= {summary['overall_accuracy']:.1%}")
    tr.write_line("")
    tr.write_line("By pipeline:")
    for pipe, d in sorted(summary["by_pipeline"].items()):
        ratio = d["pass"] / d["total"] if d["total"] else 0.0
        tr.write_line(f"  {pipe:24s}  {d['pass']:4d}/{d['total']:<4d}  {ratio:>6.1%}")
    tr.write_line("")
    tr.write_line("By pipeline x lang:")
    for key in sorted(summary["by_pipeline_lang"]):
        d = summary["by_pipeline_lang"][key]
        ratio = d["pass"] / d["total"] if d["total"] else 0.0
        tr.write_line(f"  {key:36s}  {d['pass']:4d}/{d['total']:<4d}  {ratio:>6.1%}")
    tr.write_line("")
    tr.write_line("By pipeline x intent:")
    for key in sorted(summary["by_pipeline_intent"]):
        d = summary["by_pipeline_intent"][key]
        ratio = d["pass"] / d["total"] if d["total"] else 0.0
        tr.write_line(f"  {key:48s}  {d['pass']:4d}/{d['total']:<4d}  {ratio:>6.1%}")

    if baseline_warning:
        tr.write_line(f"\nWARNING: {baseline_warning}")

    # Persist JSON.
    report_path = config.getoption("--ovoscope-accuracy-report")
    if report_path:
        import json
        import os
        os.makedirs(os.path.dirname(os.path.abspath(report_path)) or ".",
                    exist_ok=True)
        doc = {"summary": summary, "results": accum["results"]}
        if baseline_diff is not None:
            doc["baseline_diff"] = {
                "regressed": baseline_diff["regressed"],
                "recovered": baseline_diff["recovered"],
                "added": baseline_diff["added"],
                "removed": baseline_diff["removed"],
                "baseline_accuracy": baseline_diff["baseline_accuracy"],
                "baseline_passed": baseline_diff["baseline_passed"],
                "baseline_total": baseline_diff["baseline_total"],
            }
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        tr.write_line(f"\nWrote accuracy report -> {report_path}")

    # Persist Markdown (for PR-comment ingestion).
    md_path = config.getoption("--ovoscope-accuracy-md")
    if md_path:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(md_path)) or ".",
                    exist_ok=True)
        top_n = config.getoption("--ovoscope-accuracy-top-n")
        md = _accuracy_markdown(summary, accum["results"],
                                baseline_diff=baseline_diff, top_n=top_n)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(md)
        tr.write_line(f"Wrote accuracy markdown -> {md_path}")

    # Report the gate result computed in pytest_sessionfinish.
    if state["failures"]:
        tr.write_sep("!", "ovoscope accuracy gate FAILED")
        for f in state["failures"]:
            tr.write_line(f"  - {f}")


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Evaluate the accuracy gate and propagate it as a non-zero exit status.

    The gate MUST be computed here, not in ``pytest_terminal_summary``:
    sessionfinish runs first, so a flag set by the summary would always be
    read too late and the gate would never change the exit status.
    """
    state = _gate_state(session.config)
    if state and state["failures"]:
        session.config._ovoscope_accuracy_gate_failed = True
        if session.exitstatus == 0:
            session.exitstatus = 1
