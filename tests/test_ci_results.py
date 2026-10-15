"""Ensure CI cannot turn skipped live coverage or incomplete reports into a green check."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ci_results", Path(__file__).resolve().parents[1] / "scripts" / "ci_results.py"
)
ci_results = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_results)


def write_report(tmp_path, *, outcome="", omit_live=False, duplicate_live=False):
    names = ["test_worker_recovery_faults_restarts_and_review"]
    if not omit_live:
        names.append("test_live_worker_recovery_faults_restarts_and_review")
    if duplicate_live:
        names.append(names[-1])
    cases = "".join(
        f'<testcase classname="tests.test_worker_e2e" name="{name}">{outcome}</testcase>'
        for name in names
    )
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    return path


def test_ci_accepts_both_completed_worker_scenarios(tmp_path):
    summary, success = ci_results.check_report(write_report(tmp_path))
    assert success
    assert "| 2 | 0 | 0 | 0 |" in summary


@pytest.mark.parametrize("outcome", ["<skipped/>", "<failure/>", "<error/>"])
def test_ci_rejects_skipped_or_failed_live_tests(tmp_path, outcome):
    _, success = ci_results.check_report(write_report(tmp_path, outcome=outcome))
    assert not success


@pytest.mark.parametrize("options", [{"omit_live": True}, {"duplicate_live": True}])
def test_ci_rejects_missing_or_duplicate_live_results(tmp_path, options):
    _, success = ci_results.check_report(write_report(tmp_path, **options))
    assert not success


def test_ci_rejects_empty_reports(tmp_path):
    path = tmp_path / "empty.xml"
    path.write_text("<testsuites/>")
    _, success = ci_results.check_report(path)
    assert not success
