# -*- coding: utf-8 -*-
"""
Sewa Setu - Unified Automated Test Suite & Evaluation Runner.

Executes all unit, integration, MCP, privacy, security, and statutory deadline
test suites, captures deterministic results, and records the evaluation summary
into tests/test_results.json for display on the Evaluation Dashboard.
"""

import os
import sys
import json
import time
import unittest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.join(BASE_DIR, "tests")
RESULTS_PATH = os.path.join(TESTS_DIR, "test_results.json")

# Ensure app directory is in Python path
sys.path.insert(0, os.path.join(BASE_DIR, "app"))


def main():
    print("=" * 70)
    print("SEWA SETU — FULL AUTOMATED TEST SUITE & SECURITY EVALUATION")
    print("=" * 70)

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=TESTS_DIR, pattern="test_*.py")

    total_tests = suite.countTestCases()
    print(f"Discovered {total_tests} test cases across all phases.\n")

    start_time = time.time()
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    duration = time.time() - start_time

    passed = total_tests - (len(result.failures) + len(result.errors))
    failed = len(result.failures) + len(result.errors)

    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_tests": total_tests,
        "passed_tests": passed,
        "failed_tests": failed,
        "duration_seconds": round(duration, 3),
        "success": result.wasSuccessful(),
        "suites": [
            "test_shared_services.py",
            "test_citizen_mcp.py",
            "test_citizen_ux_privacy.py",
            "test_officer_mcp.py",
            "test_automatic_approval.py",
            "test_e2e_security_resilience.py"
        ],
        "failures": [
            {"test": str(f[0]), "error": str(f[1])}
            for f in result.failures
        ],
        "errors": [
            {"test": str(e[0]), "error": str(e[1])}
            for e in result.errors
        ]
    }

    try:
        with open(RESULTS_PATH, "w") as fh:
            json.dump(summary, fh, indent=2)
        print(f"\nEvaluation summary recorded to: {RESULTS_PATH}")
    except Exception as ex:
        print(f"Warning: Could not save test_results.json: {ex}")

    print("\n" + "=" * 70)
    print(f"TOTAL TESTS : {total_tests}")
    print(f"PASSED      : {passed}")
    print(f"FAILED      : {failed}")
    print(f"DURATION    : {duration:.2f}s")
    print(f"STATUS      : {'SUCCESS (ALL PASSED)' if result.wasSuccessful() else 'FAILED'}")
    print("=" * 70)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
