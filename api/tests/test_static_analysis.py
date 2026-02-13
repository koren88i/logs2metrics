"""Static analysis tests — grep-based checks on source code.

These tests read source files and flag patterns that have caused bugs.
They act as automated coding standard enforcement from CLAUDE.md.
"""

import os
import re
import pytest

API_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)))
HTML_PATH = os.path.join(API_DIR, "debug_ui.html")


def _read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestNoRawFetchBypassingApiWrapper:
    """Bug 6 prevention: all backend calls must use api() wrapper, not raw fetch()."""

    def test_no_raw_fetch_outside_api_definition(self):
        content = _read_file(HTML_PATH)
        lines = content.split("\n")
        violations = []

        in_api_function = False
        api_brace_depth = 0
        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # Track the api() function body using brace depth
            if not in_api_function and "async function api(" in stripped:
                in_api_function = True
                api_brace_depth = 0

            if in_api_function:
                api_brace_depth += stripped.count("{") - stripped.count("}")
                # Function ends when brace depth returns to 0 after opening
                if api_brace_depth <= 0 and "{" not in stripped:
                    in_api_function = False
                continue

            # Allow the config fetch (known exception — runs before api() is ready)
            if "fetch('/api/config')" in stripped or 'fetch("/api/config")' in stripped:
                continue
            if "fetch('/api/config'" in stripped or 'fetch("/api/config"' in stripped:
                continue

            # Flag raw fetch() calls
            if re.search(r'\bfetch\s*\(', stripped):
                violations.append(f"Line {i}: {stripped[:100]}")

        assert violations == [], (
            f"Found raw fetch() calls outside api() definition (Bug 6 risk). "
            f"Use the api() wrapper instead:\n" + "\n".join(violations)
        )


class TestNoReservedFieldNamesInMappings:
    """Bug 5 prevention: no ES reserved field names in Python mapping/agg code."""

    def test_no_doc_count_in_elastic_backend(self):
        backend_path = os.path.join(API_DIR, "elastic_backend.py")
        content = _read_file(backend_path)

        violations = []
        for i, line in enumerate(content.split("\n"), 1):
            if line.strip().startswith("#"):
                continue
            # Match: aggs["doc_count"] or properties["doc_count"]
            if re.search(r'(aggs|properties)\["doc_count"\]', line):
                violations.append(f"Line {i}: {line.strip()[:100]}")

        assert violations == [], (
            f"Found 'doc_count' as output field in elastic_backend.py. "
            f"Use 'event_count' instead (Bug 5):\n" + "\n".join(violations)
        )

    def test_kibana_connector_uses_event_count(self):
        kc_path = os.path.join(API_DIR, "kibana_connector.py")
        content = _read_file(kc_path)

        # The _METRIC_AGG_MAP should reference event_count for count type
        assert '"event_count"' in content, (
            "_METRIC_AGG_MAP should map count type to 'event_count', not 'doc_count'. "
            "See Bug 5 in CLAUDE.md."
        )


class TestNoInnerHtmlPlusEqualsInLoops:
    """Bug 1 prevention: innerHTML += inside loops destroys DOM references."""

    def test_no_innerhtml_plus_equals_in_for_loops(self):
        content = _read_file(HTML_PATH)
        lines = content.split("\n")
        violations = []

        # Simple heuristic: track brace depth after for/forEach/while
        in_loop = False
        loop_depth = 0
        brace_depth = 0

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            if re.search(r'\b(for\s*\(|\.forEach\s*\(|while\s*\()', stripped):
                in_loop = True
                loop_depth += 1

            if in_loop:
                brace_depth += stripped.count("{") - stripped.count("}")
                if brace_depth <= 0 and loop_depth > 0:
                    loop_depth -= 1
                    if loop_depth == 0:
                        in_loop = False
                        brace_depth = 0

            if in_loop and "innerHTML +=" in line:
                violations.append(f"Line {i}: {stripped[:80]}")

        assert violations == [], (
            f"Found innerHTML += inside loops (Bug 1 risk). "
            f"Build HTML as a string first, then assign once:\n"
            + "\n".join(violations)
        )
