"""Tests for the fresh `holdout2` S1 split added to scripts/battery_tasks.py.

Reference solutions and plausible-wrong solutions for the 9 new `holdout2`
code tasks live ONLY in this file -- never in scripts/battery_tasks.py and
never in any Task.files dict -- so a coding agent working one of these tasks
can never read them from its workspace (Task.files is exactly what gets
copied into the agent's workspace; these dicts are not part of it).

For every code task this file proves:
  1. the unmodified starter FAILS task.evaluate(),
  2. the reference solution PASSES task.evaluate() (failed == 0, checks > 0)
     and, when a test_public.py ships, passes it via subprocess,
  3. at least one plausible wrong solution FAILS task.evaluate() (or fails
     test_public.py when one ships).

For the 3 new `answer` tasks it proves the correct token passes and
plausible decoys fail, using the same battery_tasks.check_answer() harness
as the rest of the suite.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import battery_tasks as bt  # noqa: E402


def _materialize(files: dict, extra: dict | None = None) -> Path:
    d = Path(tempfile.mkdtemp())
    merged = dict(files)
    if extra:
        merged.update(extra)
    for name, content in merged.items():
        path = d / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# holdout2 reference solutions -- TEST-ONLY, never shipped in Task.files.
# ---------------------------------------------------------------------------

_REFERENCE_SOLUTIONS_H2: dict[str, dict[str, str]] = {
    "repair_roman_to_int": {
        "solution.py": '''import re

_RE = re.compile(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})")
_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_to_int(s):
    if not isinstance(s, str):
        raise TypeError("s must be a string")
    if not s or not _RE.fullmatch(s):
        raise ValueError("invalid roman numeral")
    total = 0
    i = 0
    while i < len(s):
        if i + 1 < len(s) and _VALUES[s[i]] < _VALUES[s[i + 1]]:
            total += _VALUES[s[i + 1]] - _VALUES[s[i]]
            i += 2
        else:
            total += _VALUES[s[i]]
            i += 1
    return total
''',
    },
    "repair_flatten_ints": {
        "solution.py": '''def flatten_ints(value):
    if not isinstance(value, list):
        raise TypeError("value must be a list")
    out = []

    def go(v):
        if isinstance(v, list):
            for item in v:
                go(item)
        elif isinstance(v, bool):
            raise ValueError("bool not allowed")
        elif isinstance(v, int):
            out.append(v)
        else:
            raise ValueError("invalid element")

    go(value)
    return out
''',
    },
    "repair_reorder_point": {
        "solution.py": '''import math
from stats_util import mean, stdev
from demand_config import MIN_LEAD_TIME_DAYS, MAX_SERVICE_Z


def reorder_point(daily_demand, lead_time_days, service_z):
    if not isinstance(daily_demand, list):
        raise TypeError("daily_demand must be a list")
    if isinstance(lead_time_days, bool) or not isinstance(lead_time_days, int):
        raise TypeError("lead_time_days must be an int")
    if isinstance(service_z, bool) or not isinstance(service_z, (int, float)):
        raise TypeError("service_z must be a number")
    if len(daily_demand) < 2:
        raise ValueError("daily_demand needs at least two samples")
    for v in daily_demand:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError("daily_demand values must be numbers")
        if v < 0:
            raise ValueError("daily_demand values must be nonnegative")
    if lead_time_days < MIN_LEAD_TIME_DAYS:
        raise ValueError("lead_time_days must be >= MIN_LEAD_TIME_DAYS")
    if service_z < 0 or service_z > MAX_SERVICE_Z:
        raise ValueError("service_z must be between 0 and MAX_SERVICE_Z")
    m = mean(daily_demand)
    sd = stdev(daily_demand)
    rp = m * lead_time_days + service_z * sd * math.sqrt(lead_time_days)
    return math.ceil(rp)
''',
    },
    "edit_temp_round_fahrenheit": {
        "temp_util.py": '''def to_fahrenheit(celsius):
    return round(celsius * 9 / 5 + 32, 1)


def to_celsius(fahrenheit):
    return (fahrenheit - 32) * 5 / 9
''',
    },
    "edit_slugify_max_length": {
        "slugify.py": '''import re


def slugify(text, max_length=None):
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    slug = text.strip("-")
    if max_length is None or len(slug) <= max_length:
        return slug
    cut = slug.rfind("-", 0, max_length + 1)
    if cut == -1:
        return slug[:max_length]
    return slug[:cut]
''',
    },
    "edit_cart_apply_coupon": {
        "cart.py": '''from pricing import COUPONS, apply_discount


class Cart:
    def __init__(self):
        self._items = []
        self._coupon_code = None

    def add_item(self, name, price, qty=1):
        if price < 0 or qty <= 0:
            raise ValueError("invalid item")
        self._items.append((name, price, qty))

    def subtotal(self):
        return round(sum(price * qty for _, price, qty in self._items), 2)

    def apply_coupon(self, code):
        if code not in COUPONS:
            raise KeyError(code)
        self._coupon_code = code

    def remove_coupon(self):
        self._coupon_code = None

    def total(self):
        sub = self.subtotal()
        if self._coupon_code is None:
            return sub
        return apply_discount(sub, COUPONS[self._coupon_code])
''',
    },
    "bugfix_dedupe_keep_order": {
        "dedupe_util.py": '''def dedupe_keep_order(items):
    if not isinstance(items, list):
        raise TypeError("items must be a list")
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
''',
    },
    "bugfix_rate_limiter_off_by_one": {
        "rate_limiter.py": '''def allow_request(timestamps, now, window_seconds, max_requests):
    if not isinstance(timestamps, list):
        raise TypeError("timestamps must be a list")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if max_requests < 1:
        raise ValueError("max_requests must be >= 1")
    for t in timestamps:
        if t > now:
            raise ValueError("timestamp in the future")
    cutoff = now - window_seconds
    count = sum(1 for t in timestamps if t > cutoff)
    return count < max_requests
''',
    },
    "bugfix_invoice_tax_unit_mismatch": {
        "invoice.py": '''from tax_table import rate_for


def line_total(unit_price, quantity):
    if unit_price < 0 or quantity < 0:
        raise ValueError("unit_price and quantity must be nonnegative")
    return round(unit_price * quantity, 2)


def invoice_total(line_items, region):
    if not isinstance(line_items, list):
        raise TypeError("line_items must be a list")
    subtotal = round(sum(line_total(p, q) for p, q in line_items), 2)
    rate = rate_for(region)
    tax = subtotal * rate
    return round(subtotal + tax, 2)
''',
    },
}

_WRONG_SOLUTIONS_H2: dict[str, dict[str, str]] = {
    "repair_roman_to_int": {
        # Handles subtractive pairs but never validates max-3-repeat, so it
        # accepts "IIII" (should be ValueError) -- fails the boundary check.
        "solution.py": '''def roman_to_int(s):
    if not isinstance(s, str):
        raise TypeError("s must be a string")
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    if not s or any(ch not in values for ch in s):
        raise ValueError("invalid roman numeral")
    total = 0
    i = 0
    while i < len(s):
        if i + 1 < len(s) and values[s[i]] < values[s[i + 1]]:
            total += values[s[i + 1]] - values[s[i]]
            i += 2
        else:
            total += values[s[i]]
            i += 1
    return total
''',
    },
    "repair_flatten_ints": {
        # Validates elements but forgets to reject bool, so [1, True] is
        # accepted instead of raising ValueError.
        "solution.py": '''def flatten_ints(value):
    if not isinstance(value, list):
        raise TypeError("value must be a list")
    out = []

    def go(v):
        if isinstance(v, list):
            for item in v:
                go(item)
        elif isinstance(v, int):
            out.append(v)
        else:
            raise ValueError("invalid element")

    go(value)
    return out
''',
    },
    "repair_reorder_point": {
        # Forgets to multiply the safety-stock term by sqrt(lead_time_days).
        "solution.py": '''import math
from stats_util import mean, stdev
from demand_config import MIN_LEAD_TIME_DAYS, MAX_SERVICE_Z


def reorder_point(daily_demand, lead_time_days, service_z):
    if not isinstance(daily_demand, list):
        raise TypeError("daily_demand must be a list")
    if isinstance(lead_time_days, bool) or not isinstance(lead_time_days, int):
        raise TypeError("lead_time_days must be an int")
    if isinstance(service_z, bool) or not isinstance(service_z, (int, float)):
        raise TypeError("service_z must be a number")
    if len(daily_demand) < 2:
        raise ValueError("daily_demand needs at least two samples")
    for v in daily_demand:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError("daily_demand values must be numbers")
        if v < 0:
            raise ValueError("daily_demand values must be nonnegative")
    if lead_time_days < MIN_LEAD_TIME_DAYS:
        raise ValueError("lead_time_days must be >= MIN_LEAD_TIME_DAYS")
    if service_z < 0 or service_z > MAX_SERVICE_Z:
        raise ValueError("service_z must be between 0 and MAX_SERVICE_Z")
    m = mean(daily_demand)
    sd = stdev(daily_demand)
    rp = m * lead_time_days + service_z * sd  # BUG (wrong-solution): missing sqrt(lead_time_days)
    return math.ceil(rp)
''',
    },
    "edit_temp_round_fahrenheit": {
        # Rounds to 0 decimals (an int) instead of 1 decimal place.
        "temp_util.py": '''def to_fahrenheit(celsius):
    return round(celsius * 9 / 5 + 32)


def to_celsius(fahrenheit):
    return (fahrenheit - 32) * 5 / 9
''',
    },
    "edit_slugify_max_length": {
        # Hard-truncates unconditionally, cutting words mid-word instead of
        # preferring the last hyphen at or before max_length.
        "slugify.py": '''import re


def slugify(text, max_length=None):
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    slug = text.strip("-")
    if max_length is None:
        return slug
    return slug[:max_length].rstrip("-")
''',
    },
    "edit_cart_apply_coupon": {
        # Silently ignores an unknown coupon code instead of raising KeyError.
        "cart.py": '''from pricing import COUPONS, apply_discount


class Cart:
    def __init__(self):
        self._items = []
        self._coupon_code = None

    def add_item(self, name, price, qty=1):
        if price < 0 or qty <= 0:
            raise ValueError("invalid item")
        self._items.append((name, price, qty))

    def subtotal(self):
        return round(sum(price * qty for _, price, qty in self._items), 2)

    def apply_coupon(self, code):
        if code in COUPONS:
            self._coupon_code = code
        # BUG (wrong-solution): unknown codes are silently ignored, no KeyError

    def remove_coupon(self):
        self._coupon_code = None

    def total(self):
        sub = self.subtotal()
        if self._coupon_code is None:
            return sub
        return apply_discount(sub, COUPONS[self._coupon_code])
''',
    },
    "bugfix_dedupe_keep_order": {
        # Forgets to add to `seen`, so nothing is ever deduped.
        "dedupe_util.py": '''def dedupe_keep_order(items):
    if not isinstance(items, list):
        raise TypeError("items must be a list")
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            out.append(item)  # BUG (wrong-solution): forgot to add to `seen`
    return out
''',
    },
    "bugfix_rate_limiter_off_by_one": {
        # Fixes the off-by-one but introduces a different boundary bug
        # (inclusive cutoff), still allowing one request too many at the
        # exact-cutoff boundary case.
        "rate_limiter.py": '''def allow_request(timestamps, now, window_seconds, max_requests):
    if not isinstance(timestamps, list):
        raise TypeError("timestamps must be a list")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if max_requests < 1:
        raise ValueError("max_requests must be >= 1")
    for t in timestamps:
        if t > now:
            raise ValueError("timestamp in the future")
    cutoff = now - window_seconds
    count = sum(1 for t in timestamps if t >= cutoff)  # BUG (wrong-solution): inclusive cutoff
    return count < max_requests
''',
    },
    "bugfix_invoice_tax_unit_mismatch": {
        # Fixes the unit mismatch but, while rewriting line_total, drops the
        # nonnegative-price validation -- a very plausible slip that passes
        # every "happy path" case but fails the negative-price boundary.
        "invoice.py": '''from tax_table import rate_for


def line_total(unit_price, quantity):
    return round(unit_price * quantity, 2)  # BUG (wrong-solution): lost the validation


def invoice_total(line_items, region):
    if not isinstance(line_items, list):
        raise TypeError("line_items must be a list")
    subtotal = round(sum(line_total(p, q) for p, q in line_items), 2)
    rate = rate_for(region)
    tax = subtotal * rate
    return round(subtotal + tax, 2)
''',
    },
}

_HOLDOUT2_CODE_TASK_NAMES = [
    "repair_roman_to_int",
    "repair_flatten_ints",
    "repair_reorder_point",
    "edit_temp_round_fahrenheit",
    "edit_slugify_max_length",
    "edit_cart_apply_coupon",
    "bugfix_dedupe_keep_order",
    "bugfix_rate_limiter_off_by_one",
    "bugfix_invoice_tax_unit_mismatch",
]

_HOLDOUT2_ANSWER_TASK_NAMES = [
    "answer_cache_eviction_policy",
    "answer_retry_max_attempts",
    "answer_shipping_zone_count",
]

_HOLDOUT2_LONG_TASKS = {
    "repair_reorder_point",
    "edit_cart_apply_coupon",
    "bugfix_invoice_tax_unit_mismatch",
    "answer_shipping_zone_count",
}


class TestHoldout2RegistryShape(unittest.TestCase):
    def test_twelve_holdout2_tasks_three_per_family(self):
        holdout2 = bt.split("holdout2")
        self.assertEqual(len(holdout2), 12)
        by_family = {}
        for name in holdout2:
            t = bt.TASKS[name]
            self.assertEqual(t.split, "holdout2", name)
            by_family.setdefault(t.family, 0)
            by_family[t.family] += 1
        self.assertEqual(by_family, {"repair": 3, "edit": 3, "bugfix": 3, "answer": 3})

    def test_holdout2_names_distinct_from_all_other_tasks(self):
        holdout2 = set(bt.split("holdout2"))
        others = set(bt.TASKS.keys()) - holdout2
        self.assertEqual(holdout2 & others, set())
        self.assertEqual(len(holdout2), len(_HOLDOUT2_CODE_TASK_NAMES) + len(_HOLDOUT2_ANSWER_TASK_NAMES))

    def test_all_expected_names_present(self):
        holdout2 = set(bt.split("holdout2"))
        expected = set(_HOLDOUT2_CODE_TASK_NAMES) | set(_HOLDOUT2_ANSWER_TASK_NAMES)
        self.assertEqual(holdout2, expected)

    def test_one_long_task_per_family(self):
        by_family = {}
        for name in _HOLDOUT2_LONG_TASKS:
            t = bt.TASKS[name]
            by_family.setdefault(t.family, 0)
            by_family[t.family] += 1
        self.assertEqual(by_family, {"repair": 1, "edit": 1, "bugfix": 1, "answer": 1})
        # The long tasks read/edit noticeably more: more files than their
        # family's other holdout2 tasks.
        for name in _HOLDOUT2_LONG_TASKS:
            long_file_count = len(bt.TASKS[name].files)
            family = bt.TASKS[name].family
            siblings = [
                n for n in bt.split("holdout2")
                if bt.TASKS[n].family == family and n != name
            ]
            for sib in siblings:
                self.assertGreaterEqual(
                    long_file_count, len(bt.TASKS[sib].files),
                    f"{name} should read at least as many files as {sib}",
                )

    def test_protected_files_match_existing_convention(self):
        for name in _HOLDOUT2_CODE_TASK_NAMES:
            t = bt.TASKS[name]
            self.assertIn("README.md", t.protected, name)
            if "test_public.py" in t.files:
                self.assertIn("test_public.py", t.protected, name)

    def test_reference_solutions_not_in_task_files_or_module_dict(self):
        # Requirement: reference solutions must never be readable from an
        # agent's workspace. Task.files is exactly what gets copied there.
        for name, overrides in _REFERENCE_SOLUTIONS_H2.items():
            task_files = bt.TASKS[name].files
            for filename, ref_content in overrides.items():
                self.assertIn(filename, task_files, f"{name}: {filename} must exist in starter files")
                self.assertNotEqual(
                    task_files[filename], ref_content,
                    f"{name}: starter {filename} must not already equal the reference solution",
                )
            # And REFERENCE_SOLUTIONS (the module-level dict used by the
            # original 15 tasks) must not have picked up entries for these
            # new names -- they are tracked only in this test file.
            self.assertNotIn(name, bt.REFERENCE_SOLUTIONS)


class TestHoldout2CodeTasks(unittest.TestCase):
    def test_starter_fails_reference_passes_wrong_fails(self):
        for name in _HOLDOUT2_CODE_TASK_NAMES:
            task = bt.TASKS[name]
            with self.subTest(task=name):
                ws = _materialize(task.files)
                try:
                    result = task.evaluate(ws)
                    self.assertGreater(result["failed"], 0, f"{name}: starter should fail evaluate, got {result}")
                finally:
                    shutil.rmtree(ws, ignore_errors=True)

                overrides = _REFERENCE_SOLUTIONS_H2[name]
                ws2 = _materialize(task.files, overrides)
                try:
                    result2 = task.evaluate(ws2)
                    self.assertEqual(
                        result2["failed"], 0,
                        f"{name}: reference solution should pass, got {result2['failure_labels']}",
                    )
                    self.assertGreater(result2["checks"], 0, name)

                    if "test_public.py" in task.files:
                        proc = subprocess.run(
                            [sys.executable, "-m", "unittest", "test_public.py"],
                            cwd=str(ws2),
                            capture_output=True,
                            text=True,
                            timeout=20,
                        )
                        self.assertEqual(
                            proc.returncode, 0,
                            f"{name}: reference solution should pass test_public.py\n{proc.stdout}\n{proc.stderr}",
                        )
                finally:
                    shutil.rmtree(ws2, ignore_errors=True)

                wrong_overrides = _WRONG_SOLUTIONS_H2[name]
                ws3 = _materialize(task.files, wrong_overrides)
                try:
                    result3 = task.evaluate(ws3)
                    wrong_evaluate_fails = result3["failed"] > 0
                    wrong_public_test_fails = False
                    if "test_public.py" in task.files:
                        proc3 = subprocess.run(
                            [sys.executable, "-m", "unittest", "test_public.py"],
                            cwd=str(ws3),
                            capture_output=True,
                            text=True,
                            timeout=20,
                        )
                        wrong_public_test_fails = proc3.returncode != 0
                    self.assertTrue(
                        wrong_evaluate_fails or wrong_public_test_fails,
                        f"{name}: plausible wrong solution should fail evaluate or test_public.py, "
                        f"got evaluate={result3}",
                    )
                finally:
                    shutil.rmtree(ws3, ignore_errors=True)

    def test_bugfix_starters_fail_public_test(self):
        for name in _HOLDOUT2_CODE_TASK_NAMES:
            task = bt.TASKS[name]
            if task.family != "bugfix":
                continue
            with self.subTest(task=name):
                ws = _materialize(task.files)
                try:
                    proc = subprocess.run(
                        [sys.executable, "-m", "unittest", "test_public.py"],
                        cwd=str(ws),
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    self.assertNotEqual(proc.returncode, 0, f"{name}: buggy starter should fail public test")
                finally:
                    shutil.rmtree(ws, ignore_errors=True)


class TestHoldout2AnswerTasks(unittest.TestCase):
    _CORRECT = {
        "answer_cache_eviction_policy": ["lru", "LRU", "`lru`"],
        "answer_retry_max_attempts": ["4", "four", "The answer is 4."],
        "answer_shipping_zone_count": ["5", "five"],
    }
    _DECOYS = {
        "answer_cache_eviction_policy": ["fifo", "random", "lru_cache"],
        "answer_retry_max_attempts": ["3", "5", "MAX_RETRIES"],
        "answer_shipping_zone_count": ["4", "6", "3", "9"],
    }

    def test_answer_tasks_are_answer_kind_with_zero_check_evaluate(self):
        for name in _HOLDOUT2_ANSWER_TASK_NAMES:
            task = bt.TASKS[name]
            self.assertEqual(task.kind, "answer", name)
            self.assertEqual(task.family, "answer", name)
            result = task.evaluate(Path("/nonexistent"))
            self.assertEqual(result, {"checks": 0, "passed": 0, "failed": 0, "failure_labels": []})

    def test_correct_answers_pass(self):
        for name, answers in self._CORRECT.items():
            task = bt.TASKS[name]
            for ans in answers:
                with self.subTest(task=name, answer=ans):
                    result = bt.check_answer(task, f"ANSWER: {ans}")
                    self.assertEqual(result["failed"], 0, result)
                    self.assertEqual(result["passed"], 1)

    def test_decoys_and_missing_answer_fail(self):
        for name, decoys in self._DECOYS.items():
            task = bt.TASKS[name]
            for decoy in decoys:
                with self.subTest(task=name, decoy=decoy):
                    result = bt.check_answer(task, f"ANSWER: {decoy}")
                    self.assertEqual(result["failed"], 1, result)
            with self.subTest(task=name, case="no_answer_line"):
                result = bt.check_answer(task, "DONE: finished looking around")
                self.assertEqual(result["failed"], 1)
            with self.subTest(task=name, case="none"):
                result = bt.check_answer(task, None)
                self.assertEqual(result["failed"], 1)


if __name__ == "__main__":
    unittest.main()
