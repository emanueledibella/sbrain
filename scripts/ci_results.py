"""Reject incomplete CI runs, including silently skipped live worker recovery tests."""

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REQUIRED_TESTS = {
    ("tests.test_worker_e2e", "test_worker_recovery_faults_restarts_and_review"),
    ("tests.test_worker_e2e", "test_live_worker_recovery_faults_restarts_and_review"),
}


def check_report(path: Path) -> tuple[str, bool]:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = sum(case.find("failure") is not None for case in cases)
    errors = sum(case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    passed = len(cases) - failures - errors - skipped
    problems = []
    if not cases:
        problems.append("The report contains no test cases.")
    if failures or errors or skipped:
        problems.append("All tests must pass; failures, errors and skips are rejected.")
    for classname, name in sorted(REQUIRED_TESTS):
        matches = [
            case for case in cases
            if case.get("classname") == classname and case.get("name") == name
        ]
        if len(matches) != 1:
            problems.append(f"Expected exactly one result for `{classname}.{name}`.")
    summary = (
        "## Full suite (Ollama + Neo4j)\n\n"
        "| Passed | Failures | Errors | Skipped |\n"
        "| --- | --- | --- | --- |\n"
        f"| {passed} | {failures} | {errors} | {skipped} |\n\n"
    )
    if problems:
        summary += "\n".join(f"- {problem}" for problem in problems) + "\n"
    else:
        summary += "Both deterministic and real-service worker recovery scenarios passed.\n"
    return summary, not problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    try:
        summary, success = check_report(args.report)
    except (OSError, ET.ParseError) as exc:
        summary, success = f"CI test report could not be read: {exc}\n", False
    print(summary, end="")
    if args.summary:
        with args.summary.open("a") as stream:
            stream.write(summary)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
